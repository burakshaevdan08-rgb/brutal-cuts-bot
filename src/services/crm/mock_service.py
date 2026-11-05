"""Автономная in-memory реализация CRM для режима USE_MOCK_CRM=True.

Что умеет Mock-движок:

* 2 филиала, 12 барберов (по 6 на филиал) с грейдами Chef / Pro / Junior;
* прайс с ценами по грейдам и допами;
* динамическое расписание на ближайшие дни, 10:00–21:00 с шагом в час:
  - у барберов реальные графики смен (5/2 на Белорусской, 2/2 на Бауманской),
    так что одновременно работает не больше барберов, чем кресел в филиале;
  - часть окон уже занята «другими клиентами» — детерминированно
    (один и тот же день выглядит одинаково при каждом запросе), причём
    Chef загружены сильнее Junior, а выходные — сильнее будней;
  - записи, созданные через бота, блокируют слоты и освобождают их при отмене;
* многочасовые услуги (комплекс — 2 часа) требуют двух свободных часов подряд;
* программа лояльности по номеру телефона (+ демо-номера).

Состояние (записи и счётчик ID) хранится в памяти и, если задан ``state_path``,
сохраняется в JSON-файл после каждого изменения и загружается при старте —
перезапуск бота не освобождает занятые слоты и не сбрасывает нумерацию.
Дополнительно ``import_appointment`` позволяет подтянуть записи из локальной БД
(см. ``mock_sync.sync_mock_crm_with_database``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import math
import os
import random
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final
from zoneinfo import ZoneInfo

from src.services.crm.base import (
    Appointment,
    AppointmentStatus,
    BaseCRMService,
    BookingData,
    Branch,
    CRMNotFoundError,
    CRMValidationError,
    Grade,
    LoyaltyBalance,
    Service,
    ServiceKind,
    SlotUnavailableError,
    Staff,
)

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Константы расписания
# ----------------------------------------------------------------------

WORK_START_HOUR: Final[int] = 10
WORK_END_HOUR: Final[int] = 21  # последнее окно начинается в 20:00 и заканчивается в 21:00
SLOT_STEP_MINUTES: Final[int] = 60
MIN_LEAD_TIME: Final[dt.timedelta] = dt.timedelta(hours=1)
MAX_BOOKING_HORIZON_DAYS: Final[int] = 60
FIRST_RECORD_ID: Final[int] = 48_001
STATE_FILE_VERSION: Final[int] = 1
# Записи старше этого срока не загружаются из файла состояния (счётчик ID сохраняется).
STATE_RETENTION: Final[dt.timedelta] = dt.timedelta(days=7)

# Базовая загрузка мастера «чужими» записями по грейдам (доля занятых часов).
_GRADE_LOAD: Final[dict[Grade, float]] = {
    Grade.CHEF: 0.72,
    Grade.PRO: 0.55,
    Grade.JUNIOR: 0.35,
}

# ----------------------------------------------------------------------
# Справочные данные
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ShiftPattern:
    """График смен «N через M», сдвинутый на ``offset`` дней."""

    work_days: int
    off_days: int
    offset: int

    def works_on(self, day: dt.date) -> bool:
        cycle = self.work_days + self.off_days
        return (day.toordinal() - self.offset) % cycle < self.work_days


# Тип графика по филиалам: (рабочих дней, выходных дней).
# Белорусская (5 кресел): 5/2 — одновременно работает максимум 5 из 6 барберов.
# Бауманская (4 кресла): 2/2 — одновременно работает максимум 4 из 6 барберов.
_BRANCH_SHIFTS: Final[dict[int, tuple[int, int]]] = {
    1: (5, 2),
    2: (2, 2),
}

_BRANCH_ROWS: Final[tuple[Branch, ...]] = (
    Branch(
        id=1,
        title="BRUTAL CUTS · Белорусская",
        metro="Белорусская",
        address="Ленинградский пр-т",
        chairs=5,
        phone="+7 (495) 000-10-01",
    ),
    Branch(
        id=2,
        title="BRUTAL CUTS · Бауманская",
        metro="Бауманская",
        address="ул. Ладожская",
        chairs=4,
        phone="+7 (495) 000-10-02",
    ),
)

# (id, branch_id, имя, фамилия, грейд, стаж (лет), рейтинг, slug для ссылки, сдвиг смены, описание)
_STAFF_ROWS: Final[tuple[tuple[int, int, str, str, Grade, int, float, str, int, str], ...]] = (
    # --- Белорусская ------------------------------------------------
    (1, 1, "Марк", "Левин", Grade.CHEF, 11, 4.9, "mark-belorusskaya", 0,
     "Шеф-барбер и лицо клуба. Классика, жёсткие фейды и сложные силуэты."),
    (2, 1, "Артем", "Громов", Grade.CHEF, 9, 4.9, "artem-belorusskaya", 3,
     "Шеф-барбер. Работает с текстурой и объёмом, мастер горячего бритья."),
    (3, 1, "Богдан", "Сафронов", Grade.PRO, 6, 4.8, "bogdan-belorusskaya", 1,
     "Уверенные деловые и спортивные стрижки, чёткие контуры."),
    (4, 1, "Денис", "Ковалёв", Grade.PRO, 5, 4.8, "denis-belorusskaya", 4,
     "Специалист по бородам: подбирает форму под тип лица."),
    (5, 1, "Илья", "Нестеров", Grade.JUNIOR, 2, 4.7, "ilya-belorusskaya", 2,
     "Аккуратные базовые стрижки, всегда укладывается в время."),
    (6, 1, "Влад", "Тихонов", Grade.JUNIOR, 1, 4.6, "vlad-belorusskaya", 5,
     "Выпускник академии клуба. Чистая работа машинкой и ножницами."),
    # --- Бауманская -------------------------------------------------
    (7, 2, "Марк", "Орлов", Grade.CHEF, 10, 4.9, "mark-baumanskaya", 0,
     "Шеф-барбер. Скин-фейды, кроп и авторские стрижки по фото."),
    (8, 2, "Артем", "Власов", Grade.CHEF, 8, 4.9, "artem-baumanskaya", 2,
     "Шеф-барбер. Длинные волосы, укладка и уход за бородой."),
    (9, 2, "Богдан", "Крылов", Grade.PRO, 5, 4.8, "bogdan-baumanskaya", 1,
     "Чёткие классические стрижки, работа опасной бритвой."),
    (10, 2, "Денис", "Мартынов", Grade.PRO, 4, 4.8, "denis-baumanskaya", 3,
     "Современные стрижки с текстурой, комплексные услуги."),
    (11, 2, "Илья", "Фролов", Grade.JUNIOR, 2, 4.7, "ilya-baumanskaya", 0,
     "Базовые мужские стрижки, внимательно слушает пожелания."),
    (12, 2, "Влад", "Беляев", Grade.JUNIOR, 1, 4.6, "vlad-baumanskaya", 1,
     "Растёт в профессии: аккуратные стрижки и оформление бороды."),
)


def _flat(price: int) -> MappingProxyType[Grade, int]:
    """Одинаковая цена для всех грейдов (для дополнительных опций)."""
    return MappingProxyType({grade: price for grade in Grade})


def _graded(junior: int, pro: int, chef: int) -> MappingProxyType[Grade, int]:
    """Цены по грейдам барбера."""
    return MappingProxyType({Grade.JUNIOR: junior, Grade.PRO: pro, Grade.CHEF: chef})


_SERVICE_ROWS: Final[tuple[Service, ...]] = (
    Service(
        id=1,
        kind=ServiceKind.HAIRCUT,
        title="Мужская стрижка",
        description="Консультация, мытьё головы, стрижка и укладка.",
        duration_minutes=60,
        prices=_graded(junior=1800, pro=2200, chef=2800),
    ),
    Service(
        id=2,
        kind=ServiceKind.BEARD,
        title="Моделирование бороды",
        description="Подбор формы, окантовка, уход маслом и горячее полотенце.",
        duration_minutes=45,
        prices=_graded(junior=1200, pro=1400, chef=1600),
    ),
    Service(
        id=3,
        kind=ServiceKind.COMPLEX,
        title="Комплекс: стрижка + борода",
        description="Полный образ: стрижка, моделирование бороды и укладка.",
        duration_minutes=120,
        prices=_graded(junior=2800, pro=3300, chef=3800),
    ),
    Service(
        id=11,
        kind=ServiceKind.ADDON,
        title="Патчи",
        description="Патчи под глаза во время процедуры.",
        duration_minutes=0,
        prices=_flat(300),
    ),
    Service(
        id=12,
        kind=ServiceKind.ADDON,
        title="Воск",
        description="Коррекция волос воском: нос и уши.",
        duration_minutes=0,
        prices=_flat(400),
    ),
    Service(
        id=13,
        kind=ServiceKind.ADDON,
        title="Маска-детокс",
        description="Очищающая маска для лица после бритья.",
        duration_minutes=0,
        prices=_flat(700),
    ),
)

# Демо-номера для презентации программы лояльности: (визиты, баллы).
_DEMO_LOYALTY: Final[dict[str, tuple[int, int]]] = {
    "+79990000001": (0, 0),      # новый клиент
    "+79990000002": (8, 1240),   # постоянный клиент
    "+79990000003": (27, 4975),  # легенда клуба
}


# ----------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------


def normalize_phone(raw: str) -> str:
    """Приводит российский номер к формату ``+7XXXXXXXXXX``."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) != 11 or digits[0] != "7":
        raise CRMValidationError(
            "Некорректный номер телефона. Укажите номер в формате +7 999 123-45-67."
        )
    return "+" + digits


