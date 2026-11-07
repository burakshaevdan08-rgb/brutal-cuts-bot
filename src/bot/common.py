"""Общие утилиты Telegram-слоя: форматирование, справочники, фото, показ экранов."""

from __future__ import annotations

import datetime as dt
import html
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeVar
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from src.database.models import Appointment as DbAppointment
from src.database.models import BookingStatus
from src.services.crm import (
    BaseCRMService,
    Branch,
    CRMValidationError,
    Grade,
    Service,
    Staff,
    normalize_phone,
)

if TYPE_CHECKING:
    from src.core.config import Settings

T = TypeVar("T")

Event = CallbackQuery | Message
Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove

# ----------------------------------------------------------------------
# Справочники для оформления
# ----------------------------------------------------------------------

WEEKDAYS_SHORT: Final[tuple[str, ...]] = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_WEEKDAYS_LOWER: Final[tuple[str, ...]] = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS_NOMINATIVE: Final[tuple[str, ...]] = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)
_MONTHS_GENITIVE: Final[tuple[str, ...]] = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

GRADE_ORDER: Final[tuple[Grade, ...]] = (Grade.CHEF, Grade.PRO, Grade.JUNIOR)
GRADE_EMOJI: Final[dict[Grade, str]] = {Grade.CHEF: "👑", Grade.PRO: "🔥", Grade.JUNIOR: "🌱"}
GRADE_TITLE: Final[dict[Grade, str]] = {
    Grade.CHEF: "Шеф-барбер",
    Grade.PRO: "Pro-барбер",
    Grade.JUNIOR: "Junior-барбер",
}
GRADE_TITLE_PLURAL: Final[dict[Grade, str]] = {
    Grade.CHEF: "Шеф-барберы",
    Grade.PRO: "Pro-барберы",
    Grade.JUNIOR: "Junior-барберы",
}

SERVICE_EMOJI: Final[dict[str, str]] = {
    "haircut": "✂️",
    "beard": "🧔",
    "complex": "💎",
    "addon": "➕",
}

STATUS_LABEL: Final[dict[BookingStatus, str]] = {
    BookingStatus.CREATED: "🕓 Ожидает подтверждения",
    BookingStatus.CONFIRMED: "✅ Подтверждена",
    BookingStatus.CANCELLED: "❌ Отменена",
    BookingStatus.COMPLETED: "✔️ Завершена",
}

MAIN_MENU_TEXT: Final[str] = "🏠 <b>Главное меню</b>\nВыберите, что хотите сделать:"

PHONE_PROMPT: Final[str] = (
    "📱 <b>Подтвердите номер телефона</b>\n"
    "Нажмите кнопку «Поделиться контактом» внизу экрана. "
    "Номер нужен для записи и программы лояльности."
)


@dataclass(frozen=True, slots=True)
class Beverage:
    """Напиток барного меню."""

    code: str
    label: str
    emoji: str
    adults_only: bool = False


NO_BEVERAGE_CODE: Final[str] = "none"

BEVERAGES: Final[tuple[Beverage, ...]] = (
    Beverage("americano", "Американо", "☕"),
    Beverage("cappuccino", "Капучино", "☕"),
    Beverage("bourbon", "Шот виски (Бурбон)", "🥃", adults_only=True),
    Beverage("lemonade", "Лимонад", "🍋"),
)
BEVERAGES_BY_CODE: Final[dict[str, Beverage]] = {item.code: item for item in BEVERAGES}


def beverage_label(code: str | None) -> str | None:
    """Название напитка по коду; для «ничего» и неизвестных кодов — None."""
    if code is None or code == NO_BEVERAGE_CODE:
        return None
    item = BEVERAGES_BY_CODE.get(code)
    return item.label if item else None


# ----------------------------------------------------------------------
# Форматирование
# ----------------------------------------------------------------------


def esc(value: object) -> str:
    """HTML-экранирование для parse_mode=HTML."""
    return html.escape(str(value), quote=False)


def format_number(value: int) -> str:
    """1240 → «1 240»."""
    return f"{value:,}".replace(",", " ")


def format_price(amount: int) -> str:
    """1800 → «1 800 ₽»."""
    return f"{format_number(amount)} ₽"


