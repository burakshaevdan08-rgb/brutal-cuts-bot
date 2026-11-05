"""Роутеры бота."""

from aiogram import Router

from src.bot.handlers import booking, menu


def get_routers() -> list[Router]:
    """Роутеры в порядке подключения.

    ``menu`` идёт первым: команды /start и /menu должны срабатывать даже тогда,
    когда пользователь находится посреди сценария записи.
    """
    return [menu.router, booking.router]


__all__ = ["get_routers"]
