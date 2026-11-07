"""Роутеры бота."""

from aiogram import Router

from src.bot.handlers import booking, menu, reminders, reviews


def get_routers() -> list[Router]:
    """Роутеры в порядке подключения.

    * ``menu`` идёт первым: команды /start и /menu должны срабатывать даже тогда,
      когда пользователь находится посреди сценария записи;
    * ``reminders`` и ``reviews`` — раньше ``booking``, чтобы debug-команды администратора
      не перехватывались FSM-обработчиками шага «телефон»; внутри ``reviews`` команды
      исключены из приёма текста претензии, поэтому /book и другие команды работают
      и посреди сбора отзыва.
    """
    return [menu.router, reminders.router, reviews.router, booking.router]


__all__ = ["get_routers"]
