from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RetrievalStrategy(StrEnum):
    VECTOR = "vector"
    FULLTEXT = "fulltext"


@dataclass(frozen=True, slots=True)
class RetrievedArticle:
    slug: str
    title: str
    url: str
    body: str
    score: float


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    articles: list[RetrievedArticle]
    strategy: RetrievalStrategy


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    title: str
    category: str
    tags: list[str]
    language: str