def _loyalty_tier(visits: int) -> tuple[str, int]:
    """Название уровня и скидка в процентах по числу визитов."""
    if visits >= 15:
        return "Легенда", 10
    if visits >= 5:
        return "Свой", 5
    return "Новичок", 0


def _appointment_to_dict(appointment: Appointment) -> dict[str, Any]:
    return {
        "id": appointment.id,
        "status": appointment.status.value,
        "branch_id": appointment.branch_id,
        "staff_id": appointment.staff_id,
        "service_id": appointment.service_id,
        "addon_ids": list(appointment.addon_ids),
        "starts_at": appointment.starts_at.isoformat(),
        "ends_at": appointment.ends_at.isoformat(),
        "client_name": appointment.client_name,
        "client_phone": appointment.client_phone,
        "total_price": appointment.total_price,
        "telegram_id": appointment.telegram_id,
        "comment": appointment.comment,
        "created_at": appointment.created_at.isoformat(),
    }


def _appointment_from_dict(raw: dict[str, Any]) -> Appointment:
    return Appointment(
        id=int(raw["id"]),
        status=AppointmentStatus(raw["status"]),
        branch_id=int(raw["branch_id"]),
        staff_id=int(raw["staff_id"]),
        service_id=int(raw["service_id"]),
        addon_ids=tuple(int(item) for item in raw.get("addon_ids", [])),
        starts_at=dt.datetime.fromisoformat(raw["starts_at"]),
        ends_at=dt.datetime.fromisoformat(raw["ends_at"]),
        client_name=str(raw.get("client_name", "")),
        client_phone=str(raw.get("client_phone", "")),
        total_price=int(raw["total_price"]),
        telegram_id=raw.get("telegram_id"),
        comment=str(raw.get("comment", "")),
        created_at=dt.datetime.fromisoformat(raw["created_at"]),
    )


