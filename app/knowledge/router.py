from pathlib import Path

import markdown
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.dependencies import SessionDep
from app.core.exceptions import NotFoundError
from app.knowledge.repository import ArticleRepository
from app.knowledge.schemas import ArticleOut

router = APIRouter(tags=["knowledge-base"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "ui" / "templates")


@router.get("/api/v1/kb")
async def list_articles(session: SessionDep) -> list[ArticleOut]:
    return [ArticleOut.model_validate(a) for a in await ArticleRepository(session).list_all()]


@router.get("/kb/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def article_page(slug: str, request: Request, session: SessionDep) -> HTMLResponse:
    article = await ArticleRepository(session).get(slug)
    if article is None:
        raise NotFoundError(f"Article '{slug}' not found")
    html = markdown.markdown(article.body, extensions=["tables"])
    return templates.TemplateResponse(
        request, "article.html", {"article": article, "content": html}
    )
