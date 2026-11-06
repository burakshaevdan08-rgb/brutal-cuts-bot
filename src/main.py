"""Точка входа: запуск Telegram-бота BRUTAL CUTS."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

from src.core.config import Settings, get_settings
from src.database import close_db, init_db
from src.bot.handlers import get_routers
from src.scheduler.scheduler import create_scheduler
from src.services.crm import get_crm_service
from src.services.crm.mock_sync import sync_mock_crm_with_database

logger = logging.getLogger(__name__)


def build_storage(settings: Settings) -> BaseStorage:
    if settings.USE_REDIS:
        logger.info("FSM: RedisStorage")
        return RedisStorage.from_url(settings.REDIS_URL)
    logger.info("FSM: MemoryStorage")
    return MemoryStorage()


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.LOG_LEVEL,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    # Создаёт движок и недостающие таблицы (SQLite-файл или PostgreSQL).
    session_factory = await init_db(settings.DATABASE_URL)
    crm = get_crm_service(settings)
    try:
        # Mock-CRM подтягивает из БД записи, которых не знает (слоты и счётчик ID).
        await sync_mock_crm_with_database(crm)
    except Exception:
        logger.exception("Не удалось синхронизировать Mock-CRM с БД")
    storage = build_storage(settings)
    bot = Bot(
        token=settings.BOT_TOKEN.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Всё, что передано в Dispatcher как kwargs, доступно хендлерам по имени параметра.
    dp = Dispatcher(storage=storage, crm=crm, settings=settings, session_factory=session_factory)
    for router in get_routers():
        dp.include_router(router)

    try:
        await bot.send_message(
            settings.ADMIN_ID,
            f"BRUTAL CUTS запущен. CRM: {'MOCK' if crm.is_mock else 'LIVE'}",
        )
    except Exception:
        logger.warning("Не удалось отправить стартовое сообщение администратору")

    # Планировщик живёт в том же event loop, что и бот: опрос напоминаний и утренний дайджест.
    scheduler = create_scheduler(bot, crm, settings)
    scheduler.start()
    logger.info(
        "Планировщик запущен: опрос напоминаний каждые %s с, дайджест барберов в %s (%s)",
        settings.SCHEDULER_POLL_SECONDS,
        settings.DIGEST_TIME,
        settings.TIMEZONE,
    )

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await crm.close()
        await close_db()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
