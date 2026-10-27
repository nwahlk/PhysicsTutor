"""一次性迁移：在 advisory lock 下串行执行业务表 SQL 与 checkpointer.setup()。

多个 migrate 进程同时启动也安全：拿不到锁的进程轮询等待，拿到锁后发现已全部应用即退出。
"""

import asyncio
import os
import re
import sys
from pathlib import Path

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from tutor.content import seed_content

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
LOCK_KEY = 74_160_001
NAME_PATTERN = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    files = sorted(directory.glob("*.sql"))
    numbers = set()
    for path in files:
        match = NAME_PATTERN.match(path.name)
        if not match:
            raise ValueError(f"迁移文件名不合法: {path.name}（应形如 0001_init.sql）")
        if match.group(1) in numbers:
            raise ValueError(f"迁移编号重复: {match.group(1)}")
        numbers.add(match.group(1))
    return files


def pending_migrations(files: list[Path], applied: set[str]) -> list[Path]:
    return [path for path in files if path.stem not in applied]


async def acquire_lock(conn, timeout: float = 60.0, interval: float = 0.5) -> None:
    # 不用阻塞的 pg_advisory_lock：setup() 里的 CREATE INDEX CONCURRENTLY 会等待
    # 所有在途事务，而阻塞在锁上的会话正是在途事务，两者互等即死锁。
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        cursor = await conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
        if (await cursor.fetchone())[0]:
            return
        if loop.time() >= deadline:
            raise TimeoutError(f"{timeout:.0f}s 内未拿到迁移锁")
        await asyncio.sleep(interval)


async def migrate(conninfo: str) -> list[str]:
    files = list_migrations()
    applied_now = []
    # prepare_threshold=None：迁移文件含多条语句，预编译协议不允许多命令
    async with await psycopg.AsyncConnection.connect(
        conninfo, autocommit=True, prepare_threshold=None
    ) as conn:
        await acquire_lock(conn)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cursor = await conn.execute("SELECT version FROM schema_migrations")
            applied = {row[0] for row in await cursor.fetchall()}
            for path in pending_migrations(files, applied):
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,)
                    )
                applied_now.append(path.stem)
            await seed_content(conn)
            await AsyncPostgresSaver(conn).setup()
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return applied_now


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    conninfo = os.environ.get("DATABASE_URL")
    if not conninfo:
        raise SystemExit("缺少 DATABASE_URL 环境变量")
    applied = asyncio.run(migrate(conninfo))
    print(f"迁移完成，本次应用: {', '.join(applied) or '无（已是最新）'}")


if __name__ == "__main__":
    main()
