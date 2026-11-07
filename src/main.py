"""Точка входа: запуск Telegram-бота BRUTAL CUTS."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from apscheduler.schedulers.asyncio import AsyncIOScheduler

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


async def setup_bot_commands(bot: Bot, settings: Settings) -> None:
    """Меню команд: общее для всех и расширенное (debug) для администратора."""
    public = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="book", description="Записаться"),
    ]
    admin = public + [
        BotCommand(command="test_reminder_24h", description="Тест: напоминание за 24 часа"),
        BotCommand(command="test_reminder_2h", description="Тест: напоминание за 2 часа"),
        BotCommand(command="test_review", description="Тест: запрос оценки визита"),
        BotCommand(command="test_digest", description="Тест: дайджест барберов"),
    ]
    try:
        await bot.set_my_commands(public, scope=BotCommandScopeDefault())
        await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=settings.ADMIN_ID))
    except TelegramAPIError:
        # Например, администратор ещё не писал боту: меню команд не критично для работы.
        logger.warning("Не удалось настроить меню команд бота", exc_info=True)


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.LOG_LEVEL,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    # Создаёт движок и недостающие таблицы (SQLite-файл или PostgreSQL).
    session_factory = await init_db(settings.DATABASE_URL)
    crm = get_crm_service(settings)
    storage = build_storage(settings)
    bot = Bot(
        token=settings.BOT_TOKEN.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    scheduler: AsyncIOScheduler | None = None

    try:
        # Быстрая проверка токена: понятная ошибка вместо traceback из недр polling.
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            logger.critical("Telegram отклонил BOT_TOKEN: проверьте токен в .env (его выдаёт @BotFather).")
            return
        logger.info("Бот авторизован: @%s", me.username)

        try:
            # Mock-CRM подтягивает из БД записи, которых не знает (слоты и счётчик ID).
            await sync_mock_crm_with_database(crm)
        except Exception:
            logger.exception("Не удалось синхронизировать Mock-CRM с БД")

        # Polling и webhook взаимоисключающие: если у токена остался webhook, апдейты не придут.
        await bot.delete_webhook(drop_pending_updates=False)

        # Всё, что передано в Dispatcher как kwargs, доступно хендлерам по имени параметра.
        dp = Dispatcher(storage=storage, crm=crm, settings=settings, session_factory=session_factory)
        for router in get_routers():
            dp.include_router(router)
        await setup_bot_commands(bot, settings)

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
        logger.info("Debug-команды (/test_*) доступны только ADMIN_ID=%s", settings.ADMIN_ID)

        await dp.start_polling(bot)
    finally:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
        await crm.close()
        await close_db()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