# ----------------------------------------------------------------------
# Mock-движок
# ----------------------------------------------------------------------


class MockCRMService(BaseCRMService):
    """In-memory CRM без обращений к внешним API."""

    is_mock = True

    def __init__(
        self,
        *,
        timezone: str = "Europe/Moscow",
        latency_ms: int = 0,
        now_provider: Callable[[], dt.datetime] | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        """
        :param timezone: часовой пояс клуба (IANA).
        :param latency_ms: средняя имитируемая сетевая задержка каждого вызова.
        :param now_provider: подмена «текущего времени» (для тестов и демо).
        :param state_path: JSON-файл, в котором сохраняются записи и счётчик ID.
            ``None`` — состояние живёт только в памяти процесса.
        """
        self._tz = ZoneInfo(timezone)
        self._latency_ms = max(latency_ms, 0)
        self._now_provider = now_provider

        self._lock = asyncio.Lock()
        self._next_id = FIRST_RECORD_ID
        self._state_path = Path(state_path) if state_path else None
        self._appointments: dict[int, Appointment] = {}

        self._branches: dict[int, Branch] = {branch.id: branch for branch in _BRANCH_ROWS}
        self._services: dict[int, Service] = {service.id: service for service in _SERVICE_ROWS}
        self._staff: dict[int, Staff] = {}
        self._shifts: dict[int, _ShiftPattern] = {}
        self._load_staff()
        self._load_state()

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------

    async def get_branches(self) -> list[Branch]:
        await self._simulate_latency()
        return list(self._branches.values())

    async def get_services(self, branch_id: int) -> list[Service]:
        await self._simulate_latency()
        self._get_branch(branch_id)
        return list(self._services.values())

    async def get_staff(self, branch_id: int) -> list[Staff]:
        await self._simulate_latency()
        self._get_branch(branch_id)
        return [member for member in self._staff.values() if member.branch_id == branch_id]

    async def get_available_dates(
        self,
        branch_id: int,
        staff_id: int,
        service_id: int,
        days: int = 14,
    ) -> list[dt.date]:
        await self._simulate_latency()
        self._get_branch(branch_id)
        staff = self._get_staff_member(branch_id, staff_id)
        service = self._get_service(service_id)
        if not 1 <= days <= MAX_BOOKING_HORIZON_DAYS:
            raise CRMValidationError(
                f"Количество дней должно быть от 1 до {MAX_BOOKING_HORIZON_DAYS}."
            )

        slots = self._slots_needed(service.duration_minutes)
        today = self._today()
        result: list[dt.date] = []
        for offset in range(days):
            day = today + dt.timedelta(days=offset)
            if self._free_start_hours(staff, day, slots):
                result.append(day)
        return result

    async def get_available_times(
        self,
        branch_id: int,
        staff_id: int,
        date: dt.date,
        service_id: int | None = None,
    ) -> list[dt.time]:
        await self._simulate_latency()
        self._get_branch(branch_id)
        staff = self._get_staff_member(branch_id, staff_id)
        slots = 1
        if service_id is not None:
            slots = self._slots_needed(self._get_service(service_id).duration_minutes)

        today = self._today()
        if date < today or date >= today + dt.timedelta(days=MAX_BOOKING_HORIZON_DAYS):
            return []
        return [dt.time(hour) for hour in self._free_start_hours(staff, date, slots)]

    async def create_appointment(self, booking_data: BookingData) -> Appointment:
        await self._simulate_latency()

        self._get_branch(booking_data.branch_id)
        staff = self._get_staff_member(booking_data.branch_id, booking_data.staff_id)
        service = self._get_service(booking_data.service_id)
        addons = self._resolve_addons(booking_data.addon_ids)

        client_name = booking_data.client_name.strip()
        if not 2 <= len(client_name) <= 64:
            raise CRMValidationError("Имя клиента должно содержать от 2 до 64 символов.")
        client_phone = normalize_phone(booking_data.client_phone)

        start_time = booking_data.time
        if start_time.minute or start_time.second or start_time.microsecond:
            raise CRMValidationError("Запись возможна только на начало часа, например 14:00.")
        if not WORK_START_HOUR <= start_time.hour < WORK_END_HOUR:
            raise CRMValidationError(
                f"Клуб работает с {WORK_START_HOUR:02d}:00 до {WORK_END_HOUR:02d}:00."
            )

        today = self._today()
        if booking_data.date >= today + dt.timedelta(days=MAX_BOOKING_HORIZON_DAYS):
            raise CRMValidationError(
                f"Запись доступна не более чем на {MAX_BOOKING_HORIZON_DAYS} дней вперёд."
            )

        slots = self._slots_needed(service.duration_minutes)

        async with self._lock:
            if start_time.hour not in self._free_start_hours(staff, booking_data.date, slots):
                raise SlotUnavailableError(
                    f"Время {booking_data.date:%d.%m.%Y} {start_time:%H:%M} "
                    f"у мастера {staff.first_name} недоступно. Выберите другое окно."
                )

            starts_at = dt.datetime.combine(booking_data.date, start_time, tzinfo=self._tz)
            ends_at = starts_at + dt.timedelta(minutes=service.duration_minutes)
            total_price = service.price_for(staff.grade) + sum(
                addon.price_for(staff.grade) for addon in addons
            )

            appointment = Appointment(
                id=self._allocate_id(),
                status=AppointmentStatus.PENDING,
                branch_id=booking_data.branch_id,
                staff_id=booking_data.staff_id,
                service_id=booking_data.service_id,
                addon_ids=tuple(addon.id for addon in addons),
                starts_at=starts_at,
                ends_at=ends_at,
                client_name=client_name,
                client_phone=client_phone,
                total_price=total_price,
                telegram_id=booking_data.telegram_id,
                comment=booking_data.comment.strip()[:500],
                created_at=self._now(),
            )
            self._appointments[appointment.id] = appointment
            await self._persist()

        logger.info(
            "Mock CRM: создана запись #%s (%s, %s, %s ₽)",
            appointment.id,
            staff.full_name,
            starts_at.isoformat(timespec="minutes"),
            total_price,
        )
        return appointment

    async def cancel_appointment(self, record_id: int) -> Appointment:
        await self._simulate_latency()
        async with self._lock:
            appointment = self._get_appointment(record_id)
            if appointment.status is AppointmentStatus.CANCELLED:
                return appointment
            if appointment.starts_at <= self._now():
                raise CRMValidationError("Нельзя отменить запись, которая уже началась.")
            cancelled = replace(appointment, status=AppointmentStatus.CANCELLED)
            self._appointments[record_id] = cancelled
            await self._persist()

        logger.info("Mock CRM: запись #%s отменена", record_id)
        return cancelled

    async def confirm_appointment(self, record_id: int) -> Appointment:
        await self._simulate_latency()
        async with self._lock:
            appointment = self._get_appointment(record_id)
            if appointment.status is AppointmentStatus.CANCELLED:
                raise CRMValidationError("Отменённую запись нельзя подтвердить.")
            if appointment.status is AppointmentStatus.CONFIRMED:
                return appointment
            confirmed = replace(appointment, status=AppointmentStatus.CONFIRMED)
            self._appointments[record_id] = confirmed
            await self._persist()

        logger.info("Mock CRM: запись #%s подтверждена", record_id)
        return confirmed

    async def get_client_loyalty_balance(self, phone: str) -> LoyaltyBalance:
        await self._simulate_latency()
        normalized = normalize_phone(phone)

        if normalized in _DEMO_LOYALTY:
            visits, points = _DEMO_LOYALTY[normalized]
        else:
            # Для остальных номеров значения детерминированы: один и тот же
            # номер всегда возвращает один и тот же баланс.
            digest = hashlib.sha256(normalized.encode("utf-8")).digest()
            visits = digest[0] % 31
            points = visits * 120 + (digest[1] % 12) * 25

        tier, discount = _loyalty_tier(visits)
        return LoyaltyBalance(
            phone=normalized,
            points=points,
            visits=visits,
            tier=tier,
            discount_percent=discount,
        )

    # ------------------------------------------------------------------
    # Импорт записей из внешнего источника (локальная БД)
    # ------------------------------------------------------------------

    @property
    def timezone(self) -> ZoneInfo:
        """Часовой пояс клуба."""
        return self._tz

    async def import_appointment(self, appointment: Appointment) -> tuple[Appointment, str]:
        """Добавляет в Mock-CRM существующую запись (например, из локальной БД).

        Возвращает ``(запись, результат)``, где результат:

        * ``"exists"``     — такая запись (тот же ID, мастер и время) уже есть;
        * ``"imported"``   — запись добавлена под своим ID;
        * ``"reassigned"`` — ID уже занят другой записью (или некорректен), выдан новый:
          вызывающий код должен обновить ссылку на запись у себя;
        * ``"conflict"``   — слот мастера уже занят другой активной записью, запись не добавлена.

        Счётчик ID автоматически сдвигается за максимальный импортированный ID.
        """
        starts_at = appointment.starts_at.astimezone(self._tz)
        ends_at = appointment.ends_at.astimezone(self._tz)
        candidate = replace(appointment, starts_at=starts_at, ends_at=ends_at)

        async with self._lock:
            existing = self._appointments.get(candidate.id)
            if (
                existing is not None
                and existing.staff_id == candidate.staff_id
                and existing.starts_at == candidate.starts_at
            ):
                return existing, "exists"

            if candidate.is_active and self._overlaps_active(candidate):
                return candidate, "conflict"

            outcome = "imported"
            if candidate.id <= 0 or candidate.id in self._appointments:
                candidate = replace(candidate, id=self._allocate_id())
                outcome = "reassigned"

            self._appointments[candidate.id] = candidate
            self._next_id = max(self._next_id, candidate.id + 1)
            await self._persist()
        return candidate, outcome

    def _occupied_until(self, appointment: Appointment) -> dt.datetime:
        """Конец занятых часовых окон записи (с округлением вверх до часа)."""
        minutes = int((appointment.ends_at - appointment.starts_at).total_seconds() // 60)
        slots = self._slots_needed(minutes)
        return appointment.starts_at + dt.timedelta(minutes=slots * SLOT_STEP_MINUTES)

    def _overlaps_active(self, candidate: Appointment) -> bool:
        candidate_end = self._occupied_until(candidate)
        for other in self._appointments.values():
            if not other.is_active or other.staff_id != candidate.staff_id:
                continue
            if other.starts_at < candidate_end and self._occupied_until(other) > candidate.starts_at:
                return True
        return False

    # ------------------------------------------------------------------
    # Сохранение состояния
    # ------------------------------------------------------------------

    def _allocate_id(self) -> int:
        record_id = self._next_id
        self._next_id += 1
        return record_id

    def _load_state(self) -> None:
        """Загружает записи и счётчик ID из файла состояния (если он есть)."""
        path = self._state_path
        if path is None or not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            records = [_appointment_from_dict(item) for item in raw["appointments"]]
            saved_next_id = int(raw.get("next_id", FIRST_RECORD_ID))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            backup = path.with_suffix(path.suffix + ".corrupt")
            logger.error("Файл состояния Mock-CRM повреждён (%s), он переименован в %s", exc, backup)
            with suppress(OSError):
                os.replace(path, backup)
            return

        horizon = self._now() - STATE_RETENTION
        self._appointments = {
            record.id: record for record in records if record.ends_at.astimezone(self._tz) >= horizon
        }
        highest = max((record.id for record in records), default=FIRST_RECORD_ID - 1)
        self._next_id = max(saved_next_id, highest + 1, FIRST_RECORD_ID)
        logger.info(
            "Mock CRM: загружено записей: %s, следующий ID: %s (%s)",
            len(self._appointments),
            self._next_id,
            path,
        )

    async def _persist(self) -> None:
        """Атомарно записывает состояние в файл. Ошибка записи не ломает бронирование."""
        if self._state_path is None:
            return
        payload = json.dumps(
            {
                "version": STATE_FILE_VERSION,
                "next_id": self._next_id,
                "appointments": [_appointment_to_dict(item) for item in self._appointments.values()],
            },
            ensure_ascii=False,
            indent=2,
        )
        try:
            await asyncio.to_thread(self._write_state_file, self._state_path, payload)
        except OSError:
            logger.exception("Не удалось сохранить состояние Mock-CRM в %s", self._state_path)

    @staticmethod
    def _write_state_file(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, path)  # атомарная замена: файл не бывает записан наполовину

    # ------------------------------------------------------------------
    # Инициализация справочников
    # ------------------------------------------------------------------

    def _load_staff(self) -> None:
        for (
            staff_id,
            branch_id,
            first_name,
            last_name,
            grade,
            experience,
            rating,
            slug,
            shift_offset,
            description,
        ) in _STAFF_ROWS:
            self._staff[staff_id] = Staff(
                id=staff_id,
                branch_id=branch_id,
                first_name=first_name,
                last_name=last_name,
                grade=grade,
                experience_years=experience,
                description=description,
                netmonet_url=f"https://netmonet.co/tip/brutalcuts-{slug}",
                rating=rating,
            )
            work_days, off_days = _BRANCH_SHIFTS[branch_id]
            self._shifts[staff_id] = _ShiftPattern(
                work_days=work_days,
                off_days=off_days,
                offset=shift_offset,
            )

    # ------------------------------------------------------------------
    # Время
    # ------------------------------------------------------------------

    def _now(self) -> dt.datetime:
        if self._now_provider is None:
            return dt.datetime.now(self._tz)
        value = self._now_provider()
        if value.tzinfo is None:
            return value.replace(tzinfo=self._tz)
        return value.astimezone(self._tz)

    def _today(self) -> dt.date:
        return self._now().date()

    async def _simulate_latency(self) -> None:
        if self._latency_ms <= 0:
            return
        delay = self._latency_ms * random.uniform(0.5, 1.5) / 1000
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Поиск сущностей
    # ------------------------------------------------------------------

    def _get_branch(self, branch_id: int) -> Branch:
        branch = self._branches.get(branch_id)
        if branch is None:
            raise CRMNotFoundError(f"Филиал {branch_id} не найден.")
        return branch

    def _get_staff_member(self, branch_id: int, staff_id: int) -> Staff:
        member = self._staff.get(staff_id)
        if member is None or member.branch_id != branch_id:
            raise CRMNotFoundError(f"Мастер {staff_id} не найден в филиале {branch_id}.")
        return member

    def _get_service(self, service_id: int) -> Service:
        """Основная услуга (не допы) по ID."""
        service = self._services.get(service_id)
        if service is None:
            raise CRMNotFoundError(f"Услуга {service_id} не найдена.")
        if service.is_addon:
            raise CRMValidationError(
                f"«{service.title}» — дополнительная опция, её нельзя выбрать как основную услугу."
            )
        return service

    def _resolve_addons(self, addon_ids: tuple[int, ...]) -> list[Service]:
        if len(set(addon_ids)) != len(addon_ids):
            raise CRMValidationError("Дополнительные опции не должны повторяться.")
        addons: list[Service] = []
        for addon_id in addon_ids:
            addon = self._services.get(addon_id)
            if addon is None:
                raise CRMNotFoundError(f"Дополнительная опция {addon_id} не найдена.")
            if not addon.is_addon:
                raise CRMValidationError(f"«{addon.title}» не является дополнительной опцией.")
            addons.append(addon)
        return addons

    def _get_appointment(self, record_id: int) -> Appointment:
        appointment = self._appointments.get(record_id)
        if appointment is None:
            raise CRMNotFoundError(f"Запись {record_id} не найдена.")
        return appointment

    # ------------------------------------------------------------------
    # Генератор расписания
    # ------------------------------------------------------------------

    @staticmethod
    def _slots_needed(duration_minutes: int) -> int:
        """Сколько часовых окон подряд занимает услуга."""
        return max(1, math.ceil(duration_minutes / SLOT_STEP_MINUTES))

    def _baseline_busy_hours(self, staff: Staff, day: dt.date) -> set[int]:
        """Часы, уже занятые «чужими» записями.

        Генерация детерминирована: seed зависит от мастера и даты, поэтому
        повторный запрос на тот же день возвращает то же расписание.
        Чем дальше день, тем свободнее мастер; в выходные загрузка выше.
        """
        rng = random.Random(f"brutal-cuts:{staff.id}:{day.isoformat()}")
        days_ahead = max((day - self._today()).days, 0)
        load = _GRADE_LOAD[staff.grade] * max(0.45, 1 - 0.035 * days_ahead)
        if day.weekday() >= 5:
            load = min(0.92, load * 1.2)
        return {hour for hour in range(WORK_START_HOUR, WORK_END_HOUR) if rng.random() < load}

    def _booked_hours(self, staff_id: int, day: dt.date) -> set[int]:
        """Часы, занятые записями, созданными через бота."""
        hours: set[int] = set()
        for appointment in self._appointments.values():
            if not appointment.is_active or appointment.staff_id != staff_id:
                continue
            if appointment.starts_at.date() != day:
                continue
            minutes = int((appointment.ends_at - appointment.starts_at).total_seconds() // 60)
            slots = self._slots_needed(minutes)
            hours.update(range(appointment.starts_at.hour, appointment.starts_at.hour + slots))
        return hours

    def _free_hours(self, staff: Staff, day: dt.date) -> set[int]:
        """Свободные часовые окна мастера на день."""
        if not self._shifts[staff.id].works_on(day):
            return set()

        busy = self._baseline_busy_hours(staff, day) | self._booked_hours(staff.id, day)
        earliest_start = self._now() + MIN_LEAD_TIME

        free: set[int] = set()
        for hour in range(WORK_START_HOUR, WORK_END_HOUR):
            slot_start = dt.datetime.combine(day, dt.time(hour), tzinfo=self._tz)
            if slot_start < earliest_start or hour in busy:
                continue
            free.add(hour)
        return free

    def _free_start_hours(self, staff: Staff, day: dt.date, slots: int) -> list[int]:
        """Часы начала, с которых услуга на ``slots`` окон помещается целиком."""
        free = self._free_hours(staff, day)
        return [
            hour
            for hour in sorted(free)
            if all((hour + shift) in free for shift in range(slots))
        ]