# Имена мастеров из справочника склоняются по таблице, остальные — по простым правилам.
_NAME_GENITIVE: Final[dict[str, str]] = {
    "Марк": "Марка", "Артем": "Артема", "Артём": "Артёма", "Богдан": "Богдана",
    "Денис": "Дениса", "Илья": "Ильи", "Влад": "Влада",
}
_NAME_DATIVE: Final[dict[str, str]] = {
    "Марк": "Марку", "Артем": "Артему", "Артём": "Артёму", "Богдан": "Богдану",
    "Денис": "Денису", "Илья": "Илье", "Влад": "Владу",
}
_CONSONANTS: Final[str] = "бвгджзклмнпрстфхцчшщ"


def name_genitive(name: str) -> str:
    """«Марк» → «Марка» (у мастера Марка)."""
    if name in _NAME_GENITIVE:
        return _NAME_GENITIVE[name]
    last = name[-1:].lower()
    if last in _CONSONANTS:
        return name + "а"
    if last in ("й", "ь"):
        return name[:-1] + "я"
    if last == "я":
        return name[:-1] + "и"
    if last == "а":
        return name[:-1] + "ы"
    return name


def name_dative(name: str) -> str:
    """«Марк» → «Марку» (оставить Марку на чай)."""
    if name in _NAME_DATIVE:
        return _NAME_DATIVE[name]
    last = name[-1:].lower()
    if last in _CONSONANTS:
        return name + "у"
    if last in ("й", "ь"):
        return name[:-1] + "ю"
    if last in ("я", "а"):
        return name[:-1] + "е"
    return name


def visit_phrase(start: dt.datetime, service_name: str, today: dt.date) -> str:
    """«сегодняшняя стрижка» / «вчерашний визит» — для запроса оценки.

    ``start`` — начало визита в часовом поясе клуба.
    """
    day = start.date()
    is_haircut = any(word in service_name.lower() for word in ("стрижк", "комплекс"))
    if day == today:
        adjective = "сегодняшняя" if is_haircut else "сегодняшний"
    elif day == today - dt.timedelta(days=1):
        adjective = "вчерашняя" if is_haircut else "вчерашний"
    else:
        adjective = "недавняя" if is_haircut else "недавний"
    return f"{adjective} {'стрижка' if is_haircut else 'визит'}"


def format_date_ru(day: dt.date) -> str:
    """2026-11-05 → «чт, 5 ноября»."""
    return f"{_WEEKDAYS_LOWER[day.weekday()]}, {day.day} {_MONTHS_GENITIVE[day.month - 1]}"


def format_phone(phone: str) -> str:
    """+79991234567 → «+7 999 123-45-67» (прочие форматы возвращаются как есть)."""
    match = re.fullmatch(r"\+7(\d{3})(\d{3})(\d{2})(\d{2})", phone)
    if match is None:
        return phone
    return f"+7 {match[1]} {match[2]}-{match[3]}-{match[4]}"


def barber_label(grade: Grade, name: str) -> str:
    """«Шеф-барбер Марк 👑»."""
    return f"{GRADE_TITLE[grade]} {esc(name)} {GRADE_EMOJI[grade]}"


def branch_card(branch: Branch, staff: Sequence[Staff] | None = None) -> str:
    """Карточка филиала."""
    lines = [
        f"📍 <b>{esc(branch.title)}</b>",
        f"м. {esc(branch.metro)}, {esc(branch.address)}",
        f"🪑 Кресел: {branch.chairs} · 🕙 {branch.work_hours_label}",
        f"📞 {esc(branch.phone)}",
    ]
    if staff:
        parts = []
        for grade in GRADE_ORDER:
            count = sum(1 for member in staff if member.grade is grade)
            if count:
                parts.append(f"{GRADE_EMOJI[grade]} {count}")
        lines.append(f"💈 Барберов: {len(staff)} ({' · '.join(parts)})")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Расчёт цены
# ----------------------------------------------------------------------


def visit_total(service: Service, grade: Grade, addons: Sequence[Service]) -> int:
    """Итоговая цена визита у барбера указанного грейда с учётом допов."""
    return service.price_for(grade) + sum(addon.price_for(grade) for addon in addons)


def baseline_price(service: Service, staff: Sequence[Staff]) -> int:
    """Базовая («от») цена услуги: у самого доступного грейда в филиале."""
    grades = {member.grade for member in staff}
    if not grades:
        return service.min_price
    return min(service.price_for(grade) for grade in grades)


# ----------------------------------------------------------------------
# Карточка визита
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VisitView:
    """Данные для отрисовки карточки визита (из черновика или из БД)."""

    branch_title: str
    branch_place: str
    staff_name: str
    grade: Grade | None
    service_name: str
    addons: tuple[str, ...]
    start: dt.datetime  # в часовом поясе клуба
    total_price: int
    beverage: str | None
    silent_cut: bool
    record_id: str | None = None
    status: BookingStatus | None = None


