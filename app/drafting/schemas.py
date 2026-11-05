from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.knowledge.schemas import RetrievalStrategy
from app.tickets.models import DraftSource
from app.tickets.schemas import SourceRef


class DraftOutput(BaseModel):
    """Structured output requested from the LLM."""

    answer: str = Field(description="Reply text for the customer, without URLs")
    cited_slugs: list[str] = Field(
        default_factory=list, description="Slugs of the knowledge-base articles the reply relies on"
    )
    needs_human: bool = Field(
        default=False, description="True when the articles do not answer the customer's question"
    )


@dataclass(frozen=True, slots=True)
class GeneratedDraft:
    text: str
    source: DraftSource
    sources: list[SourceRef] = field(default_factory=list)
    needs_manual_reply: bool = False
    retrieval: RetrievalStrategy | None = None
