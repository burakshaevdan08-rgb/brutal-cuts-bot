"""Репозитории: вся работа с БД собрана здесь, хендлеры SQL не пишут.

Правила:

* репозиторий получает ``AsyncSession`` и делает только ``flush`` — коммит
  выполняет вызывающий код (обычно ``session_scope()``), поэтому несколько
  операций можно объединить в одну транзакцию;
* все даты — aware-datetime; в БД они хранятся в UTC;
* телефон передаётся в нормализованном виде ``+7XXXXXXXXXX``
  (для нормализации используйте ``normalize_phone`` из CRM-слоя).
"""

from __future__ import annotations

import datetime as dt
import enum
import re
from collections.abc import Collection, Sequence
from typing import Any, Final

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import (
    ACTIVE_STATUSES,
    Appointment,
    BookingStatus,
    Review,
    User,
    utcnow,
)

# ----------------------------------------------------------------------
# Исключения
# ----------------------------------------------------------------------


class RepositoryError(Exception):
    """Базовое исключение слоя репозиториев."""


class EntityNotFoundError(RepositoryError):
    """Запрошенная запись не найдена в БД."""


class DuplicateEntityError(RepositoryError):
    """Запись с таким ключом уже существует."""


class InvalidStatusTransitionError(RepositoryError):
    """Недопустимый переход между статусами записи."""


class SlotTakenError(RepositoryError):
    """У мастера на это время уже есть активная запись (статус created или confirmed)."""

    def __init__(self, message: str, conflicting_id: int | None = None) -> None:
        super().__init__(message)
        self.conflicting_id = conflicting_id


class ReviewNotAllowedError(RepositoryError):
    """Отзыв оставить нельзя (визит не завершён, чужая запись или отзыв уже есть)."""


# ----------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------

class ReminderKind(enum.StrEnum):
    """Тип напоминания клиенту."""

    H24 = "24h"  # за сутки: подтвердить / перенести / отменить
    H2 = "2h"    # за 2 часа: навигация до филиала


_REMINDER_COLUMNS: Final = {
    ReminderKind.H24: Appointment.reminder_24h_sent_at,
    ReminderKind.H2: Appointment.reminder_2h_sent_at,
}

_PHONE_RE: Final = re.compile(r"^\+\d{10,15}$")

# Длительность визита по умолчанию: для записей без ``datetime_end`` и когда конец не передан.
DEFAULT_VISIT_DURATION: Final = dt.timedelta(hours=1)
# Самый длинный визит; нужен, чтобы ограничить выборку кандидатов на пересечение.
MAX_VISIT_DURATION: Final = dt.timedelta(hours=6)

_ALLOWED_TRANSITIONS: Final[dict[BookingStatus, frozenset[BookingStatus]]] = {
    BookingStatus.CREATED: frozenset(
        {BookingStatus.CONFIRMED, BookingStatus.CANCELLED, BookingStatus.COMPLETED}
    ),
    BookingStatus.CONFIRMED: frozenset({BookingStatus.CANCELLED, BookingStatus.COMPLETED}),
    BookingStatus.CANCELLED: frozenset(),
    BookingStatus.COMPLETED: frozenset(),
}


def _validate_phone(phone: str) -> str:
    cleaned = phone.strip()
    if not _PHONE_RE.match(cleaned):
        raise ValueError(
            "Телефон должен быть в международном формате, например +79991234567."
        )
    return cleaned


def _require_aware(value: dt.datetime, field_name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}: требуется datetime с часовым поясом.")
    return value


