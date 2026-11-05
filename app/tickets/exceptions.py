import uuid

from app.core.exceptions import ConflictError, NotFoundError
from app.tickets.models import TicketStatus


class TicketNotFoundError(NotFoundError):
    def __init__(self, ticket_id: uuid.UUID) -> None:
        super().__init__(f"Ticket {ticket_id} not found")


class InvalidTransitionError(ConflictError):
    def __init__(self, ticket_id: uuid.UUID, current: TicketStatus, action: str) -> None:
        super().__init__(f"Cannot {action} ticket {ticket_id} in status '{current}'")
        self.current = current
