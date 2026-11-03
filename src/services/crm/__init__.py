"""CRM-слой: интерфейс, Mock-движок и фабрика."""

from src.services.crm.base import (
    Appointment,
    AppointmentStatus,
    BaseCRMService,
    BookingData,
    Branch,
    CRMError,
    CRMNotFoundError,
    CRMValidationError,
    Grade,
    LoyaltyBalance,
    Service,
    ServiceKind,
    SlotUnavailableError,
    Staff,
)
from src.services.crm.factory import build_crm_service, get_crm_service, reset_crm_service
from src.services.crm.mock_service import MockCRMService

__all__ = [
    "Appointment",
    "AppointmentStatus",
    "BaseCRMService",
    "BookingData",
    "Branch",
    "CRMError",
    "CRMNotFoundError",
    "CRMValidationError",
    "Grade",
    "LoyaltyBalance",
    "MockCRMService",
    "Service",
    "ServiceKind",
    "SlotUnavailableError",
    "Staff",
    "build_crm_service",
    "get_crm_service",
    "reset_crm_service",
]
