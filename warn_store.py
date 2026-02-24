import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiomysql

DB_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
ROLE_ID_RE = re.compile(r"\d{17,20}")

DEFAULT_GUILD_SETTINGS = {
    "mod_log_channel_id": None,
    "automod_log_channel_id": None,
    "warn_timeout_threshold": 3,
    "warn_ban_threshold": 5,
    "warn_expiration_days": 60,
    "warn_timeout_duration_minutes": 60,
    "automod_enabled": True,
    "automod_anti_spam": True,
    "automod_anti_link": True,
    "automod_anti_mention_flood": True,
    "automod_spam_max_messages": 5,
    "automod_spam_interval_seconds": 8,
    "automod_mention_limit": 5,
    "automod_bypass_role_ids": [],
}

SETTINGS_FIELD_TYPES: dict[str, str] = {
    "mod_log_channel_id": "int_or_none",
    "automod_log_channel_id": "int_or_none",
    "warn_timeout_threshold": "int",
    "warn_ban_threshold": "int",
    "warn_expiration_days": "int",
    "warn_timeout_duration_minutes": "int",
    "automod_enabled": "bool",
    "automod_anti_spam": "bool",
    "automod_anti_link": "bool",
    "automod_anti_mention_flood": "bool",
    "automod_spam_max_messages": "int",
    "automod_spam_interval_seconds": "int",
    "automod_mention_limit": "int",
    "automod_bypass_role_ids": "role_list",
}


def _to_db_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_role_ids(raw_value: str | None) -> list[int]:
    if not raw_value:
        return []
    role_ids = {int(match) for match in ROLE_ID_RE.findall(raw_value)}
    return sorted(role_ids)


def _serialize_role_ids(role_ids: list[int]) -> str:
    return ",".join(str(role_id) for role_id in sorted(set(role_ids)))


@dataclass(frozen=True)
class MySQLConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    pool_limit: int

    def validate(self) -> None:
        if not DB_IDENTIFIER_RE.fullmatch(self.database):
            raise ValueError(
                "DB_NAME invalido. Use apenas letras, numeros e underscore (_)."
            )