def _insert_for(session: AsyncSession, model: type[Any]) -> Any:
    """Диалектный ``INSERT`` с поддержкой ``ON CONFLICT DO NOTHING``.

    Это позволяет создавать запись без SAVEPOINT и без падения транзакции
    при гонке двух одновременных /start от одного пользователя.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert(model)
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert(model)
    raise RuntimeError(f"Неподдерживаемый диалект БД: {dialect}")


# ----------------------------------------------------------------------
# Пользователи
# ----------------------------------------------------------------------


class UserRepository:
    """CRUD для клиентов бота."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, telegram_id: int) -> User | None:
        """Пользователь по Telegram ID."""
        return await self._session.get(User, telegram_id)

    async def get_by_phone(self, phone: str) -> User | None:
        """Пользователь по телефону (первый, если номер привязан к нескольким)."""
        stmt = select(User).where(User.phone == _validate_phone(phone)).limit(1)
        return (await self._session.execute(stmt)).scalars().first()

    async def get_many(self, telegram_ids: Collection[int]) -> dict[int, User]:
        """Пользователи по списку Telegram ID: ``{telegram_id: User}``."""
        if not telegram_ids:
            return {}
        stmt = select(User).where(User.telegram_id.in_(list(telegram_ids)))
        return {user.telegram_id: user for user in (await self._session.execute(stmt)).scalars()}

    async def create(
        self,
        *,
        telegram_id: int,
        first_name: str,
        username: str | None = None,
        phone: str | None = None,
    ) -> User:
        """Создаёт пользователя. Бросает ``DuplicateEntityError``, если он уже есть."""
        created = await self._insert_if_absent(
            telegram_id=telegram_id,
            first_name=first_name,
            username=username,
            phone=phone,
        )
        if not created:
            raise DuplicateEntityError(f"Пользователь {telegram_id} уже существует.")
        return await self._require(telegram_id)

    async def get_or_create(
        self,
        *,
        telegram_id: int,
        first_name: str,
        username: str | None = None,
    ) -> tuple[User, bool]:
        """Возвращает ``(пользователь, создан_ли_сейчас)``.

        Для существующего пользователя обновляет имя и username, если они
        изменились в Telegram. Безопасно при параллельных вызовах.
        """
        user = await self.get(telegram_id)
        if user is not None:
            await self._sync_profile(user, first_name, username)
            return user, False

        created = await self._insert_if_absent(
            telegram_id=telegram_id,
            first_name=first_name,
            username=username,
            phone=None,
        )
        user = await self._require(telegram_id)
        if not created:
            # Параллельный запрос успел создать запись раньше.
            await self._sync_profile(user, first_name, username)
        return user, created

    async def update_profile(
        self,
        telegram_id: int,
        *,
        first_name: str,
        username: str | None,
    ) -> User:
        """Обновляет имя и username пользователя."""
        user = await self._require(telegram_id)
        await self._sync_profile(user, first_name, username)
        return user

    async def update_phone(self, telegram_id: int, phone: str) -> User:
        """Сохраняет номер телефона (авторизация клиента)."""
        user = await self._require(telegram_id)
        user.phone = _validate_phone(phone)
        await self._session.flush()
        return user

    async def delete(self, telegram_id: int) -> bool:
        """Удаляет пользователя вместе с его записями и отзывами (ON DELETE CASCADE).

        Возвращает ``True``, если пользователь существовал.
        """
        result = await self._session.execute(
            delete(User).where(User.telegram_id == telegram_id)
        )
        return bool(result.rowcount)

    # --- внутренние методы -------------------------------------------

    async def _require(self, telegram_id: int) -> User:
        user = await self.get(telegram_id)
        if user is None:
            raise EntityNotFoundError(f"Пользователь {telegram_id} не найден.")
        return user

    async def _insert_if_absent(
        self,
        *,
        telegram_id: int,
        first_name: str,
        username: str | None,
        phone: str | None,
    ) -> bool:
        stmt = (
            _insert_for(self._session, User)
            .values(
                telegram_id=telegram_id,
                first_name=first_name.strip() or "Гость",
                username=username,
                phone=_validate_phone(phone) if phone else None,
                created_at=utcnow(),
            )
            .on_conflict_do_nothing(index_elements=["telegram_id"])
        )
        result = await self._session.execute(stmt)
        return result.rowcount == 1

    async def _sync_profile(self, user: User, first_name: str, username: str | None) -> None:
        new_name = first_name.strip() or user.first_name
        changed = False
        if user.first_name != new_name:
            user.first_name = new_name
            changed = True
        if user.username != username:
            user.username = username
            changed = True
        if changed:
            await self._session.flush()


# ----------------------------------------------------------------------
# Записи
# ----------------------------------------------------------------------


