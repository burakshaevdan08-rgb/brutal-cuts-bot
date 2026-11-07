"""Пост-сервис: сбор отзывов (Review Gatekeeping), чаевые и алерты о негативе.

Воронка::

    запрос оценки (через 45 мин после визита) → [1⭐] … [5⭐]
      5★  → благодарность + отзыв на Яндекс.Картах + чаевые мастеру
      4★  → благодарность + чаевые мастеру
      1–3★ → ссылки на карты НЕ выдаются → FSM: текст претензии
             → алерт администратору (текст + телефон гостя)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.common import (
    MAIN_MENU_TEXT,
    barber_label,
    catalog,
    esc,
    format_date_ru,
    format_phone,
    name_dative,
    require_admin,
    show_screen,
)
from src.bot.keyboards.booking_kb import main_menu_kb, to_menu_kb
from src.bot.keyboards.review_kb import ReviewCB, complaint_kb, thanks_kb
from src.bot.navigation import branch_maps_url
from src.bot.states.review import ReviewStates
from src.core.config import Settings
from src.database import (
    AppointmentRepository,
    RepositoryError,
    ReviewNotAllowedError,
    ReviewRepository,
    UserRepository,
    session_scope,
)
from src.database.models import Appointment as DbAppointment
from src.database.models import User
from src.scheduler.tasks import branch_of, send_review_request
from src.services.crm import BaseCRMService, Branch, Grade

logger = logging.getLogger(__name__)

router = Router(name="reviews")

MAX_COMPLAINT_LENGTH = 1500

# Защита от двойного нажатия на звёзды: один отзыв на визит.
_rating_locks: dict[int, asyncio.Lock] = {}


# ----------------------------------------------------------------------
# Алерт администратору
# ----------------------------------------------------------------------


def build_negative_review_alert(
    *,
    appointment: DbAppointment,
    branch: Branch | None,
    guest_name: str,
    guest_username: str | None,
    phone: str | None,
    rating: int,
    text: str | None,
    settings: Settings,
    test: bool = False,
) -> str:
    """Текст алерта «🚨 ЧП! Негативный отзыв!» с претензией и телефоном гостя."""
    start = appointment.datetime_start.astimezone(settings.tzinfo)
    try:
        master = barber_label(Grade(appointment.staff_grade), appointment.staff_name)
    except ValueError:
        master = esc(appointment.staff_name)

    guest = esc(guest_name) + (f" (@{esc(guest_username)})" if guest_username else "")
    lines: list[str] = []
    if test:
        lines += ["🧪 <i>ТЕСТ: реального инцидента нет</i>", ""]
    lines += [
        "🚨 <b>ЧП! Негативный отзыв! Связаться за 5 минут!</b>",
        "",
        f"⭐ Оценка: <b>{rating}/5</b>",
        f"👤 Гость: {guest}",
        f"📱 Телефон: <code>{esc(format_phone(phone))}</code>" if phone else "📱 Телефон: не указан",
        f"💈 Мастер: {master}",
        f"📍 Филиал: {esc(branch.title) if branch else appointment.branch_id}",
        f"🗓 Визит: {format_date_ru(start.date())}, {start:%H:%M} · ✂️ {esc(appointment.service_name)}",
        f"🧾 Запись № {esc(appointment.crm_record_id)}",
        "",
    ]
    if text:
        lines += ["📝 <b>Претензия гостя:</b>", f"«{esc(text)}»"]
    else:
        lines.append("📝 Гость поставил оценку, но не стал писать комментарий.")
    return "\n".join(lines)


async def send_negative_review_alert(
    bot: Bot,
    settings: Settings,
    crm: BaseCRMService,
    *,
    appointment: DbAppointment,
    guest: User | None,
    tg_id: int,
    tg_first_name: str,
    tg_username: str | None,
    rating: int,
    text: str | None,
    test: bool = False,
) -> bool:
    """Мгновенно отправляет алерт на ADMIN_ID. Возвращает True, если он доставлен."""
    branch = await branch_of(crm, appointment.branch_id)
    alert = build_negative_review_alert(
        appointment=appointment,
        branch=branch,
        guest_name=guest.first_name if guest else tg_first_name,
        guest_username=tg_username,
        phone=guest.phone if guest else None,
        rating=rating,
        text=text,
        settings=settings,
        test=test,
    )
    # tg://user?id=… открывает чат с гостем даже без username (если позволяют его настройки приватности).
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💬 Написать гостю", url=f"tg://user?id={tg_id}")]]
    )
    try:
        await bot.send_message(settings.ADMIN_ID, alert, reply_markup=markup)
    except TelegramAPIError:
        # Если кнопка с tg://user?id отклонена (настройки приватности), шлём алерт без неё.
        try:
            await bot.send_message(settings.ADMIN_ID, alert)
        except TelegramAPIError:
            logger.exception("Не удалось отправить алерт о негативном отзыве администратору")
            return False
    return True


# ----------------------------------------------------------------------
# Оценка визита
# ----------------------------------------------------------------------


@router.callback_query(ReviewCB.filter(F.action == "rate"))
async def on_rating(
    callback: CallbackQuery,
    callback_data: ReviewCB,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    stars = callback_data.stars
    if not 1 <= stars <= 5:
        await callback.answer("Некорректная оценка.", show_alert=True)
        return

    test = bool(callback_data.test)
    tg_user = callback.from_user
    lock = _rating_locks.setdefault(callback_data.id, asyncio.Lock())
    if lock.locked():
        await callback.answer()
        return

    async with lock:
        try:
            async with session_scope() as session:
                appointment = await AppointmentRepository(session).get(callback_data.id, user_id=tg_user.id)
                reviews = ReviewRepository(session)
                if appointment is None:
                    await callback.answer("Запись не найдена.", show_alert=True)
                    return
                if not test:
                    if await reviews.get_by_appointment(appointment.id) is not None:
                        await callback.answer("Спасибо, вы уже оценили этот визит 🙌", show_alert=True)
                        return
                    # Оценка сохраняется сразу: она не потеряется, даже если гость не напишет претензию.
                    await reviews.create(appointment_id=appointment.id, user_id=tg_user.id, rating=stars)
        except ReviewNotAllowedError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        finally:
            _rating_locks.pop(callback_data.id, None)

    await callback.answer()

    if stars <= 3:
        await _ask_for_complaint(callback, state, appointment, stars, test)
        return
    await _thank_and_offer_tip(callback, crm, settings, appointment, stars, test)


async def _thank_and_offer_tip(
    callback: CallbackQuery,
    crm: BaseCRMService,
    settings: Settings,
    appointment: DbAppointment,
    stars: int,
    test: bool,
) -> None:
    """4★ — благодарность и чаевые; 5★ — ещё и отзыв на Яндекс.Картах."""
    first_name = appointment.staff_name.split()[0]
    staff = next(
        (member for member in await catalog.staff(crm, appointment.branch_id) if member.id == appointment.staff_id),
        None,
    )
    tip_url = staff.netmonet_url if staff else None
    maps_url = branch_maps_url(appointment.branch_id, settings.branch_review_urls) if stars == 5 else None

    lines: list[str] = []
    if test:
        lines += ["🧪 <i>Тест: отзыв не сохранён</i>", ""]
    if stars == 5:
        lines += [
            "🙌 <b>Спасибо за высшую оценку!</b>",
            "",
            "Нам очень приятно. Если у вас есть минутка, оставьте отзыв о нас на Яндекс.Картах: "
            "так нас находят другие гости.",
        ]
    else:
        lines += [
            "🙏 <b>Спасибо за оценку!</b>",
            "",
            "Мы стараемся становиться лучше с каждым визитом.",
        ]
    if tip_url:
        lines += ["", f"☕ А если хочется поблагодарить мастера лично, оставьте {esc(name_dative(first_name))} на чай."]

    markup = thanks_kb(tip_url=tip_url, master_dative=name_dative(first_name), maps_url=maps_url)
    await show_screen(callback, "\n".join(lines), markup, no_preview=True)


async def _ask_for_complaint(
    callback: CallbackQuery,
    state: FSMContext,
    appointment: DbAppointment,
    stars: int,
    test: bool,
) -> None:
    """1–3★: никаких ссылок на карты, вместо них — сбор текста претензии."""
    await state.set_state(ReviewStates.waiting_complaint)
    await state.update_data(
        review_appointment_id=appointment.id,
        review_rating=stars,
        review_test=test,
    )
    lines: list[str] = []
    if test:
        lines += ["🧪 <i>Тест: отзыв не сохранён</i>", ""]
    lines += [
        "😔 <b>Нам очень жаль, что визит прошёл не так, как должен.</b>",
        "",
        "Расскажите, пожалуйста, что именно пошло не так, одним сообщением. "
        "Управляющий свяжется с вами в течение 5 минут, чтобы всё исправить.",
    ]
    await show_screen(callback, "\n".join(lines), complaint_kb(appointment.id, test=test))


# ----------------------------------------------------------------------
# Претензия (FSM)
# ----------------------------------------------------------------------


async def _finish_complaint(
    *,
    tg_id: int,
    tg_first_name: str,
    tg_username: str | None,
    reply_to: Message | CallbackQuery,
    state: FSMContext,
    bot: Bot,
    crm: BaseCRMService,
    settings: Settings,
    text: str | None,
) -> None:
    """Сохраняет претензию, мгновенно уведомляет администратора и благодарит гостя."""
    data = await state.get_data()
    appointment_id = data.get("review_appointment_id")
    rating = data.get("review_rating")
    test = bool(data.get("review_test"))
    await state.clear()

    if appointment_id is None or rating is None:
        await show_screen(reply_to, f"Сессия устарела.\n\n{MAIN_MENU_TEXT}", main_menu_kb())
        return

    async with session_scope() as session:
        appointment = await AppointmentRepository(session).get(appointment_id, user_id=tg_id)
        guest = await UserRepository(session).get(tg_id)
        if appointment is not None and text and not test:
            try:
                await ReviewRepository(session).set_feedback(appointment_id, tg_id, text)
            except RepositoryError:
                logger.exception("Не удалось сохранить текст отзыва по записи #%s", appointment_id)

    if appointment is None:
        await show_screen(reply_to, f"Запись не найдена.\n\n{MAIN_MENU_TEXT}", main_menu_kb())
        return

    delivered = await send_negative_review_alert(
        bot,
        settings,
        crm,
        appointment=appointment,
        guest=guest,
        tg_id=tg_id,
        tg_first_name=tg_first_name,
        tg_username=tg_username,
        rating=int(rating),
        text=text,
        test=test,
    )
    if not delivered:
        logger.error("Алерт по записи #%s не доставлен администратору", appointment_id)

    thanks = (
        "🙏 <b>Спасибо, что рассказали.</b>\n\n"
        "Мы уже передали это управляющему: он свяжется с вами в ближайшие минуты. "
        "Приносим извинения и обязательно всё исправим."
    )
    if test:
        thanks = "🧪 <i>Тест: алерт отправлен на ADMIN_ID, отзыв не сохранён</i>\n\n" + thanks
    await show_screen(reply_to, thanks, to_menu_kb(with_booking=True))


@router.message(Command("test_review"))
async def cmd_test_review(
    message: Message,
    bot: Bot,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """Debug (ADMIN_ID): мгновенно присылает запрос оценки по последней записи."""
    if not await require_admin(message, settings):
        return
    async with session_scope() as session:
        appointment = await AppointmentRepository(session).get_last_for_user(message.from_user.id)  # type: ignore[union-attr]
    if appointment is None:
        await message.answer("У вас пока нет записей. Создайте запись через «✂️ Записаться» и повторите команду.")
        return
    try:
        await send_review_request(bot, appointment, crm, settings, test=True)
    except TelegramAPIError as exc:
        logger.exception("Не удалось отправить тестовый запрос оценки")
        await message.answer(f"❌ Не удалось отправить запрос: {esc(exc)}")


@router.message(ReviewStates.waiting_complaint, F.text, ~F.text.startswith("/"))
async def on_complaint_text(
    message: Message,
    state: FSMContext,
    bot: Bot,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """Текст претензии → алерт на ADMIN_ID вместе с телефоном гостя."""
    text = (message.text or "").strip()
    if len(text) < 3:
        await message.answer("Опишите, пожалуйста, чуть подробнее: что именно пошло не так?")
        return
    sender = message.from_user
    if sender is None:
        return
    await _finish_complaint(
        tg_id=sender.id,
        tg_first_name=sender.first_name,
        tg_username=sender.username,
        reply_to=message,
        state=state,
        bot=bot,
        crm=crm,
        settings=settings,
        text=text[:MAX_COMPLAINT_LENGTH],
    )


@router.message(ReviewStates.waiting_complaint, ~F.text)
async def on_complaint_not_text(message: Message) -> None:
    await message.answer("Пожалуйста, опишите проблему текстом — так управляющий быстрее вам поможет.")


@router.callback_query(ReviewCB.filter(F.action == "skip"), ReviewStates.waiting_complaint)
async def on_complaint_skipped(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """«Без комментария»: администратору всё равно уходит алерт — гостю нужно перезвонить."""
    await callback.answer()
    await _finish_complaint(
        tg_id=callback.from_user.id,
        tg_first_name=callback.from_user.first_name,
        tg_username=callback.from_user.username,
        reply_to=callback,
        state=state,
        bot=bot,
        crm=crm,
        settings=settings,
        text=None,
    )


@router.callback_query(ReviewCB.filter(F.action == "skip"))
async def on_stale_skip(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка уже неактуальна.", show_alert=True)
