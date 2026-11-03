"""Фабрика CRM-сервиса.

Выбирает реализацию ``BaseCRMService`` по настройке USE_MOCK_CRM:

* True  — ``MockCRMService`` (автономный режим, внешние API не нужны);
* False — реальная интеграция с YClients (в этот каркас не входит).

Mock-движок хранит записи в памяти процесса, поэтому хендлерам нужен один
общий экземпляр — его отдаёт ``get_crm_service``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.services.crm.base import BaseCRMService
from src.services.crm.mock_service import MockCRMService

if TYPE_CHECKING:
    from src.core.config import Settings

logger = logging.getLogger(__name__)

_instance: BaseCRMService | None = None


def build_crm_service(settings: Settings | None = None) -> BaseCRMService:
    """Создаёт новый экземпляр CRM-сервиса согласно настройкам."""
    if settings is None:
        # Импорт отложен, чтобы пакет crm можно было использовать
        # (например, в тестах) без загрузки pydantic-settings.
        from src.core.config import get_settings

        settings = get_settings()

    if settings.USE_MOCK_CRM:
        logger.info("CRM: режим MOCK (встроенный движок, без внешних API)")
        return MockCRMService(
            timezone=settings.TIMEZONE,
            latency_ms=settings.MOCK_LATENCY_MS,
        )

    raise NotImplementedError(
        "Реальная интеграция с YClients не входит в этот каркас. "
        "Установите USE_MOCK_CRM=True или добавьте реализацию BaseCRMService "
        "для YClients и подключите её в этой фабрике."
    )


def get_crm_service(settings: Settings | None = None) -> BaseCRMService:
    """Возвращает общий экземпляр CRM-сервиса (создаёт при первом обращении)."""
    global _instance
    if _instance is None:
        _instance = build_crm_service(settings)
    return _instance


def reset_crm_service() -> None:
    """Сбрасывает общий экземпляр (для тестов и повторной инициализации)."""
    global _instance
    _instance = None