class AppointmentRepository:
    """CRUD и выборки для записей клиентов."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- создание -----------------------------------------------------

    async def create(
        self,
        *,
        user_id: int,
        crm_record_id: str | int,
        branch_id: int,
        staff_id: int,
        staff_name: str,
        staff_grade: str,
        service_name: str,
        datetime_start: dt.datetime,
        total_price: int,
        datetime_end: dt.datetime | None = None,
        addons: Sequence[str] = (),
        beverage: str | None = None,
        silent_cut: bool = False,
        status: BookingStatus = BookingStatus.CREATED,
    ) -> Appointment:
        """Сохраняет бронь, созданную в CRM.

        Если у мастера на это время уже есть активная запись (created / confirmed),
        бросает ``SlotTakenError``: повторно записаться на тот же слот нельзя.
        Проверка пропускается для статусов cancelled и completed.
        """
        _require_aware(datetime_start, "datetime_start")
        if datetime_end is not None:
            _require_aware(datetime_end, "datetime_end")
            if datetime_end <= datetime_start:
                raise ValueError("datetime_end должен быть позже datetime_start.")
        if total_price < 0:
            raise ValueError("total_price не может быть отрицательным.")
        if await self._session.get(User, user_id) is None:
            raise EntityNotFoundError(f"Пользователь {user_id} не найден.")

        if status in ACTIVE_STATUSES:
            conflict = await self.find_conflict(
                branch_id=branch_id,
                staff_id=staff_id,
                start=datetime_start,
                end=datetime_end,
            )
            if conflict is not None:
                raise SlotTakenError(
                    f"У мастера {staff_name} на {datetime_start.isoformat()} уже есть "
                    f"активная запись (№{conflict.id}).",
                    conflicting_id=conflict.id,
                )

        appointment = Appointment(
            user_id=user_id,
            crm_record_id=str(crm_record_id),
            branch_id=branch_id,
            staff_id=staff_id,
            staff_name=staff_name,
            staff_grade=str(staff_grade),
            service_name=service_name,
            addons=list(addons),
            datetime_start=datetime_start,
            datetime_end=datetime_end,
            total_price=total_price,
            beverage=beverage,
            silent_cut=silent_cut,
            status=status,
        )
        self._session.add(appointment)
        await self._session.flush()
        return appointment

    # --- проверка занятости слота ----------------------------------------

    async def find_conflict(
        self,
        *,
        branch_id: int,
        staff_id: int,
        start: dt.datetime,
        end: dt.datetime | None = None,
        exclude_id: int | None = None,
    ) -> Appointment | None:
        """Активная запись мастера, пересекающаяся с интервалом ``[start, end)``.

        Активные — это статусы created и confirmed. Если ``end`` не передан, интервал
        считается часовым. Записи без ``datetime_end`` трактуются как часовые.
        Пересечение считается в Python, чтобы не зависеть от арифметики дат конкретной СУБД.
        """
        _require_aware(start, "start")
        new_end = _require_aware(end, "end") if end is not None else start + DEFAULT_VISIT_DURATION

        stmt = select(Appointment).where(
            Appointment.branch_id == branch_id,
            Appointment.staff_id == staff_id,
            Appointment.status.in_(ACTIVE_STATUSES),
            Appointment.datetime_start < new_end,
            Appointment.datetime_start > start - MAX_VISIT_DURATION,
        )
        if exclude_id is not None:
            stmt = stmt.where(Appointment.id != exclude_id)
        stmt = stmt.order_by(Appointment.datetime_start.asc(), Appointment.id.asc())

        for candidate in (await self._session.execute(stmt)).scalars():
            candidate_end = candidate.datetime_end or candidate.datetime_start + DEFAULT_VISIT_DURATION
            if candidate.datetime_start < new_end and candidate_end > start:
                return candidate
        return None

    async def list_active_since(self, since: dt.datetime) -> list[Appointment]:
        """Активные записи, начинающиеся не раньше ``since`` (для синхронизации с CRM)."""
        _require_aware(since, "since")
        stmt = (
            select(Appointment)
            .where(
                Appointment.status.in_(ACTIVE_STATUSES),
                Appointment.datetime_start >= since,
            )
            .order_by(Appointment.id.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def set_crm_record_id(self, appointment_id: int, crm_record_id: str | int) -> None:
        """Обновляет ссылку на запись в CRM (например, после переназначения ID)."""
        appointment = await self._session.get(Appointment, appointment_id)
        if appointment is None:
            raise EntityNotFoundError(f"Запись {appointment_id} не найдена.")
        appointment.crm_record_id = str(crm_record_id)
        await self._session.flush()

    # --- чтение -------------------------------------------------------

    async def get(self, appointment_id: int, *, user_id: int | None = None) -> Appointment | None:
        """Запись по ID. Если задан ``user_id``, чужие записи не возвращаются."""
        appointment = await self._session.get(Appointment, appointment_id)
        if appointment is None:
            return None
        if user_id is not None and appointment.user_id != user_id:
            return None
        return appointment

    async def get_by_crm_record_id(
        self,
        crm_record_id: str | int,
        *,
        user_id: int | None = None,
    ) -> Appointment | None:
        """Самая свежая запись с указанным ID в CRM."""
        stmt = select(Appointment).where(Appointment.crm_record_id == str(crm_record_id))
        if user_id is not None:
            stmt = stmt.where(Appointment.user_id == user_id)
        stmt = stmt.order_by(Appointment.id.desc()).limit(1)
        return (await self._session.execute(stmt)).scalars().first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        statuses: Collection[BookingStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
        newest_first: bool = True,
    ) -> list[Appointment]:
        """История записей клиента."""
        stmt = select(Appointment).where(Appointment.user_id == user_id)
        if statuses:
            stmt = stmt.where(Appointment.status.in_(list(statuses)))
        order = Appointment.datetime_start.desc() if newest_first else Appointment.datetime_start.asc()
        stmt = stmt.order_by(order, Appointment.id.desc()).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_upcoming_for_user(
        self,
        user_id: int,
        *,
        now: dt.datetime | None = None,
        limit: int = 20,
    ) -> list[Appointment]:
        """Предстоящие активные записи клиента, ближайшие первыми."""
        moment = _require_aware(now, "now") if now is not None else utcnow()
        stmt = (
            select(Appointment)
            .where(
                Appointment.user_id == user_id,
                Appointment.status.in_(ACTIVE_STATUSES),
                Appointment.datetime_start > moment,
            )
            .order_by(Appointment.datetime_start.asc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # --- смена статуса ------------------------------------------------

    async def set_status(
        self,
        appointment_id: int,
        new_status: BookingStatus,
        *,
        user_id: int | None = None,
    ) -> Appointment:
        """Меняет статус с проверкой допустимости перехода.

        Повторная установка того же статуса ничего не меняет (идемпотентно).
        Если задан ``user_id``, чужая запись считается ненайденной.
        """
        appointment = await self.get(appointment_id, user_id=user_id)
        if appointment is None:
            raise EntityNotFoundError(f"Запись {appointment_id} не найдена.")

        if appointment.status is new_status:
            return appointment
        if new_status not in _ALLOWED_TRANSITIONS[appointment.status]:
            raise InvalidStatusTransitionError(
                f"Нельзя перевести запись из статуса '{appointment.status.value}' "
                f"в '{new_status.value}'."
            )

        appointment.status = new_status
        await self._session.flush()
        return appointment

    async def confirm(self, appointment_id: int, *, user_id: int | None = None) -> Appointment:
        """Подтверждает запись."""
        return await self.set_status(appointment_id, BookingStatus.CONFIRMED, user_id=user_id)

    async def cancel(self, appointment_id: int, *, user_id: int | None = None) -> Appointment:
        """Отменяет запись."""
        return await self.set_status(appointment_id, BookingStatus.CANCELLED, user_id=user_id)

    async def complete(self, appointment_id: int, *, user_id: int | None = None) -> Appointment:
        """Отмечает визит завершённым."""
        return await self.set_status(appointment_id, BookingStatus.COMPLETED, user_id=user_id)

    async def cancel_by_crm_record_id(
        self,
        crm_record_id: str | int,
        *,
        user_id: int | None = None,
    ) -> Appointment:
        """Отменяет запись по ID из CRM (например, если её отменили на стороне CRM)."""
        appointment = await self.get_by_crm_record_id(crm_record_id, user_id=user_id)
        if appointment is None:
            raise EntityNotFoundError(f"Запись CRM #{crm_record_id} не найдена.")
        return await self.set_status(appointment.id, BookingStatus.CANCELLED)

    # --- напоминания и автозавершение ---------------------------------

    async def get_due_for_reminder(
        self,
        kind: ReminderKind,
        *,
        lead_time: dt.timedelta,
        min_remaining: dt.timedelta = dt.timedelta(0),
        now: dt.datetime | None = None,
        limit: int = 200,
    ) -> list[Appointment]:
        """Активные записи, которым пора отправить напоминание ``kind``.

        Запись подходит, если она в статусе created / confirmed, напоминание этого типа
        ещё не отправлялось и до начала визита осталось не больше ``lead_time``,
        но больше ``min_remaining``. Нижняя граница нужна, чтобы не слать «напоминание
        за сутки» тому, кто записался на ближайший час.

        Типичный цикл планировщика::

            due = await repo.get_due_for_reminder(ReminderKind.H24, lead_time=timedelta(hours=24))
            ...отправить сообщения...
            await repo.mark_reminder_sent(ReminderKind.H24, [a.id for a in due])
        """
        moment = _require_aware(now, "now") if now is not None else utcnow()
        sent_column = _REMINDER_COLUMNS[kind]
        stmt = (
            select(Appointment)
            .where(
                Appointment.status.in_(ACTIVE_STATUSES),
                sent_column.is_(None),
                Appointment.datetime_start > moment + min_remaining,
                Appointment.datetime_start <= moment + lead_time,
            )
            .order_by(Appointment.datetime_start.asc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def mark_reminder_sent(
        self,
        kind: ReminderKind,
        appointment_ids: Collection[int],
        *,
        sent_at: dt.datetime | None = None,
    ) -> int:
        """Помечает напоминание ``kind`` отправленным. Возвращает число обновлённых записей."""
        if not appointment_ids:
            return 0
        moment = _require_aware(sent_at, "sent_at") if sent_at is not None else utcnow()
        column_name = _REMINDER_COLUMNS[kind].key
        result = await self._session.execute(
            update(Appointment)
            .where(Appointment.id.in_(list(appointment_ids)))
            .values({column_name: moment})
        )
        return int(result.rowcount or 0)

    async def list_between(
        self,
        start: dt.datetime,
        end: dt.datetime,
        *,
        statuses: Collection[BookingStatus] = ACTIVE_STATUSES,
    ) -> list[Appointment]:
        """Записи с началом в интервале ``[start, end)`` (по умолчанию только активные).

        Используется утренним дайджестом: ``start`` и ``end`` — границы текущего дня клуба.
        """
        _require_aware(start, "start")
        _require_aware(end, "end")
        stmt = (
            select(Appointment)
            .where(
                Appointment.status.in_(list(statuses)),
                Appointment.datetime_start >= start,
                Appointment.datetime_start < end,
            )
            .order_by(Appointment.datetime_start.asc(), Appointment.id.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_last_for_user(self, user_id: int) -> Appointment | None:
        """Последняя созданная запись клиента (в любом статусе) — для debug-команд."""
        stmt = (
            select(Appointment)
            .where(Appointment.user_id == user_id)
            .order_by(Appointment.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def complete_finished(
        self,
        *,
        grace: dt.timedelta = dt.timedelta(hours=1),
        now: dt.datetime | None = None,
    ) -> list[Appointment]:
        """Переводит подтверждённые записи, начавшиеся более ``grace`` назад, в «completed».

        Возвращает завершённые записи — по ним можно запросить отзыв у клиента.
        Неподтверждённые (``created``) записи не трогаются.
        """
        moment = _require_aware(now, "now") if now is not None else utcnow()
        stmt = (
            select(Appointment)
            .where(
                Appointment.status == BookingStatus.CONFIRMED,
                Appointment.datetime_start <= moment - grace,
            )
            .order_by(Appointment.datetime_start.asc())
        )
        finished = list((await self._session.execute(stmt)).scalars().all())
        for appointment in finished:
            appointment.status = BookingStatus.COMPLETED
        if finished:
            await self._session.flush()
        return finished


# ----------------------------------------------------------------------
# Отзывы
# ----------------------------------------------------------------------


class ReviewRepository:
    """Отзывы о завершённых визитах."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        appointment_id: int,
        user_id: int,
        rating: int,
        feedback_text: str | None = None,
    ) -> Review:
        """Сохраняет отзыв. Разрешён только владельцу завершённой записи, один раз."""
        if not 1 <= rating <= 5:
            raise ValueError("Оценка должна быть от 1 до 5.")

        appointment = await self._session.get(Appointment, appointment_id)
        if appointment is None or appointment.user_id != user_id:
            raise ReviewNotAllowedError("Запись не найдена.")
        if appointment.status is not BookingStatus.COMPLETED:
            raise ReviewNotAllowedError("Отзыв можно оставить только после завершённого визита.")
        if await self.get_by_appointment(appointment_id) is not None:
            raise ReviewNotAllowedError("Отзыв на этот визит уже оставлен.")

        text = (feedback_text or "").strip()
        review = Review(
            appointment_id=appointment_id,
            user_id=user_id,
            rating=rating,
            feedback_text=text[:2000] or None,
        )
        self._session.add(review)
        await self._session.flush()
        return review

    async def get_by_appointment(self, appointment_id: int) -> Review | None:
        """Отзыв на конкретную запись."""
        stmt = select(Review).where(Review.appointment_id == appointment_id)
        return (await self._session.execute(stmt)).scalars().first()

    async def list_for_user(self, user_id: int, *, limit: int = 50) -> list[Review]:
        """Отзывы клиента, новые первыми."""
        stmt = (
            select(Review)
            .where(Review.user_id == user_id)
            .order_by(Review.created_at.desc(), Review.id.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())
