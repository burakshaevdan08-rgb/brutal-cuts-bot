"""Главное меню: /start, бонусы, филиалы и «Мои записи»."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from src.bot.common import (
    MAIN_MENU_TEXT,
    PHONE_PROMPT,
    ContactError,
    branch_card,
    catalog,
    delete_message,
    esc,
    extract_verified_phone,
    format_date_ru,
    format_phone,
    format_visit_card,
    send_branch_card,
    send_new,
    show_screen,
    view_from_appointment,
)
from src.bot.keyboards.booking_kb import (
    PHONE_CANCEL_TEXT,
    MenuCB,
    MyCB,
    branch_info_kb,
    cancel_confirm_kb,
    main_menu_kb,
    my_appointments_kb,
    phone_request_kb,
    to_menu_kb,
)
from src.bot.states.booking import MenuStates
from src.core.config import Settings
from src.database import AppointmentRepository, BookingStatus, UserRepository, session_scope
from src.services.booking_actions import cancel_visit
from src.services.crm import BaseCRMService, CRMError

logger = logging.getLogger(__name__)

router = Router(name="menu")


# ----------------------------------------------------------------------
# /start и главное меню
# ----------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, crm: BaseCRMService) -> None:
    await state.clear()
    tg_user = message.from_user
    if tg_user is None:
        return
    async with session_scope() as session:
        _, created = await UserRepository(session).get_or_create(
            telegram_id=tg_user.id,
            first_name=tg_user.first_name,
            username=tg_user.username,
        )

    greeting = "Добро пожаловать" if created else "С возвращением"
    lines = [
        f"🔥 <b>BRUTAL CUTS</b> — мужской клуб",
        f"{greeting}, <b>{esc(tg_user.first_name)}</b>!",
        "",
        "Записывайтесь к барберам онлайн: выберите филиал, услугу, мастера и удобное время.",
    ]
    if crm.is_mock:
        lines += ["", "<i>ℹ️ Демо-режим: данные расписания и мастеров тестовые.</i>"]
    await message.answer("\n".join(lines))
    await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_kb())


@router.callback_query(MenuCB.filter(F.action == "home"))
async def on_home(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await show_screen(callback, MAIN_MENU_TEXT, main_menu_kb())


# ----------------------------------------------------------------------
# Бонусы
# ----------------------------------------------------------------------


async def _show_balance(event: CallbackQuery | Message, crm: BaseCRMService, phone: str) -> None:
    try:
        balance = await crm.get_client_loyalty_balance(phone)
    except CRMError as exc:
        await show_screen(event, f"❌ {esc(exc)}", to_menu_kb())
        return

    text = "\n".join(
        [
            "🎁 <b>Бонусы BRUTAL CUTS</b>",
            "",
            f"📱 {format_phone(balance.phone)}",
            f"🏅 Уровень: <b>{esc(balance.tier)}</b> (скидка {balance.discount_percent}%)",
            f"✂️ Визитов: {balance.visits}",
            f"💎 Баллов: <b>{balance.points}</b>",
        ]
    )
    await show_screen(event, text, to_menu_kb(with_booking=True))


@router.callback_query(MenuCB.filter(F.action == "balance"))
async def on_balance(callback: CallbackQuery, state: FSMContext, crm: BaseCRMService) -> None:
    await callback.answer()
    await state.clear()
    async with session_scope() as session:
        user = await UserRepository(session).get(callback.from_user.id)

    if user is not None and user.phone:
        await _show_balance(callback, crm, user.phone)
        return

    await state.set_state(MenuStates.requesting_phone_for_balance)
    await show_screen(callback, "🎁 <b>Бонусы BRUTAL CUTS</b>\n\nЧтобы показать баланс, нужен ваш номер телефона.")
    await send_new(callback, PHONE_PROMPT, phone_request_kb())


@router.message(MenuStates.requesting_phone_for_balance, F.contact)
async def on_balance_contact(message: Message, state: FSMContext, crm: BaseCRMService) -> None:
    try:
        phone = extract_verified_phone(message)
    except ContactError as exc:
        await message.answer(f"⚠️ {esc(exc)}", reply_markup=phone_request_kb())
        return

    tg_user = message.from_user
    assert tg_user is not None
    async with session_scope() as session:
        users = UserRepository(session)
        await users.get_or_create(
            telegram_id=tg_user.id,
            first_name=tg_user.first_name,
            username=tg_user.username,
        )
        await users.update_phone(tg_user.id, phone)

    await state.clear()
    await message.answer(
        f"✅ Номер {format_phone(phone)} подтверждён.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await _show_balance(message, crm, phone)


@router.message(MenuStates.requesting_phone_for_balance, F.text == PHONE_CANCEL_TEXT)
async def on_balance_cancelled(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Хорошо, вернёмся к бонусам позже.", reply_markup=ReplyKeyboardRemove())
    await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_kb())


@router.message(MenuStates.requesting_phone_for_balance)
async def on_balance_unexpected(message: Message) -> None:
    await message.answer(
        "Нажмите кнопку «📱 Поделиться контактом» внизу экрана.",
        reply_markup=phone_request_kb(),
    )


# ----------------------------------------------------------------------
# Филиалы
# ----------------------------------------------------------------------


@router.callback_query(MenuCB.filter(F.action == "branches"))
async def on_branches(callback: CallbackQuery, state: FSMContext, crm: BaseCRMService) -> None:
    await callback.answer()
    await state.clear()
    branches = await catalog.branches(crm)
    message = callback.message
    if not isinstance(message, Message):
        return

    for branch in branches:
        staff = await catalog.staff(crm, branch.id)
        await send_branch_card(message, branch, branch_card(branch, staff), branch_info_kb(branch.id))
    await delete_message(callback)


# ----------------------------------------------------------------------
# Мои записи
# ----------------------------------------------------------------------


async def _show_my_appointments(event: CallbackQuery, crm: BaseCRMService, settings: Settings) -> None:
    async with session_scope() as session:
        items = await AppointmentRepository(session).list_upcoming_for_user(event.from_user.id, limit=5)

    if not items:
        await show_screen(
            event,
            "📋 <b>Мои записи</b>\n\nПредстоящих записей нет.",
            to_menu_kb(with_booking=True),
        )
        return

    branches = {branch.id: branch for branch in await catalog.branches(crm)}
    cards: list[str] = []
    buttons: list[tuple[int, str]] = []
    for appointment in items:
        view = view_from_appointment(appointment, branches.get(appointment.branch_id), settings.tzinfo)
        cards.append(format_visit_card(view, compact=True))
        buttons.append(
            (appointment.id, f"{format_date_ru(view.start.date())} {view.start:%H:%M}")
        )
    text = "📋 <b>Мои записи</b>\n\n" + "\n\n".join(cards)
    await show_screen(event, text, my_appointments_kb(buttons))


@router.callback_query(MenuCB.filter(F.action == "my"))
async def on_my_appointments(
    callback: CallbackQuery,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    await callback.answer()
    await state.clear()
    await _show_my_appointments(callback, crm, settings)


@router.callback_query(MyCB.filter(F.action == "list"))
async def on_my_list(callback: CallbackQuery, crm: BaseCRMService, settings: Settings) -> None:
    await callback.answer()
    await _show_my_appointments(callback, crm, settings)


@router.callback_query(MyCB.filter(F.action == "cancel"))
async def on_cancel_ask(
    callback: CallbackQuery,
    callback_data: MyCB,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """Запрос подтверждения отмены."""
    async with session_scope() as session:
        appointment = await AppointmentRepository(session).get(callback_data.id, user_id=callback.from_user.id)

    if appointment is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    if appointment.status in (BookingStatus.CANCELLED, BookingStatus.COMPLETED):
        await callback.answer("Эту запись уже нельзя отменить.", show_alert=True)
        return
    await callback.answer()

    branch = next((b for b in await catalog.branches(crm) if b.id == appointment.branch_id), None)
    view = view_from_appointment(appointment, branch, settings.tzinfo)
    text = "❓ <b>Отменить запись?</b>\n\n" + format_visit_card(view, compact=True)
    await show_screen(callback, text, cancel_confirm_kb(appointment.id))


@router.callback_query(MyCB.filter(F.action == "cancel_yes"))
async def on_cancel_confirmed(
    callback: CallbackQuery,
    callback_data: MyCB,
    crm: BaseCRMService,
) -> None:
    """Отмена: сначала в CRM (освобождает слот), затем в локальной БД."""
    result = await cancel_visit(crm, callback.from_user.id, callback_data.id)
    if not result.ok:
        await callback.answer(result.message, show_alert=True)
        return

    await callback.answer("Запись отменена")
    await show_screen(
        callback,
        "❌ <b>Запись отменена.</b>\nБудем рады видеть вас снова!",
        to_menu_kb(with_booking=True),
    )
