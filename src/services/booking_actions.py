"""Действия над существующей записью: подтверждение и отмена.

Общий код для кнопок «Мои записи», кнопок напоминаний и сценария переноса.
Порядок всегда одинаковый: сначала CRM (она владеет расписанием и освобождает слот),
затем локальная БД. Если CRM недоступна, БД не меняется — действие можно повторить.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.database import AppointmentRepository, BookingStatus, session_scope
from src.database.models import Appointment as DbAppointment
from src.database.models import utcnow
from src.services.crm import BaseCRMService, CRMError, CRMNotFoundError, CRMValidationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Итог действия над записью."""

    ok: bool
    message: str = ""
    appointment: DbAppointment | None = None


async def confirm_visit(crm: BaseCRMService, user_id: int, appointment_id: int) -> ActionResult:
    """«Буду точно»: confirmed в БД и ``confirm_appointment`` в CRM.

    Повторное подтверждение безопасно (идемпотентно).
    """
    async with session_scope() as session:
        repo = AppointmentRepository(session)
        appointment = await repo.get(appointment_id, user_id=user_id)

        if appointment is None:
            return ActionResult(False, "Запись не найдена.")
        if appointment.status in (BookingStatus.CANCELLED, BookingStatus.COMPLETED):
            return ActionResult(False, "Эта запись уже неактуальна.", appointment)
        if appointment.datetime_start <= utcnow():
            return ActionResult(False, "Визит уже начался.", appointment)

        if appointment.status is BookingStatus.CREATED:
            try:
                await crm.confirm_appointment(int(appointment.crm_record_id))
            except CRMNotFoundError:
                # Например, Mock-CRM потерял запись: в БД подтверждаем всё равно.
                logger.warning("Запись CRM #%s не найдена при подтверждении", appointment.crm_record_id)
            except CRMValidationError as exc:
                return ActionResult(False, str(exc), appointment)
            except (CRMError, ValueError):
                logger.exception("Не удалось подтвердить запись #%s в CRM", appointment.crm_record_id)
                return ActionResult(False, "Не удалось подтвердить визит. Попробуйте позже.", appointment)
            appointment = await repo.confirm(appointment.id, user_id=user_id)

        return ActionResult(True, appointment=appointment)


async def cancel_visit(crm: BaseCRMService, user_id: int, appointment_id: int) -> ActionResult:
    """Отмена визита: запись отменяется в CRM (слот освобождается сразу), затем в БД.

    Повторная отмена безопасна: уже отменённая запись возвращает ``ok=True``.
    """
    async with session_scope() as session:
        repo = AppointmentRepository(session)
        appointment = await repo.get(appointment_id, user_id=user_id)

        if appointment is None:
            return ActionResult(False, "Запись не найдена.")
        if appointment.status is BookingStatus.CANCELLED:
            return ActionResult(True, appointment=appointment)
        if appointment.status is BookingStatus.COMPLETED:
            return ActionResult(False, "Завершённую запись отменить нельзя.", appointment)

        try:
            await crm.cancel_appointment(int(appointment.crm_record_id))
        except CRMNotFoundError:
            # Например, Mock-CRM был перезапущен и «забыл» запись: в БД всё равно отменяем.
            logger.warning("Запись CRM #%s не найдена при отмене", appointment.crm_record_id)
        except CRMValidationError as exc:
            return ActionResult(False, str(exc), appointment)
        except (CRMError, ValueError):
            logger.exception("Не удалось отменить запись #%s в CRM", appointment.crm_record_id)
            return ActionResult(False, "Не удалось отменить запись. Попробуйте позже.", appointment)

        appointment = await repo.cancel(appointment.id, user_id=user_id)
        return ActionResult(True, appointment=appointment)
