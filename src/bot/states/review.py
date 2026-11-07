"""FSM-состояния сбора отзывов."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ReviewStates(StatesGroup):
    """Сбор претензии после низкой оценки."""

    waiting_complaint = State()  # ждём текст претензии (оценка 1–3★)
