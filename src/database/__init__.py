"""Слой хранения данных: модели, сессии и репозитории."""

from src.database.models import (
    ACTIVE_STATUSES,
    Appointment,
    Base,
    BookingStatus,
    Review,
    User,
)
from src.database.repositories import (
    AppointmentRepository,
    DuplicateEntityError,
    EntityNotFoundError,
    InvalidStatusTransitionError,
    RepositoryError,
    ReviewNotAllowedError,
    ReviewRepository,
    UserRepository,
)
from src.database.session import (
    DEFAULT_DATABASE_URL,
    close_db,
    create_engine,
    get_engine,
    get_session_factory,
    init_db,
    session_scope,
)

__all__ = [
    "ACTIVE_STATUSES",
    "DEFAULT_DATABASE_URL",
    "Appointment",
    "AppointmentRepository",
    "Base",
    "BookingStatus",
    "DuplicateEntityError",
    "EntityNotFoundError",
    "InvalidStatusTransitionError",
    "RepositoryError",
    "Review",
    "ReviewNotAllowedError",
    "ReviewRepository",
    "User",
    "UserRepository",
    "close_db",
    "create_engine",
    "get_engine",
    "get_session_factory",
    "init_db",
    "session_scope",
]
