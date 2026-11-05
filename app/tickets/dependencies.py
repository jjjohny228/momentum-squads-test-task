from typing import Annotated

from fastapi import Depends

from app.core.dependencies import SessionDep, SettingsDep
from app.tickets.service import TicketService


def get_ticket_service(session: SessionDep, settings: SettingsDep) -> TicketService:
    return TicketService(session, settings)


TicketServiceDep = Annotated[TicketService, Depends(get_ticket_service)]
