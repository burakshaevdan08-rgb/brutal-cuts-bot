"""Демо-наполнение базы BRUTAL CUTS: три записи «под ключ» для проверки всех фоновых сценариев.

Запуск из корня проекта::

    python -m scripts.seed_demo_data                 # первый запуск
    python -m scripts.seed_demo_data --reset         # пересоздать демо-набор заново
    python -m scripts.seed_demo_data --user-id 123456789 --name "Алексей"

Что создаётся (клиент — ADMIN_ID из .env, либо ``--user-id``):

===  =========================  ===========================================================
 №   Когда                      Что проверяем
===  =========================  ===========================================================
 1   через 1,5 часа             напоминание «за 2 часа»: адрес, парковка, код шлагбаума
 2   завтра в 16:00            напоминание «за 24 часа»: кнопки «Буду точно / Перенести /
                                Отменить» (запись в статусе «создана» — её можно подтвердить)
 3   закончилась 50 минут      запрос оценки визита (Review Gatekeeping) — срабатывает
     назад                     при первом же проходе планировщика
===  =========================  ===========================================================

Как это устроено:

* записи пишутся только через ``UserRepository`` и ``AppointmentRepository`` — теми же
  вызовами, что и боевой сценарий бронирования;
* каждая запись сначала регистрируется в Mock-CRM (``import_appointment``), и в БД
  сохраняется настоящий номер записи в CRM — слоты в CRM заняты, отмена и подтверждение
  кнопками работают так же, как у живой записи;
* в конце запускается ``sync_mock_crm_with_database`` — тот же механизм, что при старте бота,
  как проверка, что БД и Mock-CRM согласованы;
* запись №3 намеренно остаётся в статусе «подтверждена», а не «завершена»: планировщик
  запрашивает оценку только по активным записям и сам переводит визит в «завершён»
  после отправки. Если сразу пометить её завершённой, запрос оценки не уйдёт.

Скрипт идемпотентен в безопасном смысле: если у клиента уже есть записи, он ничего не
меняет и подсказывает ``--reset``. Время всех записей считается от момента запуска.

Важно: Mock-CRM хранит состояние в памяти процесса бота. Запускайте скрипт ДО старта бота
или перезапустите бота после него — при старте он сам подтянет записи из БД в CRM.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import Final
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from src.bot.common import beverage_label
from src.core.config import Settings, get_settings
from src.database import (
    ACTIVE_STATUSES,
    AppointmentRepository,
    BookingStatus,
    UserRepository,
    close_db,
    init_db,
    session_scope,
)
from src.services.crm import (
    Appointment as CrmAppointment,
)
from src.services.crm import (
    AppointmentStatus,
    BaseCRMService,
    Branch,
    CRMError,
    MockCRMService,
    Service,
    Staff,
    get_crm_service,
)
from src.services.crm.mock_sync import SyncReport, sync_mock_crm_with_database

#: Демо-номер Mock-CRM (8 визитов, 1240 бонусов, уровень «Свой», скидка 5%):
#: на нём удобно показывать баланс лояльности. Подставляется, только если у клиента нет своего номера.
DEMO_PHONE: Final[str] = "+79990000002"
DEFAULT_GUEST_NAME: Final[str] = "Алексей"


class SeedError(Exception):
    """Понятная пользователю причина, по которой наполнение не удалось."""


# ----------------------------------------------------------------------
# План: какие записи создаём
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VisitSpec:
    """Описание демо-записи. Время считается от ``anchor`` (начало или конец визита)."""

    key: str
    scenario: str
    branch_id: int
    #: Предпочтительный мастер и запасные: берётся первый, у кого слот свободен.
    staff_ids: tuple[int, ...]
    service_title: str
    addon_titles: tuple[str, ...]
    beverage_code: str | None
    silent_cut: bool
    status: BookingStatus
    anchor: dt.datetime
    anchor_is_end: bool = False


def build_plan(now: dt.datetime, tz: ZoneInfo) -> list[VisitSpec]:
    """Три демо-записи относительно ``now`` (aware-datetime); ``tz`` — часовой пояс клуба."""
    now = now.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)
    tomorrow = now.astimezone(tz).date() + dt.timedelta(days=1)
    return [
        # №1 — напоминание за 2 часа. Белорусская: шлагбаум, парковка P1–P4.
        # «Подтверждена»: гость уже нажал «Буду точно».
        VisitSpec(
            key="reminder_2h",
            scenario="Напоминание за 2 часа: адрес, парковка и код шлагбаума",
            branch_id=1,
            staff_ids=(3, 5, 6),  # Богдан (Pro), запасные — Илья, Влад
            service_title="Мужская стрижка",
            addon_titles=(),
            beverage_code="americano",
            silent_cut=False,
            status=BookingStatus.CONFIRMED,
            anchor=now + dt.timedelta(minutes=90),
        ),
        # №2 — напоминание за 24 часа. Статус «создана»: на экране будут все три кнопки.
        # Бауманская, Шеф-барбер, комплекс на 2 часа, Silent Cut, бурбон из барного меню.
        VisitSpec(
            key="reminder_24h",
            scenario="Напоминание за 24 часа: «Буду точно» / «Перенести» / «Отменить»",
            branch_id=2,
            staff_ids=(7, 8),  # Марк (Chef), запасной — Артем
            service_title="Комплекс: стрижка + борода",
            addon_titles=("Патчи", "Маска-детокс"),
            beverage_code="bourbon",
            silent_cut=True,
            status=BookingStatus.CREATED,
            anchor=dt.datetime.combine(tomorrow, dt.time(16, 0), tzinfo=tz),
        ),
        # №3 — визит закончился 50 минут назад (порог запроса оценки — 45 минут).
        VisitSpec(
            key="review",
            scenario="Запрос оценки визита (Review Gatekeeping): уходит сразу",
            branch_id=1,
            staff_ids=(4, 3, 5),  # Денис (Pro), запасные — Богдан, Илья
            service_title="Моделирование бороды",
            addon_titles=("Воск",),
            beverage_code="lemonade",
            silent_cut=False,
            status=BookingStatus.CONFIRMED,
            anchor=now - dt.timedelta(minutes=50),
            anchor_is_end=True,
        ),
    ]


# ----------------------------------------------------------------------
# Вспомогательные структуры
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Catalog:
    """Справочники одного филиала, нужные для расчёта цены и имён."""

    branch: Branch
    services: dict[str, Service]
    staff: dict[int, Staff]


@dataclass(frozen=True, slots=True)
class _Profile:
    """Профиль клиента, который нужно сохранить при ``--reset``."""

    first_name: str
    username: str | None
    phone: str | None


@dataclass(frozen=True, slots=True)
class SeededVisit:
    """Итог создания одной записи (для итоговой сводки)."""

    spec: VisitSpec
    db_id: int
    crm_id: int
    branch_title: str
    staff_name: str
    grade: str
    service_name: str
    addons: tuple[str, ...]
    start: dt.datetime
    end: dt.datetime
    total_price: int
    status: BookingStatus


async def _load_catalog(crm: BaseCRMService, branch_id: int) -> _Catalog:
    branch = next((item for item in await crm.get_branches() if item.id == branch_id), None)
    if branch is None:
        raise SeedError(f"В CRM нет филиала №{branch_id}: справочники Mock-CRM изменились?")
    services = {item.title: item for item in await crm.get_services(branch_id)}
    staff = {item.id: item for item in await crm.get_staff(branch_id)}
    return _Catalog(branch=branch, services=services, staff=staff)


def _visit_times(spec: VisitSpec, service: Service) -> tuple[dt.datetime, dt.datetime]:
    duration = dt.timedelta(minutes=service.duration_minutes)
    if spec.anchor_is_end:
        return spec.anchor - duration, spec.anchor
    return spec.anchor, spec.anchor + duration


async def _cancel_in_crm(crm: MockCRMService, record_ids: list[int]) -> int:
    """Отменяет записи в CRM, освобождая слоты. Ошибки CRM (запись уже началась и т. п.) не фатальны."""
    cancelled = 0
    for record_id in record_ids:
        with suppress(CRMError):
            await crm.cancel_appointment(record_id)
            cancelled += 1
    return cancelled


# ----------------------------------------------------------------------
# Сброс и наполнение
# ----------------------------------------------------------------------


async def _reset_guest(crm: MockCRMService, user_id: int) -> tuple[_Profile | None, int]:
    """Удаляет клиента вместе с записями и отзывами (ON DELETE CASCADE) и освобождает слоты в CRM.

    Возвращает прежний профиль (чтобы не потерять имя и телефон) и число освобождённых слотов.
    Записи, которые уже начались, CRM отменить не даёт — они остаются в прошлом и никому не мешают.
    """
    async with session_scope() as session:
        users = UserRepository(session)
        user = await users.get(user_id)
        if user is None:
            return None, 0
        rows = await AppointmentRepository(session).list_for_user(user_id, limit=1000)
        profile = _Profile(user.first_name, user.username, user.phone)
        active_ids = [
            int(row.crm_record_id)
            for row in rows
            if row.status in ACTIVE_STATUSES and row.crm_record_id.isdigit()
        ]
        released = await _cancel_in_crm(crm, active_ids)
        await users.delete(user_id)
    return profile, released


async def _seed_visit(
    crm: MockCRMService,
    appointments: AppointmentRepository,
    spec: VisitSpec,
    *,
    catalogs: dict[int, _Catalog],
    user_id: int,
    guest_name: str,
    guest_phone: str,
    now: dt.datetime,
    imported: list[int],
) -> SeededVisit:
    """Регистрирует запись в Mock-CRM и сохраняет её в БД под настоящим номером из CRM."""
    if spec.branch_id not in catalogs:
        catalogs[spec.branch_id] = await _load_catalog(crm, spec.branch_id)
    catalog = catalogs[spec.branch_id]

    service = catalog.services.get(spec.service_title)
    if service is None or service.is_addon:
        raise SeedError(f"В прайсе нет услуги «{spec.service_title}».")
    addons: list[Service] = []
    for title in spec.addon_titles:
        addon = catalog.services.get(title)
        if addon is None or not addon.is_addon:
            raise SeedError(f"В прайсе нет дополнительной опции «{title}».")
        addons.append(addon)

    start, end = _visit_times(spec, service)
    drink = beverage_label(spec.beverage_code)
    comment = (
        f"Напиток: {drink or 'без напитка'}; "
        f"режим: {'тишина (silent cut)' if spec.silent_cut else 'общение'}; демо-запись"
    )

    for staff_id in spec.staff_ids:
        member = catalog.staff.get(staff_id)
        if member is None:
            continue
        # Слот мастера уже занят другой записью в БД — пробуем запасного мастера.
        if await appointments.find_conflict(
            branch_id=spec.branch_id, staff_id=member.id, start=start, end=end
        ):
            continue

        total_price = service.price_for(member.grade) + sum(
            addon.price_for(member.grade) for addon in addons
        )
        crm_status = (
            AppointmentStatus.CONFIRMED
            if spec.status is BookingStatus.CONFIRMED
            else AppointmentStatus.PENDING
        )
        # id=0 — «номер не назначен»: Mock-CRM выдаст свободный (outcome «reassigned» — норма).
        record, outcome = await crm.import_appointment(
            CrmAppointment(
                id=0,
                status=crm_status,
                branch_id=spec.branch_id,
                staff_id=member.id,
                service_id=service.id,
                addon_ids=tuple(addon.id for addon in addons),
                starts_at=start,
                ends_at=end,
                client_name=guest_name,
                client_phone=guest_phone,
                total_price=total_price,
                telegram_id=user_id,
                comment=comment,
                created_at=now,
            )
        )
        if outcome == "conflict":
            # Слот занят в самой CRM (например, осталась запись от прошлого запуска) — следующий мастер.
            continue
        imported.append(record.id)

        row = await appointments.create(
            user_id=user_id,
            crm_record_id=record.id,
            branch_id=spec.branch_id,
            staff_id=member.id,
            staff_name=member.full_name,
            staff_grade=member.grade.value,
            service_name=service.title,
            addons=[addon.title for addon in addons],
            datetime_start=start,
            datetime_end=end,
            total_price=total_price,
            beverage=drink,
            silent_cut=spec.silent_cut,
            status=spec.status,
        )
        return SeededVisit(
            spec=spec,
            db_id=row.id,
            crm_id=record.id,
            branch_title=catalog.branch.title,
            staff_name=member.full_name,
            grade=member.grade.label,
            service_name=service.title,
            addons=tuple(addon.title for addon in addons),
            start=start,
            end=end,
            total_price=total_price,
            status=spec.status,
        )

    raise SeedError(
        f"Не нашёл свободного мастера для сценария «{spec.scenario}»: слоты заняты. "
        "Запустите скрипт с --reset (или удалите data/mock_crm_state.json и файл БД)."
    )


async def seed(
    crm: MockCRMService,
    *,
    user_id: int,
    guest_name: str,
    reset: bool,
    tz: ZoneInfo,
) -> tuple[list[SeededVisit], int]:
    """Создаёт демо-набор. Возвращает записи и число слотов, освобождённых при ``--reset``."""
    profile: _Profile | None = None
    released = 0
    if reset:
        profile, released = await _reset_guest(crm, user_id)

    now = dt.datetime.now(dt.timezone.utc)
    plan = build_plan(now, tz)
    imported: list[int] = []  # номера записей, созданных в CRM этим запуском (для отката)
    catalogs: dict[int, _Catalog] = {}
    visits: list[SeededVisit] = []

    try:
        async with session_scope() as session:
            users = UserRepository(session)
            appointments = AppointmentRepository(session)

            user = await users.get(user_id)
            if user is None:
                user = await users.create(
                    telegram_id=user_id,
                    first_name=profile.first_name if profile else guest_name,
                    username=profile.username if profile else None,
                    phone=(profile.phone if profile and profile.phone else DEMO_PHONE),
                )
            else:
                if await appointments.list_for_user(user_id, limit=1):
                    raise SeedError(
                        f"У клиента {user_id} уже есть записи. Чтобы пересоздать демо-набор "
                        "с актуальным временем, запустите: python -m scripts.seed_demo_data --reset"
                    )
                if not user.phone:
                    user = await users.update_phone(user_id, DEMO_PHONE)

            for spec in plan:
                visits.append(
                    await _seed_visit(
                        crm,
                        appointments,
                        spec,
                        catalogs=catalogs,
                        user_id=user_id,
                        guest_name=user.first_name,
                        guest_phone=user.phone or DEMO_PHONE,
                        now=now,
                        imported=imported,
                    )
                )
    except Exception:
        # Транзакция БД откатилась — откатываем и CRM, чтобы слоты не остались занятыми «призраками».
        await _cancel_in_crm(crm, imported)
        raise
    return visits, released


# ----------------------------------------------------------------------
# Вывод
# ----------------------------------------------------------------------


def _stamp(value: dt.datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%d.%m %H:%M")


def print_summary(
    settings: Settings,
    *,
    user_id: int,
    visits: list[SeededVisit],
    released: int,
    report: SyncReport | None,
) -> None:
    tz = settings.tzinfo
    now = dt.datetime.now(tz)
    print()
    print("✅ Демо-данные BRUTAL CUTS готовы")
    print(f"   Клиент: Telegram ID {user_id} · время клуба: {settings.TIMEZONE}, сейчас {now:%d.%m %H:%M}")
    if released:
        print(f"   Прежний демо-набор удалён, слотов освобождено в CRM: {released}")
    print()
    for number, visit in enumerate(visits, start=1):
        extras = f" + {', '.join(visit.addons)}" if visit.addons else ""
        print(f"  №{number}  {visit.spec.scenario}")
        print(
            f"      {_stamp(visit.start, tz)}–{visit.end.astimezone(tz):%H:%M} · {visit.branch_title}"
        )
        print(
            f"      {visit.staff_name} ({visit.grade}) · {visit.service_name}{extras} · {visit.total_price} ₽"
        )
        print(
            f"      запись в БД №{visit.db_id} · номер в CRM {visit.crm_id} · статус: {visit.status.value}"
        )
    print()
    if report is not None:
        verdict = "согласованы" if not report.conflicts else f"КОНФЛИКТЫ: записи БД {report.conflicts}"
        print(f"   Проверка синхронизации БД ↔ Mock-CRM: {verdict}")
        print()
    print("Дальше:")
    print("   0. Откройте своего бота в Telegram и нажмите /start ДО запуска: Telegram не даёт боту")
    print("      писать первым, и без этого напоминания по демо-записям «сгорят» как недоставленные.")
    print("   1. python -m src.main   (если бот уже запущен — перезапустите его: Mock-CRM живёт в памяти)")
    print("   2. На первом проходе планировщика сами придут: напоминание за 2 часа (№1) и запрос")
    print("      оценки (№3); напоминание за 24 часа (№2) — когда до визита останется меньше суток.")
    print("   3. Не ждать: /test_reminder_24h · /test_reminder_2h · /test_review · /test_digest")
    print()


# ----------------------------------------------------------------------
# Точка входа
# ----------------------------------------------------------------------


async def run(settings: Settings, *, user_id: int, guest_name: str, reset: bool) -> None:
    if not settings.USE_MOCK_CRM:
        raise SeedError(
            "Скрипт работает только с встроенным Mock-CRM: установите USE_MOCK_CRM=True в .env."
        )

    await init_db(settings.DATABASE_URL)
    try:
        crm = get_crm_service(settings)
        if not isinstance(crm, MockCRMService):
            raise SeedError("Ожидался Mock-CRM, но фабрика вернула другую реализацию CRM.")
        try:
            visits, released = await seed(
                crm,
                user_id=user_id,
                guest_name=guest_name,
                reset=reset,
                tz=settings.tzinfo,
            )
            # Тот же механизм, что при старте бота: проверяем, что БД и Mock-CRM согласованы.
            report = await sync_mock_crm_with_database(crm)
        finally:
            await crm.close()
    finally:
        await close_db()

    print_summary(settings, user_id=user_id, visits=visits, released=released, report=report)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed_demo_data",
        description="Наполняет БД и Mock-CRM тремя демо-записями для проверки напоминаний и отзывов.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Telegram ID клиента, которому достанутся записи (по умолчанию — ADMIN_ID из .env)",
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_GUEST_NAME,
        help="Имя клиента — только если его ещё нет в БД (после /start подтянется имя из Telegram)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="удалить прежние записи клиента (и освободить слоты в Mock-CRM) и создать набор заново",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(name)s | %(message)s")

    try:
        settings = get_settings()
    except ValidationError as exc:
        print("❌ Не удалось прочитать настройки. Скопируйте .env.example в .env и заполните:", file=sys.stderr)
        for error in exc.errors():
            place = ".".join(str(part) for part in error["loc"]) or "настройки"
            print(f"   • {place}: {error['msg']}", file=sys.stderr)
        return 1

    user_id = args.user_id if args.user_id is not None else settings.ADMIN_ID
    if user_id <= 0:
        print("❌ Telegram ID должен быть положительным числом.", file=sys.stderr)
        return 1

    try:
        asyncio.run(run(settings, user_id=user_id, guest_name=args.name, reset=args.reset))
    except SeedError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
