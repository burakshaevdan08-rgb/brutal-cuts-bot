"""Клавиатуры сбора отзывов (пост-сервис)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.keyboards.booking_kb import MenuCB


class ReviewCB(CallbackData, prefix="rv"):
    """Кнопки отзыва: rate (выбор звёзд) | skip (без комментария к претензии).

    ``test=1`` — сообщение отправлено командой /test_review: отзыв в БД не сохраняется.
    """

    action: str
    id: int
    stars: int = 0
    test: int = 0


def rating_kb(appointment_id: int, *, test: bool = False) -> InlineKeyboardMarkup:
    """[1⭐] [2⭐] [3⭐] [4⭐] [5⭐]."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{stars}⭐",
                    callback_data=ReviewCB(
                        action="rate",
                        id=appointment_id,
                        stars=stars,
                        test=int(test),
                    ).pack(),
                )
                for stars in range(1, 6)
            ]
        ]
    )


def thanks_kb(
    *,
    tip_url: str | None,
    master_dative: str,
    maps_url: str | None = None,
) -> InlineKeyboardMarkup:
    """После оценки 4–5★: чаевые мастеру и (только для 5★) отзыв на Яндекс.Картах."""
    rows: list[list[InlineKeyboardButton]] = []
    if maps_url:
        rows.append([InlineKeyboardButton(text="📍 Отзыв на Яндекс.Картах", url=maps_url)])
    if tip_url:
        rows.append(
            [InlineKeyboardButton(text=f"☕ Оставить {master_dative} на чай", url=tip_url)]
        )
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data=MenuCB(action="home").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def complaint_kb(appointment_id: int, *, test: bool = False) -> InlineKeyboardMarkup:
    """Под просьбой описать проблему: можно отправить оценку без текста."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➡️ Без комментария",
                    callback_data=ReviewCB(action="skip", id=appointment_id, test=int(test)).pack(),
                )
            ]
        ]
    )
