import re
from collections.abc import Sequence

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.models import Article

_WORD = re.compile(r"\w+", re.UNICODE)
_PREFIX_LEN = 5


def build_prefix_tsquery(query: str) -> str | None:
    """OR-query of word prefixes: a cheap stand-in for stemming with the 'simple' config.

    "експортувати звіт" -> "експо:* | звіт:*", which also matches "експорт", "звіти", ...
    """
    terms = {w[:_PREFIX_LEN] for w in (m.lower() for m in _WORD.findall(query)) if len(w) >= 3}
    if not terms:
        return None
    return " | ".join(f"{t}:*" for t in sorted(terms))


class ArticleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, slug: str) -> Article | None:
        return await self.session.get(Article, slug)

    async def list_all(self) -> list[Article]:
        return list(await self.session.scalars(select(Article).order_by(Article.title)))

    async def get_many(self, slugs: Sequence[str]) -> dict[str, Article]:
        rows = await self.session.scalars(select(Article).where(Article.slug.in_(slugs)))
        return {a.slug: a for a in rows}

    async def upsert(self, values: dict[str, object]) -> None:
        stmt = insert(Article).values(**values)
        await self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Article.slug],
                set_={
                    k: stmt.excluded[k]
                    for k in ("title", "category", "tags", "language", "body", "content_hash")
                }
                | {"updated_at": func.now()},
                where=Article.content_hash != stmt.excluded.content_hash,
            )
        )

    async def delete_except(self, slugs: Sequence[str]) -> None:
        await self.session.execute(delete(Article).where(Article.slug.not_in(slugs)))

    async def search_fulltext(self, query: str, limit: int) -> list[tuple[Article, float]]:
        tsquery = build_prefix_tsquery(query)
        if tsquery is None:
            return []
        q = func.to_tsquery(text("'simple'"), tsquery)
        rank = func.ts_rank(Article.search_vector, q).label("rank")
        rows = await self.session.execute(
            select(Article, rank)
            .where(Article.search_vector.op("@@")(q))
            .order_by(rank.desc())
            .limit(limit)
        )
        return [(article, float(score)) for article, score in rows]
