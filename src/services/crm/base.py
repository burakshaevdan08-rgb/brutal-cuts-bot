"""Абстрактный интерфейс CRM-слоя BRUTAL CUTS.

Бот общается с CRM только через ``BaseCRMService``. Благодаря этому
реализацию можно подменять без изменений в хендлерах:

* ``MockCRMService``    — автономный in-memory движок (USE_MOCK_CRM=True);
* реализация YClients   — реальный API (USE_MOCK_CRM=False).

В этом же модуле лежат неизменяемые DTO (dataclass) и исключения CRM-слоя.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

# ----------------------------------------------------------------------
# Исключения
# ----------------------------------------------------------------------


class CRMError(Exception):
    """Базовое исключение CRM-слоя."""


class CRMNotFoundError(CRMError):
    """Запрошенная сущность (филиал, мастер, услуга, запись) не найдена."""


class CRMValidationError(CRMError):
    """Переданы некорректные данные или недопустимое действие над записью."""


class SlotUnavailableError(CRMError):
    """Выбранное время недоступно: занято, выходной у мастера или уже прошло."""


# ----------------------------------------------------------------------
# Перечисления
# ----------------------------------------------------------------------


class Grade(StrEnum):
    """Грейд барбера. От него зависит цена основных услуг."""

    CHEF = "chef"
    PRO = "pro"
    JUNIOR = "junior"

    @property
    def label(self) -> str:
        return {
            Grade.CHEF: "Chef",
            Grade.PRO: "Pro",
            Grade.JUNIOR: "Junior",
        }[self]


class ServiceKind(StrEnum):
    """Тип услуги."""

    HAIRCUT = "haircut"
    BEARD = "beard"
    COMPLEX = "complex"
    ADDON = "addon"


class AppointmentStatus(StrEnum):
    """Статус записи."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


# ----------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------


def plural_ru(number: int, one: str, few: str, many: str) -> str:
    """Склонение существительного после числительного: 1 год, 2 года, 5 лет."""
    n = abs(number)
    if n % 100 in range(11, 15):
        return many
    if n % 10 == 1:
        return one
    if n % 10 in (2, 3, 4):
        return few
    return many


# ----------------------------------------------------------------------
# DTO
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Branch:
    """Филиал клуба."""

    id: int
    title: str
    metro: str
    address: str
    chairs: int
    phone: str
    work_start: dt.time = dt.time(10, 0)
    work_end: dt.time = dt.time(21, 0)

    @property
    def work_hours_label(self) -> str:
        return f"{self.work_start:%H:%M}–{self.work_end:%H:%M}"


@dataclass(frozen=True, slots=True)
class Staff:
    """Барбер."""

    id: int
    branch_id: int
    first_name: str
    last_name: str
    grade: Grade
    experience_years: int
    description: str
    netmonet_url: str
    rating: float

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def experience_label(self) -> str:
        word = plural_ru(self.experience_years, "год", "года", "лет")
        return f"{self.experience_years} {word}"


@dataclass(frozen=True, slots=True, eq=False)
class Service:
    """Услуга или дополнительная опция.

    Цена основных услуг зависит от грейда барбера (``prices``),
    у дополнительных опций цена одинакова для всех грейдов.
    ``duration_minutes`` у опций равен 0: они выполняются в рамках
    времени основной услуги.
    """

    id: int
    kind: ServiceKind
    title: str
    description: str
    duration_minutes: int
    prices: Mapping[Grade, int]

    @property
    def is_addon(self) -> bool:
        return self.kind is ServiceKind.ADDON

    def price_for(self, grade: Grade) -> int:
        """Цена услуги для барбера указанного грейда, в рублях."""
        return self.prices[grade]

    @property
    def min_price(self) -> int:
        return min(self.prices.values())

    @property
    def max_price(self) -> int:
        return max(self.prices.values())


@dataclass(frozen=True, slots=True)
class BookingData:
    """Данные для создания записи."""

    branch_id: int
    staff_id: int
    service_id: int
    date: dt.date
    time: dt.time
    client_name: str
    client_phone: str
    addon_ids: tuple[int, ...] = ()
    telegram_id: int | None = None
    comment: str = ""


@dataclass(frozen=True, slots=True)
class Appointment:
    """Запись клиента."""

    id: int
    status: AppointmentStatus
    branch_id: int
    staff_id: int
    service_id: int
    addon_ids: tuple[int, ...]
    starts_at: dt.datetime
    ends_at: dt.datetime
    client_name: str
    client_phone: str
    total_price: int
    telegram_id: int | None
    comment: str
    created_at: dt.datetime

    @property
    def is_active(self) -> bool:
        return self.status is not AppointmentStatus.CANCELLED


@dataclass(frozen=True, slots=True)
class LoyaltyBalance:
    """Баланс программы лояльности клиента."""

    phone: str
    points: int
    visits: int
    tier: str
    discount_percent: int


# ----------------------------------------------------------------------
# Интерфейс
# ----------------------------------------------------------------------


class BaseCRMService(ABC):
    """Контракт CRM-сервиса.

    Все методы асинхронные. При ошибках бросают наследников ``CRMError``:
    ``CRMNotFoundError``, ``CRMValidationError``, ``SlotUnavailableError``.
    """

    #: True у Mock-реализации — удобно показывать режим в служебных сообщениях.
    is_mock: ClassVar[bool] = False

    @abstractmethod
    async def get_branches(self) -> list[Branch]:
        """Список филиалов клуба."""

    @abstractmethod
    async def get_services(self, branch_id: int) -> list[Service]:
        """Услуги и дополнительные опции филиала."""

    @abstractmethod
    async def get_staff(self, branch_id: int) -> list[Staff]:
        """Барберы филиала."""

    @abstractmethod
    async def get_available_dates(
        self,
        branch_id: int,
        staff_id: int,
        service_id: int,
        days: int = 14,
    ) -> list[dt.date]:
        """Даты ближайших ``days`` дней, на которые у мастера есть свободное
        время для указанной услуги (начиная с сегодняшнего дня)."""

    @abstractmethod
    async def get_available_times(
        self,
        branch_id: int,
        staff_id: int,
        date: dt.date,
        service_id: int | None = None,
    ) -> list[dt.time]:
        """Время начала свободных окон мастера на указанную дату.

        Если передан ``service_id``, возвращаются только те окна, в которые
        услуга помещается целиком (например, комплекс занимает два часа подряд).
        """

    @abstractmethod
    async def create_appointment(self, booking_data: BookingData) -> Appointment:
        """Создать запись. Новая запись получает статус ``PENDING``."""

    @abstractmethod
    async def cancel_appointment(self, record_id: int) -> Appointment:
        """Отменить запись и освободить слот."""

    @abstractmethod
    async def confirm_appointment(self, record_id: int) -> Appointment:
        """Подтвердить запись (статус ``CONFIRMED``)."""

    @abstractmethod
    async def get_client_loyalty_balance(self, phone: str) -> LoyaltyBalance:
        """Баланс и уровень программы лояльности по номеру телефона."""

    async def close(self) -> None:
        """Освободить ресурсы (HTTP-сессии и т. п.). По умолчанию ничего не делает."""
        return None
