"""Клавиатуры напоминаний (анти-NoShow)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.keyboards.booking_kb import MenuCB


class ReminderCB(CallbackData, prefix="rm"):
    """Кнопки напоминания: ok | resched | cancel."""

    action: str
    id: int


def reminder_24h_kb(appointment_id: int, *, confirmed: bool = False) -> InlineKeyboardMarkup:
    """[✅ Буду точно] / [🔄 Перенести] / [❌ Отменить визит].

    После подтверждения кнопка «Буду точно» скрывается.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if not confirmed:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Буду точно",
                    callback_data=ReminderCB(action="ok", id=appointment_id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 Перенести",
                callback_data=ReminderCB(action="resched", id=appointment_id).pack(),
            ),
            InlineKeyboardButton(
                text="❌ Отменить визит",
                callback_data=ReminderCB(action="cancel", id=appointment_id).pack(),
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminder_2h_kb(navigator_url: str | None) -> InlineKeyboardMarkup:
    """Кнопка маршрута в Яндекс.Навигаторе и переход к «Моим записям»."""
    rows: list[list[InlineKeyboardButton]] = []
    if navigator_url:
        rows.append([InlineKeyboardButton(text="🧭 Открыть в Яндекс.Навигаторе", url=navigator_url)])
    rows.append(
        [InlineKeyboardButton(text="📋 Мои записи", callback_data=MenuCB(action="my").pack())]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
