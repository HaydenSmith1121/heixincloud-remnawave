#!/usr/bin/env python3

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

import migrate


class FakeApi:
    def __init__(self, users: list[dict[str, object]]) -> None:
        self.users = users

    def get_user_by_username(self, username: str) -> dict[str, object] | None:
        return next((user for user in self.users if user["username"] == username), None)

    def get_user_by_short_uuid(self, short_uuid: str) -> dict[str, object] | None:
        return next((user for user in self.users if user["shortUuid"] == short_uuid), None)


class MigrationTests(unittest.TestCase):
    def make_db(self) -> Path:
        path = Path(tempfile.mkdtemp()) / "app.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            create table users (
                id integer primary key,
                email text,
                role text,
                plan_id integer,
                status text,
                token text,
                quota_bytes integer,
                expire_at integer,
                created_at integer,
                admin_note text
            );
            create table nodes (
                id integer primary key,
                mode text,
                rate real
            );
            create table usage_records (
                user_id integer,
                node_id integer,
                upload integer,
                download integer
            );
            create table managed_clients (
                id integer primary key,
                user_id integer,
                client_uuid text
            );
            create table usage_ledgers (
                managed_client_id integer,
                weighted_up integer,
                weighted_down integer
            );
            """
        )
        now = 1_700_000_000
        conn.executemany(
            "insert into users values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "alice@example.com", "user", 10, "active", "token-alice", 1000, now + 3600, now, "vip"),
                (2, "alice@example.net", "user", 20, "active", "token-bob", 1000, now - 3600, now, ""),
                (3, "expired@example.com", "user", None, "disabled", "token-expired", 0, 0, 0, ""),
                (9, "admin@example.com", "admin", None, "active", "admin-token", 0, 0, now, ""),
            ],
        )
        conn.executemany(
            "insert into nodes values (?, ?, ?)",
            [(1, "static", 2.0), (2, "managed", 3.0)],
        )
        conn.executemany(
            "insert into usage_records values (?, ?, ?, ?)",
            [(1, 1, 100, 200), (1, 2, 999, 999)],
        )
        first_uuid = str(uuid.uuid4())
        conn.execute(
            "insert into managed_clients values (?, ?, ?)",
            (1, 1, first_uuid),
        )
        conn.execute(
            "insert into managed_clients values (?, ?, ?)",
            (2, 1, str(uuid.uuid4())),
        )
        conn.execute(
            "insert into usage_ledgers values (?, ?, ?)",
            (1, 30, 70),
        )
        conn.commit()
        conn.close()
        return path

    def test_plan_preserves_token_status_and_weighted_usage(self) -> None:
        users = migrate.load_legacy_users(self.make_db())
        self.assertEqual([user.legacy_id for user in users], [1, 2, 3])
        self.assertEqual(users[0].used_traffic_bytes, 700)
        self.assertTrue(users[0].multiple_vless_uuids)

        now = 1_700_000_100
        items = migrate.build_plan(
            users,
            permanent_expire=migrate.DEFAULT_PERMANENT_EXPIRE,
            internal_squads=[],
            plan_squads={10: ["00000000-0000-0000-0000-000000000010"]},
            api=None,
            now=now,
        )
        by_id = {item.legacy_id: item for item in items}
        self.assertEqual(by_id[1].short_uuid, "token-alice")
        self.assertEqual(by_id[1].desired_status, "ACTIVE")
        self.assertEqual(by_id[1].traffic_limit_bytes, 1000)
        self.assertTrue(
            any("multiple different VLESS UUIDs" in notice for notice in by_id[1].notices)
        )
        self.assertNotEqual(by_id[1].username, by_id[2].username)
        self.assertEqual(by_id[2].desired_status, "EXPIRED")
        self.assertEqual(by_id[3].expire_at, migrate.DEFAULT_PERMANENT_EXPIRE)
        self.assertEqual(by_id[3].created_at, "")
        self.assertEqual(by_id[1].internal_squads, ["00000000-0000-0000-0000-000000000010"])
        self.assertNotIn("createdAt", migrate.create_payload(by_id[3]))

    def test_panel_status_conflict_blocks_apply(self) -> None:
        users = migrate.load_legacy_users(self.make_db())
        existing = {
            "id": 42,
            "username": "alice_2",
            "shortUuid": "token-bob",
            "status": "ACTIVE",
        }
        items = migrate.build_plan(
            users,
            permanent_expire=migrate.DEFAULT_PERMANENT_EXPIRE,
            internal_squads=[],
            plan_squads={},
            api=FakeApi([existing]),
            now=1_700_000_100,
        )
        expired = next(item for item in items if item.legacy_id == 2)
        self.assertIn("cannot be changed to EXPIRED", expired.conflict or "")

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(RuntimeError):
                migrate.apply_plan(
                    FakeApi([existing]),
                    items,
                    state_path=Path(temp_dir) / "state.json",
                    sql_path=Path(temp_dir) / "traffic.sql",
                )

    def test_plan_squad_parser_rejects_bad_input(self) -> None:
        squad = "00000000-0000-0000-0000-000000000010"
        self.assertEqual(
            migrate.parse_plan_squads([f"10={squad}", f"10={squad}"]),
            {10: [squad, squad]},
        )
        for value in ["10", "x=00000000-0000-0000-0000-000000000010", "10=not-a-uuid"]:
            with self.assertRaises(ValueError):
                migrate.parse_plan_squads([value])

    def test_traffic_sql_detects_column_name(self) -> None:
        items = [
            migrate.MigrationItem(
                legacy_id=1,
                email="a@example.com",
                username="alice",
                internal_squads=[],
                short_uuid="token-alice",
                vless_uuid=str(uuid.uuid4()),
                desired_status="ACTIVE",
                traffic_limit_bytes=1000,
                expire_at=migrate.DEFAULT_PERMANENT_EXPIRE,
                created_at="",
                description="legacy_id=1",
                used_traffic_bytes=700,
                panel_id=42,
            ),
            migrate.MigrationItem(
                legacy_id=2,
                email="b@example.com",
                username="bob",
                internal_squads=[],
                short_uuid="token-bob",
                vless_uuid=str(uuid.uuid4()),
                desired_status="ACTIVE",
                traffic_limit_bytes=0,
                expire_at=migrate.DEFAULT_PERMANENT_EXPIRE,
                created_at="",
                description="legacy_id=2",
                used_traffic_bytes=0,
                panel_id=43,
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            sql_path = Path(temp_dir) / "traffic.sql"
            migrate.write_traffic_sql(sql_path, items)
            sql = sql_path.read_text(encoding="utf-8")

        self.assertIn("information_schema.columns", sql)
        self.assertIn("'id', 't_id'", sql)
        self.assertIn("id_col", sql)
        self.assertIn("%I = 42', id_col)", sql)
        self.assertIn("%I = 43', id_col)", sql)
        self.assertIn("used_traffic_bytes = 700", sql)
        self.assertNotIn("WHERE t_id =", sql)


if __name__ == "__main__":
    unittest.main()
