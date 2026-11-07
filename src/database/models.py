"""ORM-модели BRUTAL CUTS (SQLAlchemy 2.0, строгая типизация ``Mapped``).

Таблицы:

* ``users``        — клиенты Telegram-бота;
* ``appointments`` — записи клиентов (снимок данных CRM на момент брони);
* ``reviews``      — отзывы о завершённых визитах.

Все даты и время хранятся в UTC. Тип ``UTCDateTime`` гарантирует, что и в
PostgreSQL, и в SQLite из базы всегда возвращается «aware»-datetime в UTC
(SQLite сам по себе теряет информацию о часовом поясе).
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    false,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator, TypeEngine

# ----------------------------------------------------------------------
# Базовые элементы
# ----------------------------------------------------------------------

# Единые имена ограничений и индексов — это упростит миграции Alembic.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Базовый класс всех моделей."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> dt.datetime:
    """Текущий момент как aware-datetime в UTC."""
    return dt.datetime.now(dt.timezone.utc)


class UTCDateTime(TypeDecorator[dt.datetime]):
    """DateTime, который всегда хранит UTC и всегда возвращает aware-UTC.

    * при записи naive-datetime отклоняется (иначе легко перепутать пояса);
    * при записи aware-datetime приводится к UTC;
    * при чтении SQLite отдаёт naive-значение — оно трактуется как UTC.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        # TIMESTAMP WITH TIME ZONE в PostgreSQL; SQLite параметр игнорирует.
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(
        self,
        value: dt.datetime | None,
        dialect: Dialect,
    ) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "Naive datetime не допускается: передайте datetime с часовым поясом."
            )
        return value.astimezone(dt.timezone.utc)

    def process_result_value(
        self,
        value: dt.datetime | None,
        dialect: Dialect,
    ) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)


def _enum_values(enum_cls: type[enum.Enum]) -> Sequence[str]:
    """В БД пишутся значения enum ('created'), а не имена ('CREATED')."""
    return [str(member.value) for member in enum_cls]


# ----------------------------------------------------------------------
# Перечисления
# ----------------------------------------------------------------------


class BookingStatus(enum.StrEnum):
    """Статус записи в локальной базе.

    Назван ``BookingStatus``, чтобы не путать с ``AppointmentStatus`` из CRM-слоя
    (там статусы pending / confirmed / cancelled).
    """

    CREATED = "created"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


#: Статусы «живых» записей: клиент ещё должен прийти.
ACTIVE_STATUSES: Final[tuple[BookingStatus, ...]] = (
    BookingStatus.CREATED,
    BookingStatus.CONFIRMED,
)


# ----------------------------------------------------------------------
# Модели
# ----------------------------------------------------------------------


class User(Base):
    """Клиент бота. Первичный ключ — Telegram ID (он же уникален)."""

    __tablename__ = "users"

    # Telegram ID не помещаются в int32, поэтому BigInteger; autoincrement отключён —
    # значение всегда приходит из Telegram.
    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
    )
    first_name: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64), default=None)
    # Пустой, пока клиент не авторизовался (не поделился контактом).
    phone: Mapped[str | None] = mapped_column(String(16), default=None, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime,
        default=utcnow,
        server_default=func.now(),
    )

    # В async-коде неявная подгрузка связей невозможна, поэтому lazy="raise":
    # связи нужно загружать явно (selectinload / joinedload).
    appointments: Mapped[list[Appointment]] = relationship(
        back_populates="user",
        lazy="raise",
        passive_deletes=True,
    )
    reviews: Mapped[list[Review]] = relationship(
        back_populates="user",
        lazy="raise",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"User(telegram_id={self.telegram_id}, first_name={self.first_name!r})"


class Appointment(Base):
    """Запись клиента.

    Хранит снимок данных CRM (имя мастера, услугу, цену) на момент брони:
    история остаётся корректной, даже если мастер или прайс изменятся.
    """

    __tablename__ = "appointments"
    __table_args__ = (
        # Основной запрос напоминаний: активные записи в ближайшем окне времени.
        Index("ix_appointments_status_datetime_start", "status", "datetime_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # ID записи в CRM. Не уникален намеренно: Mock-CRM после перезапуска
    # начинает нумерацию заново, а записи в локальной БД сохраняются.
    crm_record_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        index=True,
    )
    branch_id: Mapped[int] = mapped_column(Integer)
    staff_id: Mapped[int] = mapped_column(Integer)
    staff_name: Mapped[str] = mapped_column(String(128))
    staff_grade: Mapped[str] = mapped_column(String(32))
    service_name: Mapped[str] = mapped_column(String(255))
    # Список названий доп-опций, например ["Патчи", "Маска-детокс"].
    addons: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        server_default=text("'[]'"),
    )
    datetime_start: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    # Конец визита (начало + длительность услуги). NULL у записей, созданных до появления
    # колонки: для них длительность считается равной одному часу.
    datetime_end: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)
    total_price: Mapped[int] = mapped_column(Integer)
    # Напиток к визиту (клубная опция); None — без напитка.
    beverage: Mapped[str | None] = mapped_column(String(64), default=None)
    # «Тихая стрижка» — без разговоров.
    silent_cut: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )
    status: Mapped[BookingStatus] = mapped_column(
        SAEnum(
            BookingStatus,
            name="booking_status",
            native_enum=False,  # VARCHAR вместо PG ENUM: проще миграции
            length=16,
            values_callable=_enum_values,
        ),
        default=BookingStatus.CREATED,
    )
    # Когда клиенту отправлены напоминания (NULL — ещё не отправлялось).
    # По одной колонке на каждое напоминание, чтобы планировщик не слал их повторно.
    reminder_24h_sent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)
    reminder_2h_sent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)
    # Когда клиенту отправлен запрос на оценку визита (NULL — ещё не отправлялся).
    review_requested_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime,
        default=utcnow,
        server_default=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="appointments", lazy="raise")
    review: Mapped[Review | None] = relationship(
        back_populates="appointment",
        lazy="raise",
        uselist=False,
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"Appointment(id={self.id}, user_id={self.user_id}, "
            f"status={self.status.value!r}, datetime_start={self.datetime_start.isoformat()})"
        )


class Review(Base):
    """Отзыв о визите. На одну запись — не более одного отзыва."""

    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"),
        unique=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        index=True,
    )
    rating: Mapped[int] = mapped_column(Integer)
    feedback_text: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime,
        default=utcnow,
        server_default=func.now(),
    )

    appointment: Mapped[Appointment] = relationship(back_populates="review", lazy="raise")
    user: Mapped[User] = relationship(back_populates="reviews", lazy="raise")

    def __repr__(self) -> str:
        return f"Review(id={self.id}, appointment_id={self.appointment_id}, rating={self.rating})"
