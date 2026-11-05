from sqlalchemy import Computed, Index, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class Article(TimestampMixin, Base):
    """Source of truth for knowledge-base articles.

    Vectors live in the PGVector collection (managed by langchain-postgres); this table backs
    the /kb pages and the full-text fallback used when embeddings are unavailable.
    """

    __tablename__ = "kb_articles"

    slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False, default=list)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', title || ' ' || body)", persisted=True),
    )

    __table_args__ = (Index("ix_kb_articles_search_vector", "search_vector", postgresql_using="gin"),)
