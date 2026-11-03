"""Точка входа: запуск Telegram-бота BRUTAL CUTS."""

from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Message

from src.core.config import Settings, get_settings
from src.database import close_db, init_db
from src.services.crm import BaseCRMService, get_crm_service

logger = logging.getLogger(__name__)

router = Router(name="common")


@router.message(CommandStart())
async def cmd_start(message: Message, crm: BaseCRMService) -> None:
    mode = "MOCK (демо-данные)" if crm.is_mock else "LIVE (YClients)"
    await message.answer(
        "<b>BRUTAL CUTS</b> — мужской клуб.\n\n"
        "Доступные команды:\n"
        "/branches — филиалы и мастера\n"
        "/loyalty +79990000002 — баланс программы лояльности\n\n"
        f"<i>Режим CRM: {mode}</i>"
    )


@router.message(Command("branches"))
async def cmd_branches(message: Message, crm: BaseCRMService) -> None:
    lines: list[str] = []
    for branch in await crm.get_branches():
        staff = await crm.get_staff(branch.id)
        lines.append(
            f"<b>{html.escape(branch.title)}</b>\n"
            f"м. {html.escape(branch.metro)}, {html.escape(branch.address)} · "
            f"{branch.chairs} кресел · {branch.work_hours_label}"
        )
        for member in staff:
            lines.append(
                f"  • {html.escape(member.full_name)} — {member.grade.label}, "
                f"стаж {member.experience_label}"
            )
        lines.append("")
    await message.answer("\n".join(lines).strip())


@router.message(Command("loyalty"))
async def cmd_loyalty(message: Message, crm: BaseCRMService) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите телефон: /loyalty +79990000002")
        return
    try:
        balance = await crm.get_client_loyalty_balance(parts[1])
    except Exception as exc:  # CRMError и любые сбои CRM не должны ронять хендлер
        await message.answer(html.escape(str(exc)))
        return
    await message.answer(
        f"<b>{html.escape(balance.phone)}</b>\n"
        f"Уровень: {balance.tier} (скидка {balance.discount_percent}%)\n"
        f"Визитов: {balance.visits}\n"
        f"Баллов: {balance.points}"
    )


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
    storage = build_storage(settings)
    bot = Bot(
        token=settings.BOT_TOKEN.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Всё, что передано в Dispatcher как kwargs, доступно хендлерам по имени параметра.
    dp = Dispatcher(storage=storage, crm=crm, settings=settings, session_factory=session_factory)
    dp.include_router(router)

    try:
        await bot.send_message(
            settings.ADMIN_ID,
            f"BRUTAL CUTS запущен. CRM: {'MOCK' if crm.is_mock else 'LIVE'}",
        )
    except Exception:
        logger.warning("Не удалось отправить стартовое сообщение администратору")

    try:
        await dp.start_polling(bot)
    finally:
        await crm.close()
        await close_db()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
