"""FSM-состояния сценария онлайн-записи и схема данных черновика брони."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from aiogram.fsm.state import State, StatesGroup


class BookingStates(StatesGroup):
    """Шаги записи. Порядок объявления совпадает с порядком шагов."""

    choosing_branch = State()    # 1. выбор филиала
    choosing_service = State()   # 2. основная услуга
    choosing_addons = State()    # 3. допы (мультивыбор)
    choosing_staff = State()     # 4. мастер или «любой свободный»
    choosing_date = State()      # 5. календарь на 14 дней
    choosing_time = State()      # 6. свободные слоты
    confirming_price = State()   # 6а. согласование цены, если «любой мастер» ≠ базовая цена
    requesting_phone = State()   # 7. запрос контакта (пропускается, если номер уже в БД)
    choosing_beverage = State()  # 8. напиток
    choosing_silent = State()    # 9. режим тишины + итоговая карточка
    creating = State()           # 10. запись создаётся (защита от двойного нажатия)


class MenuStates(StatesGroup):
    """Состояния вне сценария записи."""

    requesting_phone_for_balance = State()  # номер нужен, чтобы показать бонусы


@dataclass(slots=True)
class BookingDraft:
    """Черновик брони, который накапливается по шагам.

    Хранится в FSM-данных как обычный dict, поэтому одинаково работает
    с MemoryStorage и RedisStorage (только JSON-совместимые типы).
    """

    branch_id: int | None = None
    service_id: int | None = None
    addon_ids: list[int] = field(default_factory=list)
    # True — клиент выбрал «Любой свободный мастер». В этом режиме staff_id
    # заполняется только после выбора слота (назначенный мастер).
    any_staff: bool = False
    staff_id: int | None = None
    date: str | None = None      # ISO: 2026-11-05
    time: str | None = None      # HH:MM
    price_confirmed: bool = False
    beverage: str | None = None  # код напитка из справочника (или "none")
    silent_cut: bool = False
    # ID записи в БД, которую нужно отменить после успешного создания новой (перенос визита).
    reschedule_of: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> BookingDraft:
        if not raw:
            return cls()
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in raw.items() if key in known})

    def reset_slot(self) -> None:
        """Сбрасывает выбранную дату, время и назначенного мастера."""
        self.date = None
        self.time = None
        self.price_confirmed = False
        if self.any_staff:
            self.staff_id = None
