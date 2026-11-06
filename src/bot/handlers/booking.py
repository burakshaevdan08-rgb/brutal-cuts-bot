"""Роутер пошаговой онлайн-записи (FSM, aiogram 3.x).

Шаги сценария::

    филиал → услуга → допы → мастер → дата → время
          → (согласование цены, если «любой мастер» и свободен только старший грейд)
          → телефон (пропускается, если номер уже в БД)
          → напиток → режим тишины + итоговая карточка → создание записи

Каждый экран отрисовывается функцией ``_show_*``: она сама ставит нужное
FSM-состояние, поэтому и переход вперёд, и кнопка «Назад» используют один код.
Черновик брони хранится в FSM-данных (``BookingDraft``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from contextlib import suppress
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from src.bot.common import (
    BEVERAGES_BY_CODE,
    GRADE_EMOJI,
    GRADE_ORDER,
    GRADE_TITLE_PLURAL,
    MAIN_MENU_TEXT,
    NO_BEVERAGE_CODE,
    PHONE_PROMPT,
    ContactError,
    Event,
    VisitView,
    barber_label,
    baseline_price,
    beverage_label,
    branch_card,
    catalog,
    delete_message,
    esc,
    extract_verified_phone,
    format_date_ru,
    format_phone,
    format_price,
    format_visit_card,
    send_branch_card,
    send_new,
    show_screen,
    view_from_appointment,
    visit_total,
    SERVICE_EMOJI,
)
from src.bot.keyboards.booking_kb import (
    PHONE_CANCEL_TEXT,
    BookingCB,
    MenuCB,
    addons_kb,
    beverage_kb,
    branch_card_kb,
    branches_kb,
    calendar_kb,
    main_menu_kb,
    phone_request_kb,
    price_confirm_kb,
    services_kb,
    silent_kb,
    staff_kb,
    times_kb,
    visit_card_kb,
)
from src.bot.states.booking import BookingDraft, BookingStates
from src.core.config import Settings
from src.database import AppointmentRepository, SlotTakenError, UserRepository, session_scope
from src.database.models import Appointment as DbAppointment
from src.database.models import BookingStatus, utcnow
from src.services.booking_actions import cancel_visit
from src.services.crm import (
    BaseCRMService,
    BookingData,
    Branch,
    CRMError,
    Service,
    SlotUnavailableError,
    Staff,
)

logger = logging.getLogger(__name__)

router = Router(name="booking")

BOOKING_DAYS = 14

# Защита от двойного нажатия «Записаться»: одна запись в обработке на пользователя.
_creation_locks: dict[int, asyncio.Lock] = {}


# ----------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------


class _PhoneMissingError(Exception):
    """К моменту создания записи у клиента нет подтверждённого телефона."""


@dataclass(slots=True)
class _Resolved:
    """Объекты CRM, соответствующие черновику."""

    branch: Branch
    services: list[Service]
    staff: list[Staff]
    service: Service | None
    addons: list[Service]
    master: Staff | None


def _to_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _parse_hhmm(value: str) -> dt.time | None:
    """«1830» → 18:30."""
    if len(value) != 4 or not value.isdigit():
        return None
    try:
        return dt.time(int(value[:2]), int(value[2:]))
    except ValueError:
        return None


def _today(settings: Settings) -> dt.date:
    return dt.datetime.now(settings.tzinfo).date()


async def _load_draft(state: FSMContext) -> BookingDraft:
    data = await state.get_data()
    return BookingDraft.from_dict(data.get("draft"))


async def _save_draft(state: FSMContext, draft: BookingDraft) -> None:
    await state.update_data(draft=draft.to_dict())


async def _resolve(crm: BaseCRMService, draft: BookingDraft) -> _Resolved | None:
    """Находит в CRM филиал, услуги, мастеров и выбранные элементы черновика."""
    if draft.branch_id is None:
        return None
    branches = await catalog.branches(crm)
    branch = next((item for item in branches if item.id == draft.branch_id), None)
    if branch is None:
        return None

    services, staff = await asyncio.gather(
        catalog.services(crm, branch.id),
        catalog.staff(crm, branch.id),
    )
    by_id = {service.id: service for service in services}

    service = by_id.get(draft.service_id) if draft.service_id is not None else None
    if service is not None and service.is_addon:
        service = None
    addons = [by_id[i] for i in draft.addon_ids if i in by_id and by_id[i].is_addon]
    master = next((member for member in staff if member.id == draft.staff_id), None)
    return _Resolved(branch, services, staff, service, addons, master)


def _candidates(res: _Resolved, draft: BookingDraft) -> list[Staff]:
    """Мастера, среди которых ищутся окна: все («любой») или выбранный."""
    if draft.any_staff:
        return list(res.staff)
    return [member for member in res.staff if member.id == draft.staff_id]


def _master_line(res: _Resolved, draft: BookingDraft) -> str:
    if draft.any_staff and res.master is None:
        return "⚡ Любой свободный мастер"
    if res.master is None:
        return "—"
    return barber_label(res.master.grade, res.master.full_name)


def _service_line(res: _Resolved) -> str:
    if res.service is None:
        return ""
    line = f"✂️ {esc(res.service.title)}"
    if res.addons:
        line += " + " + esc(", ".join(addon.title for addon in res.addons))
    return line


async def _restart(event: Event, state: FSMContext, notice: str | None = None) -> None:
    """Сбрасывает сценарий и показывает главное меню."""
    await state.clear()
    prefix = f"{notice}\n\n" if notice else ""
    await show_screen(event, f"{prefix}{MAIN_MENU_TEXT}", main_menu_kb())


async def _times_by_staff(
    crm: BaseCRMService,
    res: _Resolved,
    draft: BookingDraft,
    day: dt.date,
    staff_list: list[Staff],
) -> dict[int, list[dt.time]]:
    assert res.service is not None
    results = await asyncio.gather(
        *(crm.get_available_times(res.branch.id, member.id, day, res.service.id) for member in staff_list)
    )
    return {member.id: times for member, times in zip(staff_list, results, strict=True)}


# ----------------------------------------------------------------------
# Экраны сценария
# ----------------------------------------------------------------------


async def _begin(event: Event, state: FSMContext, crm: BaseCRMService, *, branch_id: int | None = None) -> None:
    """Запускает сценарий с нуля; регистрирует пользователя в БД."""
    await state.clear()
    tg_user = event.from_user
    if tg_user is None:
        return
    async with session_scope() as session:
        await UserRepository(session).get_or_create(
            telegram_id=tg_user.id,
            first_name=tg_user.first_name,
            username=tg_user.username,
        )
    if branch_id is None:
        await _show_branches(event, state, crm)
        return
    await _save_draft(state, BookingDraft(branch_id=branch_id))
    await _show_services(event, state, crm)


async def _show_branches(event: Event, state: FSMContext, crm: BaseCRMService) -> None:
    await state.set_state(BookingStates.choosing_branch)
    branches = await catalog.branches(crm)
    lines = ["📍 <b>Выберите филиал</b>", ""]
    for branch in branches:
        lines.append(f"• <b>{esc(branch.title)}</b>")
        lines.append(f"     м. {esc(branch.metro)}, {esc(branch.address)}")
    await show_screen(event, "\n".join(lines), branches_kb(branches))


async def _show_services(event: Event, state: FSMContext, crm: BaseCRMService) -> None:
    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    if res is None:
        await _restart(event, state)
        return

    await state.set_state(BookingStates.choosing_service)
    main_services = [service for service in res.services if not service.is_addon]
    lines = [f"📍 <b>{esc(res.branch.title)}</b>", "", "✂️ <b>Выберите услугу</b>", ""]
    for service in main_services:
        emoji = SERVICE_EMOJI.get(service.kind.value, "✂️")
        price = format_price(baseline_price(service, res.staff))
        lines.append(f"{emoji} <b>{esc(service.title)}</b> — <i>цена от {price}</i>")
        lines.append(f"     {esc(service.description)}")
    lines += ["", "<i>Итоговая цена зависит от грейда мастера.</i>"]
    await show_screen(event, "\n".join(lines), services_kb(main_services, res.staff))


async def _show_addons(event: Event, state: FSMContext, crm: BaseCRMService) -> None:
    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    if res is None:
        await _restart(event, state)
        return
    if res.service is None:
        await _show_services(event, state, crm)
        return

    await state.set_state(BookingStates.choosing_addons)
    addons = [service for service in res.services if service.is_addon]
    total_from = baseline_price(res.service, res.staff) + sum(addon.min_price for addon in res.addons)

    lines = [
        f"{SERVICE_EMOJI.get(res.service.kind.value, '✂️')} <b>{esc(res.service.title)}</b>",
        "",
        "➕ <b>Дополнительные опции</b>",
        "Отметьте, что добавить к визиту (можно несколько):",
        "",
    ]
    for addon in addons:
        lines.append(f"• <b>{esc(addon.title)}</b> (+{format_price(addon.min_price)}) — {esc(addon.description)}")
    lines.append("")
    if res.addons:
        lines.append(f"Выбрано: {esc(', '.join(addon.title for addon in res.addons))}")
    lines.append(f"🧾 Сумма чека: <b>от {format_price(total_from)}</b>")
    await show_screen(event, "\n".join(lines), addons_kb(addons, draft.addon_ids, total_from))


async def _show_staff(event: Event, state: FSMContext, crm: BaseCRMService) -> None:
    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    if res is None:
        await _restart(event, state)
        return
    if res.service is None:
        await _show_services(event, state, crm)
        return

    await state.set_state(BookingStates.choosing_staff)
    base = baseline_price(res.service, res.staff) + sum(addon.min_price for addon in res.addons)

    lines = ["💈 <b>Выберите мастера</b>", "", _service_line(res), ""]
    for grade in GRADE_ORDER:
        members = [member for member in res.staff if member.grade is grade]
        if not members:
            continue
        price = visit_total(res.service, grade, res.addons)
        lines.append(f"{GRADE_EMOJI[grade]} <b>{GRADE_TITLE_PLURAL[grade]}</b> — {format_price(price)}")
        for member in members:
            lines.append(f"   • {esc(member.full_name)} · стаж {member.experience_label} · ★ {member.rating:.1f}")
        lines.append("")
    lines.append(f"⚡ <b>Любой свободный мастер</b> — цена от {format_price(base)}.")
    lines.append(
        "<i>Если на выбранное время будет свободен только мастер старшего грейда, "
        "мы покажем итоговую цену и попросим подтверждение.</i>"
    )
    await show_screen(event, "\n".join(lines), staff_kb(res.staff, res.service, res.addons))


async def _show_calendar(
    event: Event,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
    notice: str | None = None,
) -> None:
    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    if res is None:
        await _restart(event, state)
        return
    staff_list = _candidates(res, draft)
    if res.service is None or not staff_list:
        await _show_staff(event, state, crm)
        return

    await state.set_state(BookingStates.choosing_date)
    results = await asyncio.gather(
        *(
            crm.get_available_dates(res.branch.id, member.id, res.service.id, days=BOOKING_DAYS)
            for member in staff_list
        )
    )
    available = {day for days in results for day in days}

    lines = ["🗓 <b>Выберите дату</b>", "", _service_line(res), f"💈 {_master_line(res, draft)}", ""]
    if notice:
        lines += [notice, ""]
    if available:
        lines.append("<i>Число — есть свободные окна, «·» — всё занято.</i>")
    else:
        lines.append(
            f"😔 На ближайшие {BOOKING_DAYS} дней свободных окон нет. "
            "Вернитесь назад и выберите другого мастера."
        )
    markup = calendar_kb(today=_today(settings), days=BOOKING_DAYS, available=available)
    await show_screen(event, "\n".join(lines), markup)


async def _show_times(
    event: Event,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
    notice: str | None = None,
) -> None:
    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    if res is None:
        await _restart(event, state)
        return
    staff_list = _candidates(res, draft)
    if res.service is None or not staff_list or draft.date is None:
        await _show_calendar(event, state, crm, settings)
        return

    day = dt.date.fromisoformat(draft.date)
    by_staff = await _times_by_staff(crm, res, draft, day, staff_list)
    times = sorted({slot for slots in by_staff.values() for slot in slots})
    if not times:
        draft.reset_slot()
        await _save_draft(state, draft)
        await _show_calendar(
            event, state, crm, settings,
            notice="⚠️ На выбранную дату свободных окон не осталось — выберите другой день.",
        )
        return

    await state.set_state(BookingStates.choosing_time)
    lines = [
        "🕐 <b>Выберите время</b>",
        "",
        f"🗓 {format_date_ru(day)}",
        _service_line(res),
        f"💈 {_master_line(res, draft)}",
        "",
    ]
    if notice:
        lines += [notice, ""]
    if draft.any_staff:
        lines.append("<i>Мастера подберём автоматически под выбранное время.</i>")
    await show_screen(event, "\n".join(lines), times_kb(times))


async def _show_price_agreement(
    event: Event,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """Диалог согласования цены: «любой мастер», но свободен только старший грейд."""
    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    if (
        res is None
        or res.service is None
        or res.master is None
        or draft.date is None
        or draft.time is None
    ):
        await _restart(event, state)
        return

    await state.set_state(BookingStates.confirming_price)
    master = res.master
    day = dt.date.fromisoformat(draft.date)
    service_price = res.service.price_for(master.grade)
    total = visit_total(res.service, master.grade, res.addons)
    base = baseline_price(res.service, res.staff)

    lines = [
        f"🗓 <b>{format_date_ru(day)}</b>",
        "",
        f"В {draft.time} вас готов принять {barber_label(master.grade, master.first_name)}.",
        f"Стоимость услуги с учетом грейда: <b>{format_price(service_price)}</b>.",
    ]
    if res.addons:
        addons_cost = total - service_price
        lines.append(
            f"Допы ({esc(', '.join(addon.title for addon in res.addons))}): "
            f"+{format_price(addons_cost)}. Итого: <b>{format_price(total)}</b>."
        )
    lines += [
        "",
        f"<i>На это время нет свободных барберов по базовой цене (от {format_price(base)}).</i>",
        "",
        "Подтверждаем запись или выбрать другое время?",
    ]
    await show_screen(event, "\n".join(lines), price_confirm_kb(total))


async def _after_slot(
    event: CallbackQuery,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """Слот выбран (и цена согласована). Если номера нет — запрашиваем контакт."""
    tg_user = event.from_user
    async with session_scope() as session:
        user = await UserRepository(session).get(tg_user.id)
    if user is not None and user.phone:
        await _show_beverage(event, state, crm)
        return

    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    if res is None or draft.date is None or draft.time is None:
        await _restart(event, state)
        return

    await state.set_state(BookingStates.requesting_phone)
    day = dt.date.fromisoformat(draft.date)
    # Старое сообщение сворачиваем (без кнопок), а запрос контакта шлём новым:
    # reply-клавиатуру нельзя прикрепить через редактирование.
    await show_screen(
        event,
        "✅ <b>Время выбрано</b>\n"
        f"🗓 {format_date_ru(day)} · {draft.time}\n"
        f"💈 {_master_line(res, draft)}",
    )
    await send_new(event, PHONE_PROMPT, phone_request_kb())


async def _show_beverage(event: Event, state: FSMContext, crm: BaseCRMService) -> None:
    draft = await _load_draft(state)
    await state.set_state(BookingStates.choosing_beverage)
    text = (
        "🍹 <b>Барное меню</b>\n\n"
        "Что приготовить к вашему визиту?\n\n"
        "<i>🥃 Бурбон — только для гостей 18+.</i>"
    )
    await show_screen(event, text, beverage_kb(draft.beverage))


def _view_from_draft(res: _Resolved, draft: BookingDraft, settings: Settings) -> VisitView:
    assert res.service is not None and res.master is not None
    assert draft.date is not None and draft.time is not None
    start = dt.datetime.combine(
        dt.date.fromisoformat(draft.date),
        dt.time.fromisoformat(draft.time),
        tzinfo=settings.tzinfo,
    )
    return VisitView(
        branch_title=res.branch.title,
        branch_place=f"{res.branch.address} (м. {res.branch.metro})",
        staff_name=res.master.full_name,
        grade=res.master.grade,
        service_name=res.service.title,
        addons=tuple(addon.title for addon in res.addons),
        start=start,
        total_price=visit_total(res.service, res.master.grade, res.addons),
        beverage=beverage_label(draft.beverage),
        silent_cut=draft.silent_cut,
    )


async def _show_summary(
    event: Event,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
    notice: str | None = None,
) -> None:
    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    if (
        res is None
        or res.service is None
        or res.master is None
        or draft.date is None
        or draft.time is None
    ):
        await _restart(event, state)
        return

    await state.set_state(BookingStates.choosing_silent)
    card = format_visit_card(_view_from_draft(res, draft, settings), header="📝 <b>Проверьте запись</b>")
    parts = [card]
    if notice:
        parts.insert(0, notice + "\n")
    parts.append("\n<i>Выберите формат визита и нажмите «Записаться».</i>")
    await show_screen(event, "\n".join(parts), silent_kb(draft.silent_cut))


async def start_reschedule(
    event: CallbackQuery,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
    appointment: DbAppointment,
) -> str | None:
    """Перенос визита: запускает сценарий с уже выбранными филиалом, услугой, допами и мастером.

    Клиенту остаётся выбрать новые дату и время. Прежняя запись отменяется только
    после успешного создания новой (см. ``_finalize_booking``).
    Возвращает текст ошибки, если перенос невозможен, иначе ``None``.
    """
    if appointment.status not in (BookingStatus.CREATED, BookingStatus.CONFIRMED):
        return "Эта запись уже неактуальна."
    if appointment.datetime_start <= utcnow():
        return "Визит уже начался, перенести его нельзя."

    services, staff = await asyncio.gather(
        catalog.services(crm, appointment.branch_id),
        catalog.staff(crm, appointment.branch_id),
    )
    service = next(
        (item for item in services if not item.is_addon and item.title == appointment.service_name),
        None,
    )
    master = next((member for member in staff if member.id == appointment.staff_id), None)
    if service is None or master is None:
        return "Услуга или мастер изменились. Запишитесь заново через меню."

    addon_titles = set(appointment.addons or [])
    beverage_code = next(
        (item.code for item in BEVERAGES_BY_CODE.values() if item.label == appointment.beverage),
        NO_BEVERAGE_CODE,
    )
    await state.clear()
    await _save_draft(
        state,
        BookingDraft(
            branch_id=appointment.branch_id,
            service_id=service.id,
            addon_ids=[item.id for item in services if item.is_addon and item.title in addon_titles],
            any_staff=False,
            staff_id=master.id,
            beverage=beverage_code,
            silent_cut=appointment.silent_cut,
            reschedule_of=appointment.id,
        ),
    )
    await _show_calendar(
        event, state, crm, settings,
        notice="🔄 <b>Перенос визита.</b> Выберите новые дату и время — прежняя запись будет отменена после подтверждения.",
    )
    return None


# ----------------------------------------------------------------------
# Вход в сценарий и общие действия
# ----------------------------------------------------------------------


@router.message(Command("book"))
async def cmd_book(message: Message, state: FSMContext, crm: BaseCRMService) -> None:
    await _begin(message, state, crm)


@router.callback_query(MenuCB.filter(F.action == "book"))
async def on_menu_book(callback: CallbackQuery, state: FSMContext, crm: BaseCRMService) -> None:
    await callback.answer()
    await _begin(callback, state, crm)


@router.callback_query(BookingCB.filter(F.action == "quick"))
async def on_quick_branch(
    callback: CallbackQuery,
    callback_data: BookingCB,
    state: FSMContext,
    crm: BaseCRMService,
) -> None:
    """«Записаться сюда» из карточки филиала в главном меню."""
    branch_id = _to_int(callback_data.value)
    branches = await catalog.branches(crm)
    if branch_id is None or all(branch.id != branch_id for branch in branches):
        await callback.answer("Филиал не найден.", show_alert=True)
        return
    await callback.answer()
    await _begin(callback, state, crm, branch_id=branch_id)


@router.callback_query(BookingCB.filter(F.action == "cancel"))
async def on_cancel_flow(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Запись отменена")
    await _restart(callback, state)


@router.callback_query(BookingCB.filter(F.action == "noop"))
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(BookingCB.filter(F.action == "back"))
async def on_back(
    callback: CallbackQuery,
    callback_data: BookingCB,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    """Кнопка «Назад»: ``value`` — целевой шаг."""
    target = callback_data.value
    draft = await _load_draft(state)
    if target != "branches" and draft.branch_id is None:
        await callback.answer("Сессия записи устарела.", show_alert=True)
        await _restart(callback, state)
        return
    await callback.answer()

    if target == "branches":
        await _show_branches(callback, state, crm)
    elif target == "services":
        await _show_services(callback, state, crm)
    elif target == "addons":
        await _show_addons(callback, state, crm)
    elif target == "staff":
        await _show_staff(callback, state, crm)
    elif target == "calendar":
        draft.reset_slot()
        await _save_draft(state, draft)
        await _show_calendar(callback, state, crm, settings)
    elif target == "times":
        draft.time = None
        draft.price_confirmed = False
        if draft.any_staff:
            draft.staff_id = None
        await _save_draft(state, draft)
        await _show_times(callback, state, crm, settings)
    elif target == "beverage":
        await _show_beverage(callback, state, crm)
    else:
        await _restart(callback, state)


# ----------------------------------------------------------------------
# 1. Филиал
# ----------------------------------------------------------------------


@router.callback_query(BookingCB.filter(F.action == "branch_view"), BookingStates.choosing_branch)
async def on_branch_view(
    callback: CallbackQuery,
    callback_data: BookingCB,
    crm: BaseCRMService,
) -> None:
    """Карточка филиала с фото входной группы."""
    branch_id = _to_int(callback_data.value)
    branch = next((item for item in await catalog.branches(crm) if item.id == branch_id), None)
    if branch is None:
        await callback.answer("Филиал не найден.", show_alert=True)
        return
    await callback.answer()

    staff = await catalog.staff(crm, branch.id)
    caption = branch_card(branch, staff)
    markup = branch_card_kb(branch.id)
    if isinstance(callback.message, Message):
        await send_branch_card(callback.message, branch, caption, markup)
        await delete_message(callback)
    else:
        await send_new(callback, caption, markup)


@router.callback_query(BookingCB.filter(F.action == "branch_ok"), BookingStates.choosing_branch)
async def on_branch_selected(
    callback: CallbackQuery,
    callback_data: BookingCB,
    state: FSMContext,
    crm: BaseCRMService,
) -> None:
    branch_id = _to_int(callback_data.value)
    branch = next((item for item in await catalog.branches(crm) if item.id == branch_id), None)
    if branch is None:
        await callback.answer("Филиал не найден.", show_alert=True)
        return
    await callback.answer()
    await _save_draft(state, BookingDraft(branch_id=branch.id))
    await _show_services(callback, state, crm)


# ----------------------------------------------------------------------
# 2. Услуга и допы
# ----------------------------------------------------------------------


@router.callback_query(BookingCB.filter(F.action == "service"), BookingStates.choosing_service)
async def on_service_selected(
    callback: CallbackQuery,
    callback_data: BookingCB,
    state: FSMContext,
    crm: BaseCRMService,
) -> None:
    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    service_id = _to_int(callback_data.value)
    service = next(
        (item for item in (res.services if res else []) if item.id == service_id and not item.is_addon),
        None,
    )
    if service is None:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return
    await callback.answer()

    draft.service_id = service.id
    draft.addon_ids = []
    draft.any_staff = False
    draft.staff_id = None
    draft.reset_slot()
    await _save_draft(state, draft)
    await _show_addons(callback, state, crm)


@router.callback_query(BookingCB.filter(F.action == "addon"), BookingStates.choosing_addons)
async def on_addon_toggled(
    callback: CallbackQuery,
    callback_data: BookingCB,
    state: FSMContext,
    crm: BaseCRMService,
) -> None:
    """Тоггл допа [ ] ↔ [✅] с пересчётом суммы на кнопке «Продолжить»."""
    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    addon_id = _to_int(callback_data.value)
    addon = next(
        (item for item in (res.services if res else []) if item.id == addon_id and item.is_addon),
        None,
    )
    if addon is None:
        await callback.answer("Опция не найдена.", show_alert=True)
        return
    await callback.answer()

    if addon.id in draft.addon_ids:
        draft.addon_ids.remove(addon.id)
    else:
        draft.addon_ids.append(addon.id)
    await _save_draft(state, draft)
    await _show_addons(callback, state, crm)


@router.callback_query(BookingCB.filter(F.action == "addons_done"), BookingStates.choosing_addons)
async def on_addons_done(callback: CallbackQuery, state: FSMContext, crm: BaseCRMService) -> None:
    await callback.answer()
    await _show_staff(callback, state, crm)


# ----------------------------------------------------------------------
# 3. Мастер
# ----------------------------------------------------------------------


@router.callback_query(BookingCB.filter(F.action == "staff"), BookingStates.choosing_staff)
async def on_staff_selected(
    callback: CallbackQuery,
    callback_data: BookingCB,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    if res is None:
        await callback.answer("Сессия записи устарела.", show_alert=True)
        await _restart(callback, state)
        return

    if callback_data.value == "any":
        draft.any_staff = True
        draft.staff_id = None
    else:
        staff_id = _to_int(callback_data.value)
        member = next((item for item in res.staff if item.id == staff_id), None)
        if member is None:
            await callback.answer("Мастер не найден.", show_alert=True)
            return
        draft.any_staff = False
        draft.staff_id = member.id
    await callback.answer()

    draft.reset_slot()
    await _save_draft(state, draft)
    await _show_calendar(callback, state, crm, settings)


# ----------------------------------------------------------------------
# 4. Календарь, время и динамический ценник
# ----------------------------------------------------------------------


@router.callback_query(BookingCB.filter(F.action == "date"), BookingStates.choosing_date)
async def on_date_selected(
    callback: CallbackQuery,
    callback_data: BookingCB,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    try:
        day = dt.date.fromisoformat(callback_data.value)
    except ValueError:
        await callback.answer("Некорректная дата.", show_alert=True)
        return
    today = _today(settings)
    if not today <= day < today + dt.timedelta(days=BOOKING_DAYS):
        await callback.answer("Эта дата недоступна для записи.", show_alert=True)
        return
    await callback.answer()

    draft = await _load_draft(state)
    draft.reset_slot()
    draft.date = day.isoformat()
    await _save_draft(state, draft)
    await _show_times(callback, state, crm, settings)


@router.callback_query(BookingCB.filter(F.action == "time"), BookingStates.choosing_time)
async def on_time_selected(
    callback: CallbackQuery,
    callback_data: BookingCB,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    chosen = _parse_hhmm(callback_data.value)
    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    if chosen is None or res is None or res.service is None or draft.date is None:
        await callback.answer("Сессия записи устарела.", show_alert=True)
        await _restart(callback, state)
        return
    await callback.answer()

    day = dt.date.fromisoformat(draft.date)
    staff_list = _candidates(res, draft)
    # Слот перепроверяется в CRM: пока клиент думал, его могли занять.
    by_staff = await _times_by_staff(crm, res, draft, day, staff_list)
    free = [member for member in staff_list if chosen in by_staff[member.id]]
    if not free:
        await _show_times(
            event=callback, state=state, crm=crm, settings=settings,
            notice="⚠️ Это время только что заняли. Выберите другое.",
        )
        return

    # «Любой мастер»: назначаем самого доступного по цене (младший грейд первым).
    assignee = min(free, key=lambda member: (res.service.price_for(member.grade), member.id))
    draft.staff_id = assignee.id
    draft.time = f"{chosen:%H:%M}"
    draft.price_confirmed = False
    await _save_draft(state, draft)

    needs_agreement = (
        draft.any_staff
        and res.service.price_for(assignee.grade) > baseline_price(res.service, res.staff)
    )
    if needs_agreement:
        await _show_price_agreement(callback, state, crm, settings)
    else:
        await _after_slot(callback, state, crm, settings)


@router.callback_query(BookingCB.filter(F.action == "price_ok"), BookingStates.confirming_price)
async def on_price_confirmed(
    callback: CallbackQuery,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    await callback.answer()
    draft = await _load_draft(state)
    draft.price_confirmed = True
    await _save_draft(state, draft)
    await _after_slot(callback, state, crm, settings)


@router.callback_query(BookingCB.filter(F.action == "price_other"), BookingStates.confirming_price)
async def on_price_declined(
    callback: CallbackQuery,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    await callback.answer()
    draft = await _load_draft(state)
    draft.time = None
    draft.price_confirmed = False
    draft.staff_id = None  # назначенный мастер сбрасывается, режим «любой» сохраняется
    await _save_draft(state, draft)
    await _show_times(callback, state, crm, settings)


# ----------------------------------------------------------------------
# 5. Авторизация по номеру телефона
# ----------------------------------------------------------------------


@router.message(BookingStates.requesting_phone, F.contact)
async def on_contact_shared(
    message: Message,
    state: FSMContext,
    crm: BaseCRMService,
) -> None:
    try:
        phone = extract_verified_phone(message)
    except ContactError as exc:
        await message.answer(f"⚠️ {esc(exc)}", reply_markup=phone_request_kb())
        return

    tg_user = message.from_user
    assert tg_user is not None  # extract_verified_phone уже проверил отправителя
    async with session_scope() as session:
        users = UserRepository(session)
        await users.get_or_create(
            telegram_id=tg_user.id,
            first_name=tg_user.first_name,
            username=tg_user.username,
        )
        await users.update_phone(tg_user.id, phone)

    await message.answer(
        f"✅ Номер {format_phone(phone)} подтверждён.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await _show_beverage(message, state, crm)


@router.message(BookingStates.requesting_phone, F.text == PHONE_CANCEL_TEXT)
async def on_phone_cancelled(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Запись отменена.", reply_markup=ReplyKeyboardRemove())
    await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_kb())


@router.message(BookingStates.requesting_phone)
async def on_phone_unexpected(message: Message) -> None:
    await message.answer(
        "Чтобы продолжить, нажмите кнопку «📱 Поделиться контактом» внизу экрана.",
        reply_markup=phone_request_kb(),
    )


# ----------------------------------------------------------------------
# 6. Барное меню и Silent Cut
# ----------------------------------------------------------------------


@router.callback_query(BookingCB.filter(F.action == "bev"), BookingStates.choosing_beverage)
async def on_beverage_selected(
    callback: CallbackQuery,
    callback_data: BookingCB,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    code = callback_data.value
    if code != NO_BEVERAGE_CODE and code not in BEVERAGES_BY_CODE:
        await callback.answer("Напиток не найден.", show_alert=True)
        return
    await callback.answer()

    draft = await _load_draft(state)
    draft.beverage = code
    await _save_draft(state, draft)
    await _show_summary(callback, state, crm, settings)


@router.callback_query(BookingCB.filter(F.action == "silent"), BookingStates.choosing_silent)
async def on_silent_toggled(
    callback: CallbackQuery,
    callback_data: BookingCB,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    await callback.answer()
    draft = await _load_draft(state)
    draft.silent_cut = callback_data.value == "1"
    await _save_draft(state, draft)
    await _show_summary(callback, state, crm, settings)


# ----------------------------------------------------------------------
# 7. Создание записи
# ----------------------------------------------------------------------


async def _create_booking(
    crm: BaseCRMService,
    tg_id: int,
    client_name: str,
    res: _Resolved,
    draft: BookingDraft,
    settings: Settings,
) -> DbAppointment:
    """Создаёт запись в CRM, затем сохраняет её в БД.

    Слот проверяется дважды независимо от CRM: до обращения к ней (чтобы не создавать
    лишнюю запись) и при сохранении в БД. Если у мастера на это время уже есть активная
    запись (created / confirmed), бросается ``SlotUnavailableError``.
    Если сохранение в БД не удалось, запись в CRM отменяется (компенсация),
    чтобы слот не оставался заблокированным без записи у нас.
    """
    assert res.service is not None and res.master is not None
    assert draft.date is not None and draft.time is not None

    starts_at = dt.datetime.combine(
        dt.date.fromisoformat(draft.date),
        dt.time.fromisoformat(draft.time),
        tzinfo=settings.tzinfo,
    )
    ends_at = starts_at + dt.timedelta(minutes=res.service.duration_minutes)

    async with session_scope() as session:
        user = await UserRepository(session).get(tg_id)
        conflict = await AppointmentRepository(session).find_conflict(
            branch_id=res.branch.id,
            staff_id=res.master.id,
            start=starts_at,
            end=ends_at,
        )
    if user is None or not user.phone:
        raise _PhoneMissingError
    if conflict is not None:
        raise SlotUnavailableError(
            f"У мастера {res.master.first_name} на это время уже есть запись (№{conflict.crm_record_id})."
        )

    label = beverage_label(draft.beverage)
    comment = (
        f"Напиток: {label or 'без напитка'}; "
        f"режим: {'тишина (silent cut)' if draft.silent_cut else 'общение'}"
    )
    booking = BookingData(
        branch_id=res.branch.id,
        staff_id=res.master.id,
        service_id=res.service.id,
        date=dt.date.fromisoformat(draft.date),
        time=dt.time.fromisoformat(draft.time),
        client_name=user.first_name or client_name,
        client_phone=user.phone,
        addon_ids=tuple(addon.id for addon in res.addons),
        telegram_id=tg_id,
        comment=comment,
    )
    record = await crm.create_appointment(booking)

    try:
        async with session_scope() as session:
            return await AppointmentRepository(session).create(
                user_id=tg_id,
                crm_record_id=record.id,
                branch_id=res.branch.id,
                staff_id=res.master.id,
                staff_name=res.master.full_name,
                staff_grade=res.master.grade.value,
                service_name=res.service.title,
                addons=[addon.title for addon in res.addons],
                datetime_start=record.starts_at,
                datetime_end=record.ends_at,
                total_price=record.total_price,
                beverage=label,
                silent_cut=draft.silent_cut,
            )
    except SlotTakenError as exc:
        # БД отклонила слот (например, запись с таким временем появилась параллельно):
        # откатываем запись в CRM и сообщаем клиенту, что время занято.
        logger.warning("Слот занят по данным БД, отменяем запись #%s в CRM: %s", record.id, exc)
        with suppress(Exception):
            await crm.cancel_appointment(record.id)
        raise SlotUnavailableError(str(exc)) from exc
    except Exception:
        logger.exception("Не удалось сохранить запись #%s в БД, отменяем её в CRM", record.id)
        with suppress(Exception):
            await crm.cancel_appointment(record.id)
        raise


@router.callback_query(BookingCB.filter(F.action == "confirm"), BookingStates.choosing_silent)
async def on_confirm_booking(
    callback: CallbackQuery,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    tg_user = callback.from_user
    lock = _creation_locks.setdefault(tg_user.id, asyncio.Lock())
    if lock.locked():
        await callback.answer("Запись уже оформляется…")
        return

    async with lock:
        try:
            await _finalize_booking(callback, state, crm, settings)
        finally:
            _creation_locks.pop(tg_user.id, None)


async def _finalize_booking(
    callback: CallbackQuery,
    state: FSMContext,
    crm: BaseCRMService,
    settings: Settings,
) -> None:
    if await state.get_state() != BookingStates.choosing_silent.state:
        await callback.answer()
        return

    draft = await _load_draft(state)
    res = await _resolve(crm, draft)
    if (
        res is None
        or res.service is None
        or res.master is None
        or draft.date is None
        or draft.time is None
    ):
        await callback.answer("Сессия записи устарела.", show_alert=True)
        await _restart(callback, state)
        return

    await callback.answer()
    await state.set_state(BookingStates.creating)
    await show_screen(callback, "⏳ <b>Оформляем вашу запись…</b>")

    tg_user = callback.from_user
    try:
        saved = await _create_booking(crm, tg_user.id, tg_user.full_name, res, draft, settings)
    except SlotUnavailableError:
        draft.time = None
        draft.price_confirmed = False
        if draft.any_staff:
            draft.staff_id = None
        await _save_draft(state, draft)
        await _show_times(
            callback, state, crm, settings,
            notice="⚠️ Это время успели занять. Выберите другое.",
        )
        return
    except _PhoneMissingError:
        await _after_slot(callback, state, crm, settings)
        return
    except CRMError as exc:
        logger.warning("CRM отклонила запись: %s", exc)
        await _show_summary(callback, state, crm, settings, notice=f"❌ {esc(exc)}")
        return
    except Exception:
        logger.exception("Не удалось оформить запись")
        await _show_summary(
            callback, state, crm, settings,
            notice="❌ Не удалось оформить запись. Попробуйте ещё раз через минуту.",
        )
        return

    header = "🎉 <b>Запись создана!</b>"
    notes: list[str] = []
    if draft.reschedule_of is not None:
        # Перенос: новая запись уже создана, теперь освобождаем прежний слот.
        header = "🔄 <b>Запись перенесена!</b>"
        cancelled = await cancel_visit(crm, tg_user.id, draft.reschedule_of)
        if cancelled.ok:
            notes += ["♻️ Прежняя запись отменена, её время освобождено.", ""]
        else:
            notes += [
                f"⚠️ Прежнюю запись не удалось отменить автоматически ({esc(cancelled.message)}). "
                "Отмените её в разделе «📋 Мои записи».",
                "",
            ]

    view = view_from_appointment(saved, res.branch, settings.tzinfo)
    lines = [
        format_visit_card(view, header=header),
        "",
        *notes,
        f'💸 <a href="{esc(res.master.netmonet_url)}">Чаевые мастеру через «нетмонет»</a>',
        "⏰ Приходите за 5–10 минут до начала.",
        "Отменить запись можно в разделе «📋 Мои записи».",
    ]
    await state.clear()
    await show_screen(callback, "\n".join(lines), visit_card_kb(saved.id), no_preview=True)


# ----------------------------------------------------------------------
# Устаревшие кнопки (резервный обработчик — регистрируется последним)
# ----------------------------------------------------------------------


@router.callback_query(BookingCB.filter())
async def on_stale_button(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка из другого шага или из сессии, потерянной при перезапуске."""
    await callback.answer("Эта кнопка устарела. Начните запись заново через меню.", show_alert=True)
    if await state.get_state() is None:
        await show_screen(callback, MAIN_MENU_TEXT, main_menu_kb())
