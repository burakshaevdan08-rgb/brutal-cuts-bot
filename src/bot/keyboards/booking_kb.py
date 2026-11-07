"""Фабрики клавиатур сценария записи и главного меню.

Все inline-кнопки используют типизированные CallbackData:

* ``MenuCB``    — главное меню (prefix ``menu``);
* ``BookingCB`` — шаги записи (prefix ``bk``): ``action`` и ``value``;
* ``MyCB``      — управление существующими записями (prefix ``my``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from src.bot.common import (
    BEVERAGES,
    GRADE_EMOJI,
    GRADE_ORDER,
    GRADE_TITLE_PLURAL,
    MONTHS_NOMINATIVE,
    NO_BEVERAGE_CODE,
    SERVICE_EMOJI,
    WEEKDAYS_SHORT,
    baseline_price,
    format_price,
    visit_total,
)
from src.bot.navigation import navigation_url
from src.services.crm import Branch, Service, Staff

PHONE_CANCEL_TEXT = "✖️ Отмена"
_BLANK = "\u2800"  # невидимый символ для пустых ячеек календаря


# ----------------------------------------------------------------------
# CallbackData
# ----------------------------------------------------------------------


class MenuCB(CallbackData, prefix="menu"):
    """Главное меню: book | balance | branches | my | home."""

    action: str


class BookingCB(CallbackData, prefix="bk"):
    """Шаги записи."""

    action: str
    value: str = ""


class MyCB(CallbackData, prefix="my"):
    """Мои записи: list | cancel | cancel_yes | resched."""

    action: str
    id: int = 0


NOOP = BookingCB(action="noop")


# ----------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------


def _btn(text: str, callback: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback.pack())


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _nav_row(back: str | None = None) -> list[InlineKeyboardButton]:
    """Нижняя строка навигации: «Назад» (к шагу ``back``) и «Отмена»."""
    row: list[InlineKeyboardButton] = []
    if back is not None:
        row.append(_btn("⬅️ Назад", BookingCB(action="back", value=back)))
    row.append(_btn("✖️ Отмена", BookingCB(action="cancel")))
    return row


def _chunk(buttons: Sequence[InlineKeyboardButton], size: int) -> list[list[InlineKeyboardButton]]:
    return [list(buttons[i : i + size]) for i in range(0, len(buttons), size)]


# ----------------------------------------------------------------------
# Главное меню
# ----------------------------------------------------------------------


def main_menu_kb() -> InlineKeyboardMarkup:
    return _markup(
        [
            [_btn("✂️ Записаться", MenuCB(action="book"))],
            [_btn("🎁 Мой баланс бонусов", MenuCB(action="balance"))],
            [
                _btn("📍 Филиалы", MenuCB(action="branches")),
                _btn("📋 Мои записи", MenuCB(action="my")),
            ],
        ]
    )


def to_menu_kb(*, with_booking: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if with_booking:
        rows.append([_btn("✂️ Записаться", MenuCB(action="book"))])
    rows.append([_btn("🏠 В меню", MenuCB(action="home"))])
    return _markup(rows)


# ----------------------------------------------------------------------
# Шаг 1. Филиалы
# ----------------------------------------------------------------------


def branches_kb(branches: Sequence[Branch]) -> InlineKeyboardMarkup:
    rows = [
        [_btn(f"📍 {branch.metro}", BookingCB(action="branch_view", value=str(branch.id)))]
        for branch in branches
    ]
    rows.append(_nav_row())
    return _markup(rows)


def branch_card_kb(branch_id: int) -> InlineKeyboardMarkup:
    """Под фото-карточкой внутри сценария записи."""
    return _markup(
        [
            [_btn("✅ Записаться в этот филиал", BookingCB(action="branch_ok", value=str(branch_id)))],
            [_btn("⬅️ К списку филиалов", BookingCB(action="back", value="branches"))],
        ]
    )


def branch_info_kb(branch_id: int) -> InlineKeyboardMarkup:
    """Под карточкой в разделе «Филиалы» главного меню: запись, маршрут и возврат в меню."""
    rows = [[_btn("✂️ Записаться сюда", BookingCB(action="quick", value=str(branch_id)))]]
    route = navigation_url(branch_id)
    if route:
        rows.append([InlineKeyboardButton(text="🧭 Как добраться", url=route)])
    rows.append([_btn("🏠 В меню", MenuCB(action="home"))])
    return _markup(rows)


# ----------------------------------------------------------------------
# Шаг 2. Услуги и допы
# ----------------------------------------------------------------------


def services_kb(services: Sequence[Service], staff: Sequence[Staff]) -> InlineKeyboardMarkup:
    """Основные услуги с плашкой «от X ₽»."""
    rows = [
        [
            _btn(
                f"{SERVICE_EMOJI.get(service.kind.value, '✂️')} {service.title} · "
                f"от {format_price(baseline_price(service, staff))}",
                BookingCB(action="service", value=str(service.id)),
            )
        ]
        for service in services
    ]
    rows.append(_nav_row(back="branches"))
    return _markup(rows)


def addons_kb(
    addons: Sequence[Service],
    selected_ids: Sequence[int],
    total_from: int,
) -> InlineKeyboardMarkup:
    """Допы с чекбоксами [ ] / [✅] и кнопкой «Продолжить» с динамической суммой."""
    rows = []
    for addon in addons:
        mark = "[✅]" if addon.id in selected_ids else "[  ]"
        rows.append(
            [
                _btn(
                    f"{mark} {addon.title} (+{format_price(addon.min_price)})",
                    BookingCB(action="addon", value=str(addon.id)),
                )
            ]
        )
    rows.append([_btn(f"➡️ Продолжить · от {format_price(total_from)}", BookingCB(action="addons_done"))])
    rows.append(_nav_row(back="services"))
    return _markup(rows)


# ----------------------------------------------------------------------
# Шаг 3. Мастер
# ----------------------------------------------------------------------


def staff_kb(
    staff: Sequence[Staff],
    service: Service,
    addons: Sequence[Service],
) -> InlineKeyboardMarkup:
    """«Любой свободный мастер» + мастера по грейдам с итоговой ценой."""
    base = baseline_price(service, staff) + sum(addon.min_price for addon in addons)
    rows: list[list[InlineKeyboardButton]] = [
        [
            _btn(
                f"⚡ Любой свободный мастер · от {format_price(base)}",
                BookingCB(action="staff", value="any"),
            )
        ]
    ]
    for grade in GRADE_ORDER:
        members = [member for member in staff if member.grade is grade]
        if not members:
            continue
        price = visit_total(service, grade, addons)
        rows.append([_btn(f"── {GRADE_EMOJI[grade]} {GRADE_TITLE_PLURAL[grade]} ──", NOOP)])
        buttons = [
            _btn(
                f"{member.first_name} · {format_price(price)}",
                BookingCB(action="staff", value=str(member.id)),
            )
            for member in members
        ]
        rows.extend(_chunk(buttons, 2))
    rows.append(_nav_row(back="addons"))
    return _markup(rows)


# ----------------------------------------------------------------------
# Шаг 4. Календарь и время
# ----------------------------------------------------------------------


def _month_caption(first: dt.date, last: dt.date) -> str:
    if (first.year, first.month) == (last.year, last.month):
        return f"{MONTHS_NOMINATIVE[first.month - 1]} {first.year}"
    if first.year == last.year:
        return f"{MONTHS_NOMINATIVE[first.month - 1]} – {MONTHS_NOMINATIVE[last.month - 1]} {last.year}"
    return (
        f"{MONTHS_NOMINATIVE[first.month - 1]} {first.year} – "
        f"{MONTHS_NOMINATIVE[last.month - 1]} {last.year}"
    )


def calendar_kb(
    *,
    today: dt.date,
    days: int,
    available: set[dt.date],
) -> InlineKeyboardMarkup:
    """Календарь на ``days`` дней вперёд в виде сетки Пн–Вс.

    * число — есть свободные окна (сегодняшний день в скобках);
    * «·» — окон нет; пустые ячейки — вне диапазона.
    """
    last = today + dt.timedelta(days=days - 1)
    rows: list[list[InlineKeyboardButton]] = [
        [_btn(f"🗓 {_month_caption(today, last)}", NOOP)],
        [_btn(name, NOOP) for name in WEEKDAYS_SHORT],
    ]

    cursor = today - dt.timedelta(days=today.weekday())
    end = last + dt.timedelta(days=6 - last.weekday())
    while cursor <= end:
        row: list[InlineKeyboardButton] = []
        for offset in range(7):
            day = cursor + dt.timedelta(days=offset)
            if day < today or day > last:
                row.append(_btn(_BLANK, NOOP))
            elif day in available:
                label = f"[{day.day}]" if day == today else str(day.day)
                row.append(_btn(label, BookingCB(action="date", value=day.isoformat())))
            else:
                row.append(_btn("·", NOOP))
        rows.append(row)
        cursor += dt.timedelta(days=7)

    rows.append(_nav_row(back="staff"))
    return _markup(rows)


def times_kb(times: Sequence[dt.time]) -> InlineKeyboardMarkup:
    buttons = [
        _btn(f"{slot:%H:%M}", BookingCB(action="time", value=f"{slot:%H%M}"))
        for slot in times
    ]
    rows = _chunk(buttons, 4)
    rows.append(_nav_row(back="calendar"))
    return _markup(rows)


def price_confirm_kb(total: int) -> InlineKeyboardMarkup:
    """Согласование цены при «любом мастере»."""
    return _markup(
        [
            [_btn(f"✅ Подтверждаю ({format_price(total)})", BookingCB(action="price_ok"))],
            [_btn("🔄 Выбрать другое время", BookingCB(action="price_other"))],
        ]
    )


# ----------------------------------------------------------------------
# Шаг 5. Телефон (reply-клавиатура)
# ----------------------------------------------------------------------


def phone_request_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться контактом", request_contact=True)],
            [KeyboardButton(text=PHONE_CANCEL_TEXT)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Нажмите «Поделиться контактом»",
    )


# ----------------------------------------------------------------------
# Шаг 6. Бар и Silent Cut
# ----------------------------------------------------------------------


def beverage_kb(selected: str | None = None) -> InlineKeyboardMarkup:
    def label(code: str, text: str) -> str:
        return f"✅ {text}" if selected == code else text

    by_code = {item.code: item for item in BEVERAGES}
    rows = [
        [
            _btn(label("americano", "☕ Американо"), BookingCB(action="bev", value="americano")),
            _btn(label("cappuccino", "☕ Капучино"), BookingCB(action="bev", value="cappuccino")),
        ],
        [
            _btn(
                label("bourbon", f"{by_code['bourbon'].emoji} {by_code['bourbon'].label}"),
                BookingCB(action="bev", value="bourbon"),
            )
        ],
        [_btn(label("lemonade", "🍋 Лимонад"), BookingCB(action="bev", value="lemonade"))],
        [_btn(label(NO_BEVERAGE_CODE, "🚫 Ничего"), BookingCB(action="bev", value=NO_BEVERAGE_CODE))],
        _nav_row(back="times"),
    ]
    return _markup(rows)


def silent_kb(silent_cut: bool) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                _btn(
                    f"{'✅ ' if silent_cut else ''}🔇 Хочу стрижку в тишине",
                    BookingCB(action="silent", value="1"),
                )
            ],
            [
                _btn(
                    f"{'' if silent_cut else '✅ '}💬 Пообщаемся",
                    BookingCB(action="silent", value="0"),
                )
            ],
            [_btn("✅ Записаться", BookingCB(action="confirm"))],
            _nav_row(back="beverage"),
        ]
    )


# ----------------------------------------------------------------------
# После записи и «Мои записи»
# ----------------------------------------------------------------------


def visit_card_kb(appointment_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            [_btn("❌ Отменить запись", MyCB(action="cancel", id=appointment_id))],
            [
                _btn("📋 Мои записи", MenuCB(action="my")),
                _btn("🏠 В меню", MenuCB(action="home")),
            ],
        ]
    )


def my_appointments_kb(items: Sequence[tuple[int, str]]) -> InlineKeyboardMarkup:
    """``items`` — пары (id записи, короткая подпись «5.11 · 18:30»).

    Для каждой записи — строка из двух кнопок: перенести и отменить.
    """
    rows = [
        [
            _btn(f"🔄 {label}", MyCB(action="resched", id=appointment_id)),
            _btn(f"❌ {label}", MyCB(action="cancel", id=appointment_id)),
        ]
        for appointment_id, label in items
    ]
    rows.append([_btn("✂️ Записаться", MenuCB(action="book"))])
    rows.append([_btn("🏠 В меню", MenuCB(action="home"))])
    return _markup(rows)


def cancel_confirm_kb(appointment_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            [_btn("✅ Да, отменить", MyCB(action="cancel_yes", id=appointment_id))],
            [_btn("↩️ Оставить запись", MyCB(action="list"))],
        ]
    )
