"""Асинхронный движок и фабрика сессий SQLAlchemy 2.0.

По умолчанию бот работает на локальном файле SQLite
(``sqlite+aiosqlite:///./brutal_cuts.db``). Чтобы перейти на PostgreSQL,
достаточно задать ``DATABASE_URL=postgresql+asyncpg://...`` — код менять не нужно.

Использование::

    session_factory = await init_db(settings.DATABASE_URL)

    async with session_scope() as session:          # commit / rollback автоматически
        users = UserRepository(session)
        await users.get_or_create(telegram_id=1, first_name="Иван")

    await close_db()
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.database.models import Base

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL: Final[str] = "sqlite+aiosqlite:///./brutal_cuts.db"

_SUPPORTED_DRIVERS: Final[frozenset[str]] = frozenset(
    {"sqlite+aiosqlite", "postgresql+asyncpg"}
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


# ----------------------------------------------------------------------
# Создание движка
# ----------------------------------------------------------------------


def _is_sqlite_memory(url: URL) -> bool:
    return url.database in (None, "", ":memory:")


def _ensure_sqlite_directory(url: URL) -> None:
    """Создаёт каталог для файла SQLite, если его ещё нет."""
    if _is_sqlite_memory(url) or url.database is None:
        return
    Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _prepare_asyncpg_url(url: URL) -> URL:
    """asyncpg не понимает ``sslmode`` (так пишут Heroku, Neon и т. п.) — нужен ``ssl``."""
    query: dict[str, Any] = dict(url.query)
    sslmode = query.pop("sslmode", None)
    if sslmode is not None and "ssl" not in query:
        query["ssl"] = sslmode
        return url.set(query=query)
    return url


def create_engine(database_url: str = DEFAULT_DATABASE_URL, *, echo: bool = False) -> AsyncEngine:
    """Создаёт асинхронный движок для SQLite (aiosqlite) или PostgreSQL (asyncpg)."""
    url = make_url(database_url)
    if url.drivername not in _SUPPORTED_DRIVERS:
        raise ValueError(
            f"Неподдерживаемый драйвер БД '{url.drivername}'. "
            "Используйте 'sqlite+aiosqlite://...' или 'postgresql+asyncpg://...'."
        )

    if url.get_backend_name() == "sqlite":
        _ensure_sqlite_directory(url)
        engine = create_async_engine(url, echo=echo)
        in_memory = _is_sqlite_memory(url)

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                # Без этого SQLite игнорирует внешние ключи и ON DELETE CASCADE.
                cursor.execute("PRAGMA foreign_keys=ON")
                # Ожидание при блокировке вместо мгновенной ошибки "database is locked".
                cursor.execute("PRAGMA busy_timeout=5000")
                if not in_memory:
                    # Читатели не блокируют писателя: хендлеры и планировщик работают параллельно.
                    cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

        return engine

    return create_async_engine(
        _prepare_asyncpg_url(url),
        echo=echo,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
    )


# ----------------------------------------------------------------------
# Жизненный цикл
# ----------------------------------------------------------------------


async def init_db(
    database_url: str = DEFAULT_DATABASE_URL,
    *,
    echo: bool = False,
) -> async_sessionmaker[AsyncSession]:
    """Создаёт движок и фабрику сессий, затем создаёт недостающие таблицы.

    Повторный вызов безопасен: если БД уже инициализирована, возвращается
    существующая фабрика.
    """
    global _engine, _session_factory

    if _session_factory is not None:
        return _session_factory

    engine = create_engine(database_url, echo=echo)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    except Exception:
        await engine.dispose()
        raise

    _engine = engine
    _session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        # Объекты остаются доступными после commit без повторного обращения к БД.
        expire_on_commit=False,
    )
    logger.info("БД инициализирована: %s", engine.url.render_as_string(hide_password=True))
    return _session_factory


async def close_db() -> None:
    """Закрывает пул соединений и сбрасывает глобальное состояние."""
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def get_engine() -> AsyncEngine:
    """Текущий движок. Требует предварительного ``init_db``."""
    if _engine is None:
        raise RuntimeError("База данных не инициализирована: вызовите init_db() при старте.")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Текущая фабрика сессий. Требует предварительного ``init_db``."""
    if _session_factory is None:
        raise RuntimeError("База данных не инициализирована: вызовите init_db() при старте.")
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия с транзакцией: commit при успехе, rollback при исключении."""
    session = get_session_factory()()
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()
