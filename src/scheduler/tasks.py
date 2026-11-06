"""Фоновые задачи бота и построение сообщений-напоминаний.

* ``process_due_reminders`` — опрос БД (каждые 60 секунд): напоминания за 24 часа
  и за 2 часа до визита;
* ``send_barber_digests`` — утренний дайджест расписания для мастеров;
* ``send_reminder_24h`` / ``send_reminder_2h`` — отправка одного напоминания
  (их же используют debug-команды администратора).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import Message

from src.bot.common import (
    GRADE_ORDER,
    barber_label,
    catalog,
    esc,
    format_date_ru,
    format_price,
    format_visit_card,
    view_from_appointment,
)
from src.bot.keyboards.reminder_kb import reminder_2h_kb, reminder_24h_kb
from src.bot.navigation import format_navigation_block, navigation_url
from src.core.config import Settings
from src.database import AppointmentRepository, BookingStatus, ReminderKind, UserRepository, session_scope
from src.database.models import Appointment as DbAppointment
from src.database.models import User, utcnow
from src.services.crm import BaseCRMService, Branch, Staff
from src.services.crm.base import plural_ru

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Правила напоминаний
# ----------------------------------------------------------------------

#: Напоминание №1 уходит, когда до визита остаётся не больше суток…
REMINDER_24H_LEAD: Final = dt.timedelta(hours=24)
#: …но не уходит, если визит ближе этого порога (тогда актуально только напоминание за 2 часа).
REMINDER_24H_MIN_REMAINING: Final = dt.timedelta(hours=3)
#: Напоминание №2 (навигация) уходит за два часа до визита.
REMINDER_2H_LEAD: Final = dt.timedelta(hours=2)

_MESSAGE_LIMIT: Final = 3800  # запас до лимита Telegram в 4096 символов


# ----------------------------------------------------------------------
# Тексты напоминаний
# ----------------------------------------------------------------------


def _relative_day(day: dt.date, today: dt.date) -> str:
    if day == today:
        return "сегодня"
    if day == today + dt.timedelta(days=1):
        return "завтра"
    return format_date_ru(day)


async def branch_of(crm: BaseCRMService, branch_id: int) -> Branch | None:
    return next((item for item in await catalog.branches(crm) if item.id == branch_id), None)


def build_reminder_24h_text(
    appointment: DbAppointment,
    branch: Branch | None,
    settings: Settings,
    *,
    confirmed: bool | None = None,
    test: bool = False,
    now: dt.datetime | None = None,
) -> str:
    """Текст напоминания за 24 часа. ``confirmed`` по умолчанию берётся из статуса записи."""
    view = view_from_appointment(appointment, branch, settings.tzinfo)
    today = (now or utcnow()).astimezone(settings.tzinfo).date()
    if confirmed is None:
        confirmed = appointment.status is BookingStatus.CONFIRMED

    lines: list[str] = []
    if test:
        lines.append("🧪 <i>Тестовое напоминание (кнопки работают с реальной записью)</i>")
    lines.append(
        f"⏰ <b>Напоминание о визите — {_relative_day(view.start.date(), today)}, {view.start:%H:%M}</b>"
    )
    lines += ["", format_visit_card(view), ""]
    if confirmed:
        lines.append("✅ <b>Визит подтверждён.</b> Ждём вас!")
        lines.append("Если планы изменились — перенесите или отмените визит.")
    else:
        lines.append("Подтвердите, пожалуйста, визит: мастер готовится к вашему приходу.")
        lines.append("Если планы изменились — перенесите или отмените визит, так время достанется другому клиенту.")
    return "\n".join(lines)


def build_reminder_2h_text(
    appointment: DbAppointment,
    branch: Branch | None,
    settings: Settings,
    *,
    test: bool = False,
    now: dt.datetime | None = None,
) -> str:
    """Текст напоминания за 2 часа: карточка навигации до филиала."""
    view = view_from_appointment(appointment, branch, settings.tzinfo)
    today = (now or utcnow()).astimezone(settings.tzinfo).date()

    lines: list[str] = []
    if test:
        lines.append("🧪 <i>Тестовое напоминание</i>")
    lines.append(
        f"🧭 <b>Скоро ваш визит — {_relative_day(view.start.date(), today)} в {view.start:%H:%M}</b>"
    )
    lines.append(f"🏠 <b>{esc(view.branch_title)}</b>")
    lines.append(f"💈 {esc(view.staff_name)} · ✂️ {esc(view.service_name)}")
    lines += ["", format_navigation_block(branch, appointment.branch_id), ""]
    lines.append("Приходите за 5–10 минут до начала.")
    if branch is not None:
        lines.append(f"Если задерживаетесь — позвоните: 📞 {esc(branch.phone)}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Отправка одного напоминания
# ----------------------------------------------------------------------


async def send_reminder_24h(
    bot: Bot,
    appointment: DbAppointment,
    crm: BaseCRMService,
    settings: Settings,
    *,
    test: bool = False,
) -> Message:
    """Напоминание за 24 часа с кнопками [✅ Буду точно] [🔄 Перенести] [❌ Отменить визит]."""
    branch = await branch_of(crm, appointment.branch_id)
    text = build_reminder_24h_text(appointment, branch, settings, test=test)
    markup = reminder_24h_kb(
        appointment.id,
        confirmed=appointment.status is BookingStatus.CONFIRMED,
    )
    return await bot.send_message(appointment.user_id, text, reply_markup=markup)


async def send_reminder_2h(
    bot: Bot,
    appointment: DbAppointment,
    crm: BaseCRMService,
    settings: Settings,
    *,
    test: bool = False,
) -> Message:
    """Напоминание за 2 часа: адрес, вход, парковка, код шлагбаума и кнопка Яндекс.Навигатора."""
    branch = await branch_of(crm, appointment.branch_id)
    text = build_reminder_2h_text(appointment, branch, settings, test=test)
    markup = reminder_2h_kb(navigation_url(appointment.branch_id))
    return await bot.send_message(appointment.user_id, text, reply_markup=markup)


# ----------------------------------------------------------------------
# Опрос БД: напоминания
# ----------------------------------------------------------------------


class _Delivery(enum.Enum):
    SENT = "sent"          # доставлено
    UNREACHABLE = "gone"   # доставить нельзя (бот заблокирован, чат не найден) — повторять бессмысленно
    RETRY = "retry"        # временный сбой — попробуем при следующем опросе


async def _deliver(send: Callable[[], Awaitable[Message]], appointment_id: int) -> _Delivery:
    """Отправляет сообщение и классифицирует результат."""
    try:
        await send()
    except TelegramRetryAfter as exc:
        logger.warning("Telegram просит подождать %s с. (запись #%s)", exc.retry_after, appointment_id)
        await asyncio.sleep(min(exc.retry_after, 30))
        return _Delivery.RETRY
    except TelegramForbiddenError:
        logger.info("Клиент заблокировал бота, напоминание по записи #%s пропущено", appointment_id)
        return _Delivery.UNREACHABLE
    except TelegramBadRequest as exc:
        # Например, «chat not found» или ошибка разметки: повторы ничего не изменят.
        logger.error("Telegram отклонил напоминание по записи #%s: %s", appointment_id, exc)
        return _Delivery.UNREACHABLE
    except TelegramAPIError as exc:
        logger.warning("Сбой отправки напоминания по записи #%s: %s", appointment_id, exc)
        return _Delivery.RETRY
    return _Delivery.SENT


@dataclass(slots=True)
class ReminderStats:
    """Итог одного прохода опроса."""

    sent_24h: int = 0
    sent_2h: int = 0
    unreachable: int = 0
    retry: int = 0


async def process_due_reminders(
    bot: Bot,
    crm: BaseCRMService,
    settings: Settings,
    *,
    now: dt.datetime | None = None,
) -> ReminderStats:
    """Находит записи, которым пора отправить напоминания, и отправляет их.

    Отметка об отправке ставится только после успешной доставки (или если доставка
    невозможна), поэтому при временном сбое напоминание уйдёт на следующем проходе
    и никогда не уйдёт дважды.
    """
    moment = now or utcnow()
    stats = ReminderStats()

    async with session_scope() as session:
        repo = AppointmentRepository(session)
        due_24h = await repo.get_due_for_reminder(
            ReminderKind.H24,
            lead_time=REMINDER_24H_LEAD,
            min_remaining=REMINDER_24H_MIN_REMAINING,
            now=moment,
        )
        due_2h = await repo.get_due_for_reminder(
            ReminderKind.H2,
            lead_time=REMINDER_2H_LEAD,
            now=moment,
        )

    batches = (
        (ReminderKind.H24, due_24h, send_reminder_24h),
        (ReminderKind.H2, due_2h, send_reminder_2h),
    )
    for kind, appointments, sender in batches:
        for appointment in appointments:
            outcome = await _deliver(
                lambda a=appointment, s=sender: s(bot, a, crm, settings),
                appointment.id,
            )
            if outcome is _Delivery.RETRY:
                stats.retry += 1
                continue

            async with session_scope() as session:
                await AppointmentRepository(session).mark_reminder_sent(kind, [appointment.id])

            if outcome is _Delivery.UNREACHABLE:
                stats.unreachable += 1
            elif kind is ReminderKind.H24:
                stats.sent_24h += 1
            else:
                stats.sent_2h += 1

    if stats.sent_24h or stats.sent_2h or stats.unreachable or stats.retry:
        logger.info(
            "Напоминания: за 24 ч — %s, за 2 ч — %s, недоступны — %s, повтор — %s",
            stats.sent_24h,
            stats.sent_2h,
            stats.unreachable,
            stats.retry,
        )
    return stats


# ----------------------------------------------------------------------
# Утренний дайджест барберов
# ----------------------------------------------------------------------


def build_master_section(
    member: Staff,
    appointments: Sequence[DbAppointment],
    users: dict[int, User],
    settings: Settings,
) -> str:
    """Расписание одного мастера на день: время, клиент, услуга, напиток и Silent Cut."""
    tz = settings.tzinfo
    title = f"💈 <b>{barber_label(member.grade, member.full_name)}</b>"
    if not appointments:
        return f"{title}\n     — записей нет"

    visits = len(appointments)
    word = plural_ru(visits, "визит", "визита", "визитов")
    total = sum(item.total_price for item in appointments)
    lines = [f"{title} — {visits} {word}, {format_price(total)}"]

    for item in appointments:
        start = item.datetime_start.astimezone(tz)
        end = (item.datetime_end or item.datetime_start + dt.timedelta(hours=1)).astimezone(tz)
        client = users.get(item.user_id)
        name = esc(client.first_name) if client else "клиент"
        mark = "✅" if item.status is BookingStatus.CONFIRMED else "🕓"
        service = esc(item.service_name)
        if item.addons:
            service += f" (+{esc(', '.join(item.addons))})"
        drink = f"🍹 {esc(item.beverage)}" if item.beverage else "🍹 без напитка"
        silent = " · 🔇 <b>SILENT CUT</b>" if item.silent_cut else ""

        lines.append(f"🕐 <b>{start:%H:%M}–{end:%H:%M}</b> · {name} {mark}")
        lines.append(f"     ✂️ {service}")
        lines.append(f"     {drink}{silent}")
    return "\n".join(lines)


def _chunk_blocks(header: str, blocks: Sequence[str], limit: int = _MESSAGE_LIMIT) -> list[str]:
    """Склеивает блоки в сообщения не длиннее ``limit`` (блок не разрывается)."""
    messages: list[str] = []
    current = header
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current != header:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


@dataclass(slots=True)
class DigestStats:
    """Итог рассылки дайджестов."""

    messages: int = 0
    appointments: int = 0
    failed: int = 0


async def _send_text(bot: Bot, chat_id: int, text: str, stats: DigestStats) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError as exc:
        stats.failed += 1
        logger.error("Не удалось отправить дайджест в чат %s: %s", chat_id, exc)
    else:
        stats.messages += 1


async def send_barber_digests(
    bot: Bot,
    crm: BaseCRMService,
    settings: Settings,
    *,
    day: dt.date | None = None,
) -> DigestStats:
    """Утренний дайджест: расписание на день для каждого мастера.

    * мастера из ``BARBER_CHAT_IDS`` получают своё расписание в личные сообщения
      (если у них есть записи);
    * расписание остальных мастеров собирается в одно сообщение на филиал
      и уходит в чат дайджестов (``DIGEST_CHAT_ID``, по умолчанию — ``ADMIN_ID``).
    """
    tz = settings.tzinfo
    today = day or dt.datetime.now(tz).date()
    day_start = dt.datetime.combine(today, dt.time.min, tzinfo=tz)
    day_end = dt.datetime.combine(today + dt.timedelta(days=1), dt.time.min, tzinfo=tz)

    async with session_scope() as session:
        rows = await AppointmentRepository(session).list_between(day_start, day_end)
        users = await UserRepository(session).get_many({row.user_id for row in rows})

    by_master: dict[tuple[int, int], list[DbAppointment]] = defaultdict(list)
    for row in rows:
        by_master[(row.branch_id, row.staff_id)].append(row)

    stats = DigestStats(appointments=len(rows))
    personal_chats = settings.barber_chat_ids
    date_label = format_date_ru(today)

    for branch in await catalog.branches(crm):
        grade_rank = {grade: index for index, grade in enumerate(GRADE_ORDER)}
        staff = sorted(
            await catalog.staff(crm, branch.id),
            key=lambda member: (grade_rank[member.grade], member.id),
        )

        shared_sections: list[str] = []
        branch_has_visits = False
        for member in staff:
            items = by_master.get((branch.id, member.id), [])
            branch_has_visits = branch_has_visits or bool(items)
            section = build_master_section(member, items, users, settings)

            personal_chat = personal_chats.get(member.id)
            if personal_chat is None:
                shared_sections.append(section)
                continue
            if items:
                header = f"🌅 <b>Ваше расписание на {date_label}</b>\n📍 {esc(branch.title)}"
                for text in _chunk_blocks(header, [section]):
                    await _send_text(bot, personal_chat, text, stats)

        if not shared_sections:
            continue
        header = f"🌅 <b>Расписание на {date_label}</b>\n📍 {esc(branch.title)}"
        if not branch_has_visits:
            await _send_text(bot, settings.digest_chat_id, f"{header}\n\nНа сегодня записей нет.", stats)
            continue
        for text in _chunk_blocks(header, shared_sections):
            await _send_text(bot, settings.digest_chat_id, text, stats)

    logger.info(
        "Дайджест на %s: записей %s, сообщений %s, ошибок %s",
        today.isoformat(),
        stats.appointments,
        stats.messages,
        stats.failed,
    )
    return stats
