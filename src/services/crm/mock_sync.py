"""Синхронизация Mock-CRM с локальной БД при старте бота.

Mock-CRM — отдельная «внешняя система» со своим состоянием (JSON-файл). Но локальная БД
может знать о записях, которых Mock не помнит: файл состояния удалён или ещё не создавался
(например, БД осталась от прошлой версии бота). Тогда Mock предложил бы уже занятое время
и начал нумерацию записей заново.

``sync_mock_crm_with_database`` один раз при старте переносит активные записи из БД в Mock:

* их слоты снова становятся занятыми;
* счётчик ID сдвигается за максимальный номер записи;
* если ID записи в БД уже занят другой записью, выдаётся новый, и ссылка в БД исправляется;
* пересекающиеся записи одного мастера (дубли) не импортируются, а попадают в отчёт —
  решать, что с ними делать, должен человек.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass, field

from src.database import AppointmentRepository, UserRepository, session_scope
from src.database.models import BookingStatus, utcnow
from src.services.crm.base import (
    Appointment,
    AppointmentStatus,
    BaseCRMService,
    CRMNotFoundError,
    Service,
)
from src.services.crm.mock_service import SLOT_STEP_MINUTES, MockCRMService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncReport:
    """Итог синхронизации."""

    imported: int = 0
    already_known: int = 0
    reassigned: int = 0
    #: ID записей БД, которые не удалось перенести из-за пересечения с другой записью мастера.
    conflicts: list[int] = field(default_factory=list)


def _occupied_end(start: dt.datetime, end: dt.datetime) -> dt.datetime:
    """Конец занятых часовых окон: длительность округляется вверх до часа."""
    minutes = int((end - start).total_seconds() // 60)
    slots = max(1, math.ceil(minutes / SLOT_STEP_MINUTES))
    return start + dt.timedelta(minutes=slots * SLOT_STEP_MINUTES)


async def sync_mock_crm_with_database(
    crm: BaseCRMService,
    *,
    lookback: dt.timedelta = dt.timedelta(days=1),
) -> SyncReport | None:
    """Переносит активные записи (created / confirmed) из БД в Mock-CRM.

    Для не-Mock реализаций ничего не делает и возвращает ``None``.
    """
    if not isinstance(crm, MockCRMService):
        return None

    report = SyncReport()
    catalogs: dict[int, tuple[dict[str, Service], dict[str, Service]]] = {}
    accepted: list[tuple[int, dt.datetime, dt.datetime]] = []  # (staff_id, начало, занято до)

    async with session_scope() as session:
        appointments = AppointmentRepository(session)
        users = UserRepository(session)
        rows = await appointments.list_active_since(utcnow() - lookback)

        for row in rows:
            if row.branch_id not in catalogs:
                try:
                    services = await crm.get_services(row.branch_id)
                except CRMNotFoundError:
                    logger.warning("Синхронизация: запись БД #%s ссылается на неизвестный филиал %s", row.id, row.branch_id)
                    report.conflicts.append(row.id)
                    continue
                catalogs[row.branch_id] = (
                    {item.title: item for item in services if not item.is_addon},
                    {item.title: item for item in services if item.is_addon},
                )
            main_services, addon_services = catalogs[row.branch_id]

            service = main_services.get(row.service_name)
            duration = dt.timedelta(minutes=service.duration_minutes if service else 60)
            starts_at = row.datetime_start
            ends_at = row.datetime_end or starts_at + duration

            # Дубли внутри самой БД: у одного мастера две активные записи на пересекающееся время.
            occupied_until = _occupied_end(starts_at, ends_at)
            clash = next(
                (
                    item
                    for item in accepted
                    if item[0] == row.staff_id and item[1] < occupied_until and item[2] > starts_at
                ),
                None,
            )
            if clash is not None:
                logger.warning(
                    "Синхронизация: запись БД #%s (мастер %s, %s) пересекается с другой активной "
                    "записью и не перенесена в Mock-CRM. Отмените дубль вручную.",
                    row.id,
                    row.staff_id,
                    starts_at.isoformat(),
                )
                report.conflicts.append(row.id)
                continue

            user = await users.get(row.user_id)
            candidate = Appointment(
                id=int(row.crm_record_id) if row.crm_record_id.isdigit() else 0,
                status=(
                    AppointmentStatus.CONFIRMED
                    if row.status is BookingStatus.CONFIRMED
                    else AppointmentStatus.PENDING
                ),
                branch_id=row.branch_id,
                staff_id=row.staff_id,
                service_id=service.id if service else 0,
                addon_ids=tuple(addon_services[title].id for title in row.addons if title in addon_services),
                starts_at=starts_at,
                ends_at=ends_at,
                client_name=user.first_name if user else "Клиент",
                client_phone=user.phone if user and user.phone else "",
                total_price=row.total_price,
                telegram_id=row.user_id,
                comment="Импортировано из локальной БД",
                created_at=row.created_at,
            )

            record, outcome = await crm.import_appointment(candidate)
            if outcome == "imported":
                report.imported += 1
            elif outcome == "exists":
                report.already_known += 1
            elif outcome == "reassigned":
                report.reassigned += 1
                await appointments.set_crm_record_id(row.id, record.id)
                logger.warning(
                    "Синхронизация: ID записи БД #%s был занят, выдан новый номер %s", row.id, record.id
                )
            else:  # "conflict": слот уже занят другой записью в самом Mock-CRM
                logger.warning(
                    "Синхронизация: запись БД #%s конфликтует с записью в Mock-CRM и не перенесена", row.id
                )
                report.conflicts.append(row.id)
                continue

            accepted.append((row.staff_id, starts_at, occupied_until))

    logger.info(
        "Синхронизация Mock-CRM с БД: перенесено %s, уже было %s, новых ID %s, конфликтов %s",
        report.imported,
        report.already_known,
        report.reassigned,
        len(report.conflicts),
    )
    return report