def view_from_appointment(
    appointment: DbAppointment,
    branch: Branch | None,
    tz: ZoneInfo,
) -> VisitView:
    """Собирает VisitView из записи БД (время переводится из UTC в пояс клуба)."""
    try:
        grade: Grade | None = Grade(appointment.staff_grade)
    except ValueError:
        grade = None
    if branch is not None:
        title = branch.title
        place = f"{branch.address} (м. {branch.metro})"
    else:
        title = f"Филиал №{appointment.branch_id}"
        place = ""
    return VisitView(
        branch_title=title,
        branch_place=place,
        staff_name=appointment.staff_name,
        grade=grade,
        service_name=appointment.service_name,
        addons=tuple(appointment.addons or ()),
        start=appointment.datetime_start.astimezone(tz),
        total_price=appointment.total_price,
        beverage=appointment.beverage,
        silent_cut=appointment.silent_cut,
        record_id=appointment.crm_record_id,
        status=appointment.status,
    )


def format_visit_card(view: VisitView, *, header: str | None = None, compact: bool = False) -> str:
    """Карточка визита. ``compact=True`` — короткий вариант для списков."""
    when = f"{format_date_ru(view.start.date())} · {view.start:%H:%M}"
    master = barber_label(view.grade, view.staff_name) if view.grade is not None else esc(view.staff_name)
    lines: list[str] = []
    if header:
        lines += [header, ""]

    if compact:
        title = f"🧾 <b>Запись № {esc(view.record_id)}</b>" if view.record_id else "🧾 <b>Запись</b>"
        if view.status is not None:
            title += f" · {STATUS_LABEL[view.status]}"
        extras = f" (+{esc(', '.join(view.addons))})" if view.addons else ""
        lines += [
            title,
            f"🗓 {when} · 📍 {esc(view.branch_title)}",
            f"💈 {master}",
            f"✂️ {esc(view.service_name)}{extras}",
            f"💰 {format_price(view.total_price)}",
        ]
        return "\n".join(lines)

    if view.record_id:
        title = f"🧾 <b>Запись № {esc(view.record_id)}</b>"
        if view.status is not None:
            title += f" · {STATUS_LABEL[view.status]}"
        lines.append(title)
    lines.append(f"📍 <b>{esc(view.branch_title)}</b>")
    if view.branch_place:
        lines.append(f"     {esc(view.branch_place)}")
    lines += [
        f"🗓 <b>{when}</b>",
        f"💈 {master}",
        f"✂️ {esc(view.service_name)}",
    ]
    if view.addons:
        lines.append(f"➕ {esc(', '.join(view.addons))}")
    lines.append(f"🍹 Напиток: {esc(view.beverage)}" if view.beverage else "🍹 Напиток: без напитка")
    lines.append("🔇 Режим: тишина" if view.silent_cut else "💬 Режим: общение")
    lines.append(f"💰 <b>Итого: {format_price(view.total_price)}</b>")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Кеш справочников CRM
# ----------------------------------------------------------------------


class CatalogCache:
    """Короткоживущий кеш медленно меняющихся справочников (филиалы, услуги, мастера).

    Экономит обращения к CRM: на каждом шаге сценария справочники нужны заново.
    """

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl = ttl_seconds
        self._items: dict[tuple[str, int], tuple[float, list[object]]] = {}

    async def _load(
        self,
        key: tuple[str, int],
        loader: Callable[[], Awaitable[list[T]]],
    ) -> list[T]:
        now = time.monotonic()
        cached = self._items.get(key)
        if cached is not None and now - cached[0] < self._ttl:
            return list(cached[1])  # type: ignore[arg-type]
        value = await loader()
        self._items[key] = (now, list(value))
        return list(value)

    async def branches(self, crm: BaseCRMService) -> list[Branch]:
        return await self._load(("branches", 0), crm.get_branches)

    async def services(self, crm: BaseCRMService, branch_id: int) -> list[Service]:
        return await self._load(("services", branch_id), lambda: crm.get_services(branch_id))

    async def staff(self, crm: BaseCRMService, branch_id: int) -> list[Staff]:
        return await self._load(("staff", branch_id), lambda: crm.get_staff(branch_id))

    def clear(self) -> None:
        self._items.clear()


catalog = CatalogCache()

# ----------------------------------------------------------------------
# Фото филиалов
# ----------------------------------------------------------------------

