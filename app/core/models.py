"""Imports every ORM model so `Base.metadata` is complete (Alembic, tests)."""

from app.core.db import Base
from app.drafting.models import ProviderCall
from app.jobs.models import Job
from app.knowledge.models import Article
from app.tickets.models import Ticket

__all__ = ["Article", "Base", "Job", "ProviderCall", "Ticket"]
