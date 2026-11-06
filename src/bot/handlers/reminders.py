"""Напоминания: кнопки «Буду точно / Перенести / Отменить визит» и debug-команды администратора."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot.common import esc, format_visit_card, show_screen, view_from_appointment
from src.bot.handlers.booking import start_reschedule
from src.bot.keyboards.booking_kb import to_menu_kb
from src.bot.keyboards.reminder_kb import ReminderCB, reminder_24h_kb
from src.core.config import Settings
from src.database import AppointmentRepository, session_scope
from src.database.models import Appointment as DbAppointment
from src.scheduler.tasks import (
    branch_of,
    build_reminder_24h_text,
    send_barber_digests,
    send_reminder_2h,
    send_reminder_24h,
)
from src.services.booking_actions import cancel_visit, confirm_visit
from src.services.crm import BaseCRMService

logger = logging.getLogger(__name__)

router = Router(name="reminders")

_TEST_MARK = "🧪"


# ----------------------------------------------------------------------
# Кнопки напоминания за 24 часа
# ----------------------------------------------------------------------


def _is_test_message(callback: CallbackQuery) -> bool:
    """Сообщение отправлено debug-командой (начинается с пометки «🧪»)."""
    message = callback.message
    return isinstance(message, Message) and (message.text or "").startswith(_TEST_MARK)


@router.callback_query(ReminderCB.filter(F.action == "ok"))
async def on_reminder_confirm(
    callback: CallbackQuery,
    callback_data: ReminderCB,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """«✅ Буду точно»: confirmed в БД и confirm_appointment в CRM."""
    result = await confirm_visit(crm, callback.from_user.id, callback_data.id)
    if not result.ok or result.appointment is None:
        await callback.answer(result.message or "Не удалось подтвердить визит.", show_alert=True)
        return

    await callback.answer("Визит подтверждён ✅")
    appointment = result.appointment
    branch = await branch_of(crm, appointment.branch_id)
    text = build_reminder_24h_text(
        appointment,
        branch,
        settings,
        confirmed=True,
        test=_is_test_message(callback),
    )
    await show_screen(callback, text, reminder_24h_kb(appointment.id, confirmed=True))


@router.callback_query(ReminderCB.filter(F.action == "cancel"))
async def on_reminder_cancel(
    callback: CallbackQuery,
    callback_data: ReminderCB,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """«❌ Отменить визит»: запись отменяется в CRM (слот освобождается сразу) и в БД."""
    result = await cancel_visit(crm, callback.from_user.id, callback_data.id)
    if not result.ok or result.appointment is None:
        await callback.answer(result.message or "Не удалось отменить визит.", show_alert=True)
        return

    await callback.answer("Визит отменён")
    branch = await branch_of(crm, result.appointment.branch_id)
    card = format_visit_card(
        view_from_appointment(result.appointment, branch, settings.tzinfo),
        compact=True,
    )
    text = (
        "❌ <b>Визит отменён.</b>\n\n"
        f"{card}\n\n"
        "Время освобождено — спасибо, что предупредили заранее. Будем рады видеть вас снова!"
    )
    await show_screen(callback, text, to_menu_kb(with_booking=True))


@router.callback_query(ReminderCB.filter(F.action == "resched"))
async def on_reminder_reschedule(
    callback: CallbackQuery,
    callback_data: ReminderCB,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """«🔄 Перенести»: сценарий записи с тем же мастером и услугой, нужны только новые дата и время."""
    async with session_scope() as session:
        appointment = await AppointmentRepository(session).get(
            callback_data.id,
            user_id=callback.from_user.id,
        )
    if appointment is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    await callback.answer()
    error = await start_reschedule(callback, state, crm, settings, appointment)
    if error is not None:
        await show_screen(callback, f"⚠️ {esc(error)}", to_menu_kb(with_booking=True))


# ----------------------------------------------------------------------
# Debug-команды (только ADMIN_ID)
# ----------------------------------------------------------------------


async def _admin_last_appointment(message: Message, settings: Settings) -> DbAppointment | None:
    """Проверяет, что команду прислал администратор, и возвращает его последнюю запись."""
    sender = message.from_user
    if sender is None or sender.id != settings.ADMIN_ID:
        await message.answer("⛔ Команда доступна только администратору.")
        return None

    async with session_scope() as session:
        appointment = await AppointmentRepository(session).get_last_for_user(sender.id)
    if appointment is None:
        await message.answer(
            "У вас пока нет записей. Создайте запись через «✂️ Записаться» и повторите команду."
        )
        return None
    return appointment


@router.message(Command("test_reminder_24h"))
async def cmd_test_reminder_24h(
    message: Message,
    bot: Bot,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """Мгновенно присылает напоминание «за 24 часа» по последней записи администратора."""
    appointment = await _admin_last_appointment(message, settings)
    if appointment is None:
        return
    try:
        await send_reminder_24h(bot, appointment, crm, settings, test=True)
    except TelegramAPIError as exc:
        logger.exception("Не удалось отправить тестовое напоминание")
        await message.answer(f"❌ Не удалось отправить напоминание: {esc(exc)}")


@router.message(Command("test_reminder_2h"))
async def cmd_test_reminder_2h(
    message: Message,
    bot: Bot,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """Мгновенно присылает напоминание «за 2 часа» (карточка навигации)."""
    appointment = await _admin_last_appointment(message, settings)
    if appointment is None:
        return
    try:
        await send_reminder_2h(bot, appointment, crm, settings, test=True)
    except TelegramAPIError as exc:
        logger.exception("Не удалось отправить тестовое напоминание")
        await message.answer(f"❌ Не удалось отправить напоминание: {esc(exc)}")


@router.message(Command("test_digest"))
async def cmd_test_digest(
    message: Message,
    bot: Bot,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """Мгновенно запускает утренний дайджест барберов (для проверки вёрстки)."""
    sender = message.from_user
    if sender is None or sender.id != settings.ADMIN_ID:
        await message.answer("⛔ Команда доступна только администратору.")
        return
    stats = await send_barber_digests(bot, crm, settings)
    await message.answer(
        f"Дайджест на сегодня: записей {stats.appointments}, "
        f"отправлено сообщений {stats.messages}, ошибок {stats.failed}."
    )
