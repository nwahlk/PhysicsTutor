import asyncio
from pathlib import Path

import pytest

from migrate import acquire_lock, list_migrations, pending_migrations
from tutor.catalog import CatalogError, load_catalog, parse_catalog


def test_real_catalog_is_valid():
    catalog = load_catalog()
    assert catalog.subject == "physics"
    assert [chapter["name"][:3] for chapter in catalog.tree] == ["第一章", "第二章", "第三章", "第四章"]
    assert len(catalog.points) >= 40
    assert catalog.get("b1.4.3.1").name == "牛顿第二定律"
    assert "b1.3.3.3" in catalog.get("b1.4.3.1").prereqs
    covered = [kp for kp in catalog.points.values() if kp.chapter_id in {"b1.2", "b1.4"}]
    assert covered
    assert all(kp.card is not None for kp in covered)
    assert catalog.get("b1.4.3.1").card.law == "F合 = ma"


def _raw(points):
    return {
        "subject": "physics",
        "textbook": "t",
        "chapters": [{"id": "c", "name": "c", "sections": [{"id": "s", "name": "s", "points": points}]}],
    }


@pytest.mark.parametrize(
    ("points", "message"),
    [
        ([{"id": "a", "name": "A"}, {"id": "a", "name": "A2"}], "重复"),
        ([{"id": "a", "name": "A", "prereqs": ["x"]}], "不存在"),
        (
            [{"id": "a", "name": "A", "prereqs": ["b"]}, {"id": "b", "name": "B", "prereqs": ["a"]}],
            "成环",
        ),
        ([{"id": "a", "name": "A", "prereqs": ["a"]}], "成环"),
        ([{"id": "s", "name": "与小节同名"}], "重复"),
    ],
)
def test_catalog_rejects_bad_data(points, message):
    with pytest.raises(CatalogError, match=message):
        parse_catalog(_raw(points))


def test_migration_files_are_ordered_and_named():
    files = list_migrations()
    assert files[0].name == "0001_init.sql"
    assert pending_migrations(files, {"0001_init"}) == files[1:]


def test_migration_rejects_bad_names(tmp_path: Path):
    (tmp_path / "init.sql").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="文件名不合法"):
        list_migrations(tmp_path)


def test_migration_rejects_duplicate_numbers(tmp_path: Path):
    (tmp_path / "0001_a.sql").write_text("", encoding="utf-8")
    (tmp_path / "0001_b.sql").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="编号重复"):
        list_migrations(tmp_path)


class FakeLockConn:
    def __init__(self, results):
        self.results = list(results)
        self.sql = []

    async def execute(self, sql, params=None):
        self.sql.append(sql)
        result = self.results.pop(0)

        class Cursor:
            async def fetchone(self):
                return (result,)

        return Cursor()


def test_acquire_lock_polls_non_blocking_until_free():
    conn = FakeLockConn([False, False, True])
    asyncio.run(acquire_lock(conn, timeout=5, interval=0))
    assert len(conn.sql) == 3
    assert all("pg_try_advisory_lock" in sql for sql in conn.sql)


def test_acquire_lock_times_out():
    conn = FakeLockConn([False] * 100)
    with pytest.raises(TimeoutError, match="迁移锁"):
        asyncio.run(acquire_lock(conn, timeout=0, interval=0))
