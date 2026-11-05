"""Load simulator that behaves like the helpdesk.

    uv run python -m scripts.simulate --count 500 --duration 600 --duplicates 0.3

Sends `count` tickets spread over `duration` seconds with the real helpdesk's semantics:
2 s timeout, re-delivery on timeout/5xx, a share of events re-sent and some delivered
concurrently. Then (with --wait) polls stats until every ticket has a draft.
"""

import argparse
import asyncio
import random
import statistics
import time
import uuid
from collections import Counter
from typing import Any

import httpx

MESSAGES = [
    "Не можу експортувати звіт у PDF",
    "Забув пароль, не можу увійти в акаунт",
    "Як запросити колегу в команду?",
    "Дашборд дуже повільно завантажується",
    "Не приходить лист із запланованим звітом",
    "Як підключити Google Sheets як джерело даних?",
    "Де знайти рахунок за минулий місяць?",
    "Кирилиця в CSV відкривається кракозябрами в Excel",
]
TIMEOUT = 2.0
RETRIES = 3


class Stats:
    def __init__(self) -> None:
        self.latencies: list[float] = []
        self.results: Counter[str] = Counter()


async def deliver(
    client: httpx.AsyncClient, url: str, payload: dict[str, str], stats: Stats
) -> None:
    for _ in range(RETRIES):
        started = time.perf_counter()
        try:
            r = await client.post(url, json=payload, timeout=TIMEOUT)
            stats.latencies.append(time.perf_counter() - started)
            if r.status_code < 500:
                stats.results[f"{r.status_code} {r.json().get('status', '')}".strip()] += 1
                return
            stats.results[f"{r.status_code} (redelivered)"] += 1
        except httpx.HTTPError as exc:
            stats.latencies.append(time.perf_counter() - started)
            stats.results[f"{type(exc).__name__} (redelivered)"] += 1
    stats.results["lost after retries"] += 1


def make_payload(reply_base: str) -> dict[str, str]:
    ticket_id = f"T-{random.randint(10000, 999999)}"
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "ticket_id": ticket_id,
        "customer_email": f"user{random.randint(1, 999)}@example.com",
        "message": random.choice(MESSAGES),
        "reply_url": f"{reply_base}/tickets/{ticket_id}/replies",
    }


async def send_one(client: httpx.AsyncClient, args: argparse.Namespace, stats: Stats) -> None:
    payload = make_payload(args.reply_base)
    url = f"{args.base_url}/webhooks/helpdesk"
    copies = 1
    if random.random() < args.duplicates:
        copies += random.randint(1, 3)
    if random.random() < args.concurrent:
        await asyncio.gather(*(deliver(client, url, payload, stats) for _ in range(copies)))
    else:
        for _ in range(copies):
            await deliver(client, url, payload, stats)


async def wait_for_drafts(client: httpx.AsyncClient, base_url: str, expected: int) -> None:
    print("\nwaiting for drafts (Ctrl+C to stop)...")
    peak_rpm = 0
    while True:
        s: dict[str, Any] = (await client.get(f"{base_url}/api/v1/tickets/stats")).json()
        by_status = s["tickets_by_status"]
        pending = by_status.get("received", 0) + by_status.get("generating", 0)
        peak_rpm = max(peak_rpm, s["provider_calls_last_minute"])
        print(
            f"  {by_status}  provider calls/60s={s['provider_calls_last_minute']}"
            f" (limit {s['rate_limit_rpm']}, peak {peak_rpm})"
        )
        if pending == 0 and sum(by_status.values()) >= expected:
            return
        await asyncio.sleep(10)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--reply-base", default="http://mock-helpdesk:8001")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--duration", type=float, default=60, help="seconds to spread tickets over")
    parser.add_argument("--duplicates", type=float, default=0.3, help="share of re-sent events")
    parser.add_argument(
        "--concurrent", type=float, default=0.5, help="share of duplicates sent at once"
    )
    parser.add_argument("--wait", action="store_true", help="poll until all drafts are generated")
    args = parser.parse_args()

    stats = Stats()
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=200)) as client:
        before = (await client.get(f"{args.base_url}/api/v1/tickets/stats")).json()
        started = time.perf_counter()
        tasks = []
        for i in range(args.count):
            tasks.append(asyncio.create_task(send_one(client, args, stats)))
            await asyncio.sleep(
                max(0.0, started + args.duration * (i + 1) / args.count - time.perf_counter())
            )
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started

        lat = sorted(stats.latencies)
        print(f"sent {args.count} tickets in {elapsed:.1f}s, {len(lat)} HTTP deliveries")
        print(
            f"latency p50={statistics.median(lat) * 1000:.0f}ms "
            f"p95={lat[int(len(lat) * 0.95) - 1] * 1000:.0f}ms max={lat[-1] * 1000:.0f}ms"
        )
        for key, n in sorted(stats.results.items()):
            print(f"  {key}: {n}")

        after = (await client.get(f"{args.base_url}/api/v1/tickets/stats")).json()
        created = sum(after["tickets_by_status"].values()) - sum(
            before["tickets_by_status"].values()
        )
        print(f"tickets created: {created} (expected {args.count})")
        if args.wait:
            await wait_for_drafts(client, args.base_url, sum(after["tickets_by_status"].values()))


if __name__ == "__main__":
    asyncio.run(main())
