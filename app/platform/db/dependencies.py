"""FastAPI transaction dependency shared by business routers."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.platform.db.session import transactional_session


async def get_database_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.database_session_factory
    async with transactional_session(factory) as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_database_session)]