class WarnStore:
    def __init__(self, config: MySQLConfig) -> None:
        self.config = config
        self._pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return

        self.config.validate()
        bootstrap = await aiomysql.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            autocommit=True,
            charset="utf8mb4",
        )
        try:
            async with bootstrap.cursor() as cursor:
                await cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{self.config.database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
        finally:
            bootstrap.close()

        self._pool = await aiomysql.create_pool(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            db=self.config.database,
            minsize=1,
            maxsize=self.config.pool_limit,
            autocommit=True,
            charset="utf8mb4",
        )
        await self._create_schema()

    async def close(self) -> None:
        if self._pool is None:
            return

        self._pool.close()
        await self._pool.wait_closed()
        self._pool = None

    async def _create_schema(self) -> None:
        create_warnings = """
        CREATE TABLE IF NOT EXISTS warnings (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            guild_id BIGINT UNSIGNED NOT NULL,
            user_id BIGINT UNSIGNED NOT NULL,
            moderator_id BIGINT UNSIGNED NOT NULL,
            reason VARCHAR(512) NOT NULL,
            expires_at TIMESTAMP NULL DEFAULT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            INDEX idx_warnings_guild_user (guild_id, user_id),
            INDEX idx_warnings_active (guild_id, user_id, expires_at),
            INDEX idx_warnings_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
        create_guild_settings = """
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id BIGINT UNSIGNED NOT NULL,
            mod_log_channel_id BIGINT UNSIGNED NULL DEFAULT NULL,
            automod_log_channel_id BIGINT UNSIGNED NULL DEFAULT NULL,
            warn_timeout_threshold INT UNSIGNED NOT NULL DEFAULT 3,
            warn_ban_threshold INT UNSIGNED NOT NULL DEFAULT 5,
            warn_expiration_days INT UNSIGNED NOT NULL DEFAULT 60,
            warn_timeout_duration_minutes INT UNSIGNED NOT NULL DEFAULT 60,
            automod_enabled TINYINT(1) NOT NULL DEFAULT 1,
            automod_anti_spam TINYINT(1) NOT NULL DEFAULT 1,
            automod_anti_link TINYINT(1) NOT NULL DEFAULT 1,
            automod_anti_mention_flood TINYINT(1) NOT NULL DEFAULT 1,
            automod_spam_max_messages INT UNSIGNED NOT NULL DEFAULT 5,
            automod_spam_interval_seconds INT UNSIGNED NOT NULL DEFAULT 8,
            automod_mention_limit INT UNSIGNED NOT NULL DEFAULT 5,
            automod_bypass_role_ids TEXT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
        create_infractions = """
        CREATE TABLE IF NOT EXISTS infractions (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            guild_id BIGINT UNSIGNED NOT NULL,
            user_id BIGINT UNSIGNED NOT NULL,
            actor_id BIGINT UNSIGNED NOT NULL,
            action VARCHAR(64) NOT NULL,
            reason VARCHAR(512) NOT NULL,
            related_warning_id BIGINT UNSIGNED NULL DEFAULT NULL,
            expires_at TIMESTAMP NULL DEFAULT NULL,
            metadata TEXT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            INDEX idx_infractions_guild_user (guild_id, user_id, created_at),
            INDEX idx_infractions_action (guild_id, action, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(create_warnings)
                await cursor.execute(create_guild_settings)
                await cursor.execute(create_infractions)
                await self._ensure_warning_expiration_column(cursor)

    async def _ensure_warning_expiration_column(self, cursor: aiomysql.Cursor) -> None:
        await cursor.execute("SHOW COLUMNS FROM warnings LIKE 'expires_at'")
        row = await cursor.fetchone()
        if row is not None:
            return
        await cursor.execute(
            "ALTER TABLE warnings ADD COLUMN expires_at TIMESTAMP NULL DEFAULT NULL AFTER reason"
        )

    @property
    def pool(self) -> aiomysql.Pool:
        if self._pool is None:
            raise RuntimeError("MySQL pool nao inicializado.")
        return self._pool

    async def ensure_guild_settings(self, guild_id: int) -> None:
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO guild_settings (
                        guild_id,
                        mod_log_channel_id,
                        automod_log_channel_id,
                        warn_timeout_threshold,
                        warn_ban_threshold,
                        warn_expiration_days,
                        warn_timeout_duration_minutes,
                        automod_enabled,
                        automod_anti_spam,
                        automod_anti_link,
                        automod_anti_mention_flood,
                        automod_spam_max_messages,
                        automod_spam_interval_seconds,
                        automod_mention_limit,
                        automod_bypass_role_ids
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE guild_id = guild_id
                    """,
                    (
                        guild_id,
                        DEFAULT_GUILD_SETTINGS["mod_log_channel_id"],
                        DEFAULT_GUILD_SETTINGS["automod_log_channel_id"],
                        DEFAULT_GUILD_SETTINGS["warn_timeout_threshold"],
                        DEFAULT_GUILD_SETTINGS["warn_ban_threshold"],
                        DEFAULT_GUILD_SETTINGS["warn_expiration_days"],
                        DEFAULT_GUILD_SETTINGS["warn_timeout_duration_minutes"],
                        int(DEFAULT_GUILD_SETTINGS["automod_enabled"]),
                        int(DEFAULT_GUILD_SETTINGS["automod_anti_spam"]),
                        int(DEFAULT_GUILD_SETTINGS["automod_anti_link"]),
                        int(DEFAULT_GUILD_SETTINGS["automod_anti_mention_flood"]),
                        DEFAULT_GUILD_SETTINGS["automod_spam_max_messages"],
                        DEFAULT_GUILD_SETTINGS["automod_spam_interval_seconds"],
                        DEFAULT_GUILD_SETTINGS["automod_mention_limit"],
                        _serialize_role_ids(DEFAULT_GUILD_SETTINGS["automod_bypass_role_ids"]),
                    ),
                )

    async def get_guild_settings(self, guild_id: int) -> dict[str, Any]:
        await self.ensure_guild_settings(guild_id)
        async with self.pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    """
                    SELECT
                        guild_id,
                        mod_log_channel_id,
                        automod_log_channel_id,
                        warn_timeout_threshold,
                        warn_ban_threshold,
                        warn_expiration_days,
                        warn_timeout_duration_minutes,
                        automod_enabled,
                        automod_anti_spam,
                        automod_anti_link,
                        automod_anti_mention_flood,
                        automod_spam_max_messages,
                        automod_spam_interval_seconds,
                        automod_mention_limit,
                        automod_bypass_role_ids
                    FROM guild_settings
                    WHERE guild_id = %s
                    LIMIT 1
                    """,
                    (guild_id,),
                )
                row = await cursor.fetchone()

        return self._normalize_settings(row, guild_id)

    async def update_guild_settings(self, guild_id: int, **updates: Any) -> dict[str, Any]:
        if not updates:
            return await self.get_guild_settings(guild_id)

        await self.ensure_guild_settings(guild_id)
        clauses: list[str] = []
        values: list[Any] = []
        for field, value in updates.items():
            field_type = SETTINGS_FIELD_TYPES.get(field)
            if field_type is None:
                raise ValueError(f"Campo de configuracao invalido: {field}")
            clauses.append(f"{field} = %s")
            values.append(self._serialize_setting_value(field_type, value))

        values.append(guild_id)
        query = f"UPDATE guild_settings SET {', '.join(clauses)} WHERE guild_id = %s"
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(query, tuple(values))

        return await self.get_guild_settings(guild_id)

    async def add_warning(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        reason: str,
        expires_at: datetime | None,
    ) -> tuple[int, int, int]:
        sanitized_reason = reason.strip()[:512] or "Sem motivo informado."
        db_expires_at = _to_db_datetime(expires_at)
        async with self.pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO warnings (guild_id, user_id, moderator_id, reason, expires_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (guild_id, user_id, moderator_id, sanitized_reason, db_expires_at),
                )
                warning_id = int(cursor.lastrowid or 0)

                await cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COALESCE(SUM(expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP), 0) AS active
                    FROM warnings
                    WHERE guild_id = %s AND user_id = %s
                    """,
                    (guild_id, user_id),
                )
                count_row = await cursor.fetchone()

        total = int(count_row["total"]) if count_row else 0
        active = int(count_row["active"]) if count_row else 0
        return warning_id, total, active

    async def get_warnings(
        self,
        guild_id: int,
        user_id: int,
        limit: int = 10,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        safe_limit = max(1, min(limit, 50))
        async with self.pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COALESCE(SUM(expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP), 0) AS active
                    FROM warnings
                    WHERE guild_id = %s AND user_id = %s
                    """,
                    (guild_id, user_id),
                )
                count_row = await cursor.fetchone()

                await cursor.execute(
                    """
                    SELECT
                        id,
                        moderator_id,
                        reason,
                        created_at,
                        expires_at,
                        (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP) AS is_active
                    FROM warnings
                    WHERE guild_id = %s AND user_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (guild_id, user_id, safe_limit),
                )
                rows = await cursor.fetchall()

        total = int(count_row["total"]) if count_row else 0
        active = int(count_row["active"]) if count_row else 0
        return total, active, list(rows or [])

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    DELETE FROM warnings
                    WHERE guild_id = %s AND user_id = %s
                    """,
                    (guild_id, user_id),
                )
                return int(cursor.rowcount)

    async def log_infraction(
        self,
        guild_id: int,
        user_id: int,
        actor_id: int,
        action: str,
        reason: str,
        *,
        related_warning_id: int | None = None,
        expires_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        clean_action = action.strip()[:64] or "unknown"
        clean_reason = reason.strip()[:512] or "Sem motivo informado."
        serialized_metadata = None
        if metadata:
            serialized_metadata = json.dumps(metadata, ensure_ascii=True, separators=(",", ":"))

        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO infractions (
                        guild_id,
                        user_id,
                        actor_id,
                        action,
                        reason,
                        related_warning_id,
                        expires_at,
                        metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        guild_id,
                        user_id,
                        actor_id,
                        clean_action,
                        clean_reason,
                        related_warning_id,
                        _to_db_datetime(expires_at),
                        serialized_metadata,
                    ),
                )
                return int(cursor.lastrowid or 0)

    async def get_infractions(
        self,
        guild_id: int,
        user_id: int,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 100))
        async with self.pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    """
                    SELECT
                        id,
                        action,
                        actor_id,
                        reason,
                        related_warning_id,
                        expires_at,
                        metadata,
                        created_at
                    FROM infractions
                    WHERE guild_id = %s AND user_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (guild_id, user_id, safe_limit),
                )
                rows = await cursor.fetchall()

        return list(rows or [])

    def _normalize_settings(self, row: dict[str, Any] | None, guild_id: int) -> dict[str, Any]:
        if row is None:
            base = dict(DEFAULT_GUILD_SETTINGS)
            base["guild_id"] = guild_id
            return base

        return {
            "guild_id": int(row["guild_id"]),
            "mod_log_channel_id": int(row["mod_log_channel_id"]) if row["mod_log_channel_id"] else None,
            "automod_log_channel_id": (
                int(row["automod_log_channel_id"]) if row["automod_log_channel_id"] else None
            ),
            "warn_timeout_threshold": int(row["warn_timeout_threshold"]),
            "warn_ban_threshold": int(row["warn_ban_threshold"]),
            "warn_expiration_days": int(row["warn_expiration_days"]),
            "warn_timeout_duration_minutes": int(row["warn_timeout_duration_minutes"]),
            "automod_enabled": bool(row["automod_enabled"]),
            "automod_anti_spam": bool(row["automod_anti_spam"]),
            "automod_anti_link": bool(row["automod_anti_link"]),
            "automod_anti_mention_flood": bool(row["automod_anti_mention_flood"]),
            "automod_spam_max_messages": int(row["automod_spam_max_messages"]),
            "automod_spam_interval_seconds": int(row["automod_spam_interval_seconds"]),
            "automod_mention_limit": int(row["automod_mention_limit"]),
            "automod_bypass_role_ids": _parse_role_ids(row.get("automod_bypass_role_ids")),
        }

    @staticmethod
    def _serialize_setting_value(field_type: str, value: Any) -> Any:
        if field_type == "bool":
            return int(bool(value))
        if field_type == "int":
            return int(value)
        if field_type == "int_or_none":
            if value is None:
                return None
            return int(value)
        if field_type == "role_list":
            if value is None:
                return ""
            if isinstance(value, str):
                return _serialize_role_ids(_parse_role_ids(value))
            if isinstance(value, (list, tuple, set)):
                return _serialize_role_ids([int(item) for item in value])
            raise ValueError("Valor invalido para automod_bypass_role_ids.")
        return value
