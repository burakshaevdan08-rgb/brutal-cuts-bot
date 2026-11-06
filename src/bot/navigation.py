"""Навигационные данные филиалов для напоминания «за 2 часа».

ВАЖНО: значения ниже — демонстрационные (адреса с номерами домов, ориентиры, коды
шлагбаумов и координаты придуманы для портфолио). Для боевого использования замените
их реальными или вынесите в CRM / конфигурацию.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from src.bot.common import esc
from src.services.crm import Branch


@dataclass(frozen=True, slots=True)
class BranchNavigation:
    """Как найти филиал и въехать на парковку."""

    address: str
    entrance: str
    walking: str
    parking: str
    parking_scheme: str  # моноширинная схема (выводится в <pre>)
    barrier_code: str
    latitude: float
    longitude: float


NAVIGATION: Final[dict[int, BranchNavigation]] = {
    # --- м. Белорусская ------------------------------------------------
    1: BranchNavigation(
        address="Москва, Ленинградский пр-т, 8 (вход со двора)",
        entrance=(
            "Арка между аптекой и кофейней, затем налево: зелёная дверь "
            "с вывеской BRUTAL CUTS. Домофон — кнопка «BRUTAL»."
        ),
        walking="5 минут пешком от метро «Белорусская» (выход к Ленинградскому пр-ту).",
        parking="Гостевые места P1–P4 во дворе, въезд через шлагбаум.",
        parking_scheme=(
            "   Ленинградский пр-т\n"
            " ═══════════╤═══════════\n"
            "         арка│\n"
            "   ┌─────────┴────────┐\n"
            "   │ P1  P2  P3  P4   │\n"
            "   │ шлагбаум ▓       │\n"
            "   │          ▼       │\n"
            "   │       [ ВХОД ]   │\n"
            "   └──────────────────┘"
        ),
        barrier_code="4721#",
        latitude=55.7776,
        longitude=37.5822,
    ),
    # --- м. Бауманская -------------------------------------------------
    2: BranchNavigation(
        address="Москва, ул. Ладожская, 8, стр. 1",
        entrance=(
            "Со стороны улицы — тёмная дверь с неоновой вывеской BRUTAL CUTS "
            "рядом с входом в бизнес-центр. Звонок справа от двери."
        ),
        walking="7 минут пешком от метро «Бауманская» (выход к Бауманской ул., затем на Ладожскую).",
        parking="Парковка перед зданием: P1–P3 у входа, P4 — справа от шлагбаума.",
        parking_scheme=(
            "      ул. Ладожская\n"
            " ══════════╤═══════════\n"
            "           │ въезд\n"
            "     ╔═════╧═════╗\n"
            "     ║ P1  P2 P3 ║ ▓ шлагбаум\n"
            "     ║ P4        ║\n"
            "     ╚═════╤═════╝\n"
            "           ▼\n"
            "       [ ВХОД ]"
        ),
        barrier_code="1985*",
        latitude=55.7706,
        longitude=37.6748,
    ),
}


def yandex_navigator_url(latitude: float, longitude: float) -> str:
    """Ссылка для кнопки «Яндекс.Навигатор».

    Telegram не принимает в URL-кнопках собственные схемы (``yandexnavi://``),
    поэтому используется универсальная https-ссылка Яндекс.Карт: точка отправления
    пуста (берётся текущее местоположение), на телефоне ссылка открывает приложение
    Яндекс (Карты / Навигатор) с уже построенным маршрутом.
    """
    return f"https://yandex.ru/maps/?rtext=~{latitude:.6f},{longitude:.6f}&rtt=auto"


def navigation_url(branch_id: int) -> str | None:
    """Ссылка на маршрут до филиала или None, если координаты неизвестны."""
    nav = NAVIGATION.get(branch_id)
    if nav is None:
        return None
    return yandex_navigator_url(nav.latitude, nav.longitude)


def format_navigation_block(branch: Branch | None, branch_id: int) -> str:
    """Блок «как добраться» для сообщения-напоминания."""
    nav = NAVIGATION.get(branch_id)
    if nav is None:
        if branch is None:
            return ""
        return f"📍 <b>Адрес:</b> м. {esc(branch.metro)}, {esc(branch.address)}"

    return "\n".join(
        [
            f"📍 <b>Точный адрес:</b> {esc(nav.address)}",
            f"🚪 <b>Как найти вход:</b> {esc(nav.entrance)}",
            f"🚶 <b>От метро:</b> {esc(nav.walking)}",
            f"🅿️ <b>Парковка:</b> {esc(nav.parking)}",
            f"<pre>{esc(nav.parking_scheme)}</pre>",
            f"🔐 <b>Код шлагбаума:</b> <code>{esc(nav.barrier_code)}</code>",
        ]
    )