ASSETS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "assets" / "branches"
BRANCH_PHOTO_FILES: Final[dict[int, str]] = {1: "belorusskaya.jpg", 2: "baumanskaya.jpg"}
_photo_file_ids: dict[int, str] = {}


async def send_branch_card(
    message: Message,
    branch: Branch,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """Отправляет карточку филиала с фото входной группы.

    Файл загружается один раз, затем используется file_id из кеша.
    Если фото нет или Telegram его не принял — отправляется текстовая карточка.
    """
    sources: list[str | FSInputFile] = []
    cached = _photo_file_ids.get(branch.id)
    if cached is not None:
        sources.append(cached)
    filename = BRANCH_PHOTO_FILES.get(branch.id)
    if filename is not None and (ASSETS_DIR / filename).is_file():
        sources.append(FSInputFile(ASSETS_DIR / filename))

    for source in sources:
        try:
            sent = await message.answer_photo(source, caption=caption, reply_markup=reply_markup)
        except TelegramBadRequest:
            if isinstance(source, str):
                _photo_file_ids.pop(branch.id, None)
            continue
        if sent.photo:
            _photo_file_ids[branch.id] = sent.photo[-1].file_id
        return sent

    return await message.answer(caption, reply_markup=reply_markup)


# ----------------------------------------------------------------------
# Показ экранов
# ----------------------------------------------------------------------


async def send_new(event: Event, text: str, reply_markup: Markup | None = None) -> Message | None:
    """Отправляет новое сообщение в чат события."""
    if isinstance(event, Message):
        return await event.answer(text, reply_markup=reply_markup)
    if isinstance(event.message, Message):
        return await event.message.answer(text, reply_markup=reply_markup)
    bot = event.bot
    if bot is None:
        return None
    return await bot.send_message(event.from_user.id, text, reply_markup=reply_markup)


async def delete_message(event: Event) -> None:
    """Удаляет сообщение, к которому прикреплена кнопка (если оно ещё доступно)."""
    message = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(message, Message):
        with suppress(TelegramBadRequest):
            await message.delete()


async def show_screen(
    event: Event,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    no_preview: bool = False,
) -> Message | None:
    """Показывает экран: редактирует сообщение с кнопкой или отправляет новое.

    * для Message-события всегда отправляется новое сообщение;
    * сообщения без текста (фото-карточки) нельзя отредактировать в текст —
      они удаляются, а экран отправляется заново;
    * «message is not modified» не считается ошибкой;
    * ``no_preview=True`` отключает превью ссылок.
    """
    preview = LinkPreviewOptions(is_disabled=True) if no_preview else None

    if isinstance(event, Message):
        return await event.answer(text, reply_markup=reply_markup, link_preview_options=preview)

    message = event.message
    if not isinstance(message, Message):
        return await send_new(event, text, reply_markup)

    if message.text is None:
        await delete_message(event)
        return await message.answer(text, reply_markup=reply_markup, link_preview_options=preview)

    try:
        result = await message.edit_text(
            text,
            reply_markup=reply_markup,
            link_preview_options=preview,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return message
        await delete_message(event)
        return await message.answer(text, reply_markup=reply_markup, link_preview_options=preview)
    return result if isinstance(result, Message) else message


# ----------------------------------------------------------------------
# Доступ администратора
# ----------------------------------------------------------------------


async def require_admin(message: Message, settings: Settings) -> bool:
    """Пускает дальше только ADMIN_ID; остальным отвечает отказом."""
    sender = message.from_user
    if sender is None or sender.id != settings.ADMIN_ID:
        await message.answer("⛔ Команда доступна только администратору.")
        return False
    return True


# ----------------------------------------------------------------------
# Контакт
# ----------------------------------------------------------------------


class ContactError(Exception):
    """Присланный контакт не прошёл проверку."""


def extract_verified_phone(message: Message) -> str:
    """Возвращает нормализованный номер из контакта, проверяя владельца.

    Telegram позволяет отправить чужую карточку контакта, поэтому
    ``contact.user_id`` обязан совпадать с ``from_user.id``.
    """
    contact = message.contact
    sender = message.from_user
    if contact is None or sender is None:
        raise ContactError("Отправьте контакт кнопкой «Поделиться контактом».")
    if contact.user_id != sender.id:
        raise ContactError(
            "Это чужой контакт. Нажмите кнопку «📱 Поделиться контактом» — "
            "нужен номер вашего аккаунта."
        )
    try:
        return normalize_phone(contact.phone_number)
    except CRMValidationError as exc:
        raise ContactError(str(exc)) from exc
