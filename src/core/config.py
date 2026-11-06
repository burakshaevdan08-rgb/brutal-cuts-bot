"""Конфигурация приложения BRUTAL CUTS.

Все настройки читаются из переменных окружения и файла ``.env``
и валидируются при старте приложения: если токен бота имеет неверный
формат или при USE_MOCK_CRM=False не заданы ключи YClients, бот
не запустится и сообщит, что именно нужно исправить.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR: Path = Path(__file__).resolve().parents[2]

_BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
_ALLOWED_DB_SCHEMES: tuple[str, ...] = (
    "sqlite+aiosqlite://",
    "postgresql+asyncpg://",
)
# Строки подключения «как у хостингов» приводятся к async-драйверам.
_DB_URL_ALIASES: tuple[tuple[str, str], ...] = (
    ("postgres://", "postgresql+asyncpg://"),
    ("postgresql://", "postgresql+asyncpg://"),
    ("sqlite:///", "sqlite+aiosqlite:///"),
)
_ALLOWED_REDIS_SCHEMES: tuple[str, ...] = ("redis://", "rediss://", "unix://")


class Settings(BaseSettings):
    """Типизированные настройки приложения."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Пустая строка в .env (например, YCLIENTS_USER_TOKEN=) считается
        # «значение не задано», а не пустым секретом.
        env_ignore_empty=True,
    )

    # --- Telegram -------------------------------------------------
    BOT_TOKEN: SecretStr = Field(description="Токен бота от @BotFather")
    ADMIN_ID: int = Field(gt=0, description="Telegram ID администратора")

    # --- Режим CRM ------------------------------------------------
    USE_MOCK_CRM: bool = Field(
        default=True,
        description="True — встроенный Mock-движок, False — реальный YClients API",
    )
    MOCK_LATENCY_MS: int = Field(
        default=120,
        ge=0,
        le=5000,
        description="Имитация сетевой задержки Mock-CRM, мс",
    )
    MOCK_STATE_FILE: str = Field(
        default="data/mock_crm_state.json",
        description=(
            "JSON-файл с состоянием Mock-CRM (записи и счётчик ID). "
            "Относительный путь считается от корня проекта; none — не сохранять."
        ),
    )

    # --- YClients -------------------------------------------------
    YCLIENTS_API_URL: str = "https://api.yclients.com/api/v1"
    YCLIENTS_PARTNER_TOKEN: SecretStr | None = None
    YCLIENTS_USER_TOKEN: SecretStr | None = None
    YCLIENTS_COMPANY_IDS: str = Field(
        default="",
        description="ID филиалов YClients через запятую",
    )

    # --- Планировщик и напоминания -----------------------------------
    SCHEDULER_POLL_SECONDS: int = Field(
        default=60,
        ge=10,
        le=3600,
        description="Как часто планировщик ищет записи для напоминаний, сек",
    )
    DIGEST_TIME: str = Field(
        default="09:30",
        description="Время утреннего дайджеста барберов (HH:MM, часовой пояс клуба)",
    )
    DIGEST_CHAT_ID: int | None = Field(
        default=None,
        description="Чат для дайджестов мастеров без личного чата; по умолчанию — ADMIN_ID",
    )
    BARBER_CHAT_IDS: str = Field(
        default="",
        description="Личные чаты мастеров: 'ID_мастера:chat_id' через запятую, например 1:111,7:222",
    )

    # --- Общие настройки ------------------------------------------
    TIMEZONE: str = "Europe/Moscow"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- База данных ----------------------------------------------
    DATABASE_URL: str = "sqlite+aiosqlite:///./brutal_cuts.db"

    # --- Хранилище FSM --------------------------------------------
    USE_REDIS: bool = False
    REDIS_URL: str = "redis://localhost:6379/0"

    # ------------------------------------------------------------
    # Валидаторы полей
    # ------------------------------------------------------------
    @field_validator("BOT_TOKEN")
    @classmethod
    def _validate_bot_token(cls, value: SecretStr) -> SecretStr:
        if not _BOT_TOKEN_RE.match(value.get_secret_value()):
            raise ValueError(
                "BOT_TOKEN имеет неверный формат. Ожидается вид "
                "'123456789:AAH...' — возьмите токен у @BotFather."
            )
        return value

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("TIMEZONE")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"Неизвестный часовой пояс '{value}'. Пример: Europe/Moscow."
            ) from exc
        return value

    @field_validator("DIGEST_TIME")
    @classmethod
    def _validate_digest_time(cls, value: str) -> str:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
        if match is None or int(match[1]) > 23 or int(match[2]) > 59:
            raise ValueError("DIGEST_TIME должен быть в формате HH:MM, например 09:30.")
        return f"{int(match[1]):02d}:{match[2]}"

    @field_validator("BARBER_CHAT_IDS")
    @classmethod
    def _validate_barber_chats(cls, value: str) -> str:
        cleaned = value.replace(" ", "")
        if not cleaned:
            return ""
        for chunk in cleaned.split(","):
            staff_id, separator, chat_id = chunk.partition(":")
            if not separator or not staff_id.isdigit() or not chat_id.lstrip("-").isdigit():
                raise ValueError(
                    "BARBER_CHAT_IDS: ожидается 'ID_мастера:chat_id' через запятую, например 1:111,7:222."
                )
        return cleaned

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _normalize_database_url(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        for prefix, replacement in _DB_URL_ALIASES:
            if cleaned.startswith(prefix):
                return replacement + cleaned[len(prefix):]
        return cleaned

    @field_validator("DATABASE_URL")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value.startswith(_ALLOWED_DB_SCHEMES):
            raise ValueError(
                "DATABASE_URL должен использовать async-драйвер: "
                "'sqlite+aiosqlite://...' или 'postgresql+asyncpg://...'."
            )
        return value

    @field_validator("REDIS_URL")
    @classmethod
    def _validate_redis_url(cls, value: str) -> str:
        if not value.startswith(_ALLOWED_REDIS_SCHEMES):
            raise ValueError(
                "REDIS_URL должен начинаться с 'redis://', 'rediss://' или 'unix://'."
            )
        return value

    @field_validator("YCLIENTS_COMPANY_IDS")
    @classmethod
    def _validate_company_ids(cls, value: str) -> str:
        cleaned = value.replace(" ", "")
        if not cleaned:
            return ""
        for chunk in cleaned.split(","):
            if not chunk.isdigit():
                raise ValueError(
                    "YCLIENTS_COMPANY_IDS должен содержать числовые ID через запятую, "
                    "например: 111111,222222."
                )
        return cleaned

    # ------------------------------------------------------------
    # Кросс-полевая валидация
    # ------------------------------------------------------------
    @model_validator(mode="after")
    def _validate_crm_mode(self) -> "Settings":
        if self.USE_MOCK_CRM:
            return self

        missing: list[str] = []
        if self.YCLIENTS_PARTNER_TOKEN is None:
            missing.append("YCLIENTS_PARTNER_TOKEN")
        if self.YCLIENTS_USER_TOKEN is None:
            missing.append("YCLIENTS_USER_TOKEN")
        if not self.YCLIENTS_COMPANY_IDS:
            missing.append("YCLIENTS_COMPANY_IDS")

        if missing:
            raise ValueError(
                "При USE_MOCK_CRM=False необходимо задать: " + ", ".join(missing) + ". "
                "Для работы без реального YClients установите USE_MOCK_CRM=True."
            )
        return self

    # ------------------------------------------------------------
    # Производные значения
    # ------------------------------------------------------------
    @property
    def tzinfo(self) -> ZoneInfo:
        """Объект часового пояса клуба."""
        return ZoneInfo(self.TIMEZONE)

    @property
    def yclients_company_ids(self) -> tuple[int, ...]:
        """ID филиалов YClients в виде кортежа чисел."""
        if not self.YCLIENTS_COMPANY_IDS:
            return ()
        return tuple(int(chunk) for chunk in self.YCLIENTS_COMPANY_IDS.split(","))

    @property
    def digest_hour_minute(self) -> tuple[int, int]:
        hour, minute = self.DIGEST_TIME.split(":")
        return int(hour), int(minute)

    @property
    def digest_chat_id(self) -> int:
        """Чат для дайджестов: DIGEST_CHAT_ID или, если не задан, ADMIN_ID."""
        return self.DIGEST_CHAT_ID if self.DIGEST_CHAT_ID is not None else self.ADMIN_ID

    @property
    def barber_chat_ids(self) -> dict[int, int]:
        """Личные чаты мастеров: ``{ID мастера в CRM: chat_id}``."""
        if not self.BARBER_CHAT_IDS:
            return {}
        result: dict[int, int] = {}
        for chunk in self.BARBER_CHAT_IDS.split(","):
            staff_id, _, chat_id = chunk.partition(":")
            result[int(staff_id)] = int(chat_id)
        return result

    @property
    def mock_state_path(self) -> Path | None:
        """Абсолютный путь к файлу состояния Mock-CRM или None, если сохранение отключено."""
        value = self.MOCK_STATE_FILE.strip()
        if value.lower() in {"", "none", "off", "false", "0"}:
            return None
        path = Path(value).expanduser()
        return path if path.is_absolute() else BASE_DIR / path

    @property
    def is_sqlite(self) -> bool:
        """True, если используется локальная SQLite-база."""
        return self.DATABASE_URL.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек (кешируется)."""
    return Settings()  # type: ignore[call-arg]
