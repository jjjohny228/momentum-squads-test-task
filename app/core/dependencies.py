import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import get_session


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

_basic = HTTPBasic(auto_error=False)


def require_operator(
    settings: SettingsDep,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> None:
    """HTTP Basic auth for the operator UI/API; disabled when no credentials are configured."""
    if not settings.operator_username or not settings.operator_password:
        return
    expected_user = settings.operator_username.encode()
    expected_password = settings.operator_password.get_secret_value().encode()
    if credentials is None or not (
        secrets.compare_digest(credentials.username.encode(), expected_user)
        and secrets.compare_digest(credentials.password.encode(), expected_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
