"""Роутеры бота."""

from aiogram import Router

from src.bot.handlers import booking, menu, reminders


def get_routers() -> list[Router]:
    """Роутеры в порядке подключения.

    * ``menu`` идёт первым: команды /start и /menu должны срабатывать даже тогда,
      когда пользователь находится посреди сценария записи;
    * ``reminders`` — раньше ``booking``, чтобы debug-команды администратора
      не перехватывались FSM-обработчиками шага «телефон».
    """
    return [menu.router, reminders.router, booking.router]


__all__ = ["get_routers"]
