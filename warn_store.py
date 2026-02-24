import re
from dataclasses import dataclass
from typing import Any

import aiomysql

DB_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


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
        query = """
        CREATE TABLE IF NOT EXISTS warnings (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            guild_id BIGINT UNSIGNED NOT NULL,
            user_id BIGINT UNSIGNED NOT NULL,
            moderator_id BIGINT UNSIGNED NOT NULL,
            reason VARCHAR(512) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            INDEX idx_warnings_guild_user (guild_id, user_id),
            INDEX idx_warnings_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(query)

    @property
    def pool(self) -> aiomysql.Pool:
        if self._pool is None:
            raise RuntimeError("MySQL pool nao inicializado.")
        return self._pool

    async def add_warning(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        reason: str,
    ) -> tuple[int, int]:
        sanitized_reason = reason.strip()[:512] or "Sem motivo informado."
        async with self.pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO warnings (guild_id, user_id, moderator_id, reason)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (guild_id, user_id, moderator_id, sanitized_reason),
                )
                warning_id = int(cursor.lastrowid or 0)
                await cursor.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM warnings
                    WHERE guild_id = %s AND user_id = %s
                    """,
                    (guild_id, user_id),
                )
                count_row = await cursor.fetchone()

        total = int(count_row["total"]) if count_row else 0
        return warning_id, total

    async def get_warnings(
        self,
        guild_id: int,
        user_id: int,
        limit: int = 10,
    ) -> tuple[int, list[dict[str, Any]]]:
        safe_limit = max(1, min(limit, 50))
        async with self.pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM warnings
                    WHERE guild_id = %s AND user_id = %s
                    """,
                    (guild_id, user_id),
                )
                count_row = await cursor.fetchone()
                await cursor.execute(
                    """
                    SELECT id, moderator_id, reason, created_at
                    FROM warnings
                    WHERE guild_id = %s AND user_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (guild_id, user_id, safe_limit),
                )
                rows = await cursor.fetchall()

        total = int(count_row["total"]) if count_row else 0
        return total, list(rows or [])

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
