#!/usr/bin/env python3
"""Migrate 黑心云 SQLite users into Remnawave without modifying the source DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_PERMANENT_EXPIRE = "2099-12-31T23:59:59Z"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,36}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class LegacyUser:
    legacy_id: int
    email: str
    plan_id: int | None
    status: str
    token: str
    quota_bytes: int
    balance_cents: int
    expire_at: int
    created_at: int
    admin_note: str
    used_traffic_bytes: int
    vless_uuid: str
    multiple_vless_uuids: bool = False


@dataclass
class MigrationItem:
    legacy_id: int
    email: str
    username: str
    internal_squads: list[str]
    short_uuid: str
    vless_uuid: str
    desired_status: str
    traffic_limit_bytes: int
    balance_cents: int
    expire_at: str
    created_at: str
    description: str
    used_traffic_bytes: int
    action: str = "create"
    panel_id: int | None = None
    conflict: str | None = None
    notices: list[str] = field(default_factory=list)


class ApiError(RuntimeError):
    pass


class RemnawaveApi:
    def __init__(self, base_url: str, token: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=payload, headers=headers, method=method)

        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except error.HTTPError as exc:
            if exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", errors="replace")
            raise ApiError(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise ApiError(f"{method} {path} failed: {exc.reason}") from exc

        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(f"{method} {path} returned invalid JSON") from exc

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        path = f"/api/users/by-username/{parse.quote(username, safe='')}"
        return unwrap_response(self.request("GET", path))

    def get_user_by_short_uuid(self, short_uuid: str) -> dict[str, Any] | None:
        path = f"/api/users/by-short-uuid/{parse.quote(short_uuid, safe='')}"
        return unwrap_response(self.request("GET", path))

    def create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        user = unwrap_response(self.request("POST", "/api/users", payload))
        if not isinstance(user, dict) or "id" not in user:
            raise ApiError("create user response did not contain a user id")
        return user

    def update_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        user = unwrap_response(self.request("PATCH", "/api/users", payload))
        if not isinstance(user, dict) or "id" not in user:
            raise ApiError("update user response did not contain a user id")
        return user

    def get_user_metadata(self, user_id: int) -> dict[str, Any]:
        path = f"/api/metadata/user/{user_id}"
        metadata = unwrap_response(self.request("GET", path))
        if metadata is None:
            return {}
        if not isinstance(metadata, dict) or not isinstance(metadata.get("metadata"), dict):
            raise ApiError(f"get metadata response for user {user_id} was invalid")
        return dict(metadata["metadata"])

    def upsert_user_metadata(self, user_id: int, metadata: dict[str, Any]) -> None:
        path = f"/api/metadata/user/{user_id}"
        self.request("PUT", path, {"metadata": metadata})


def unwrap_response(value: Any) -> Any:
    if isinstance(value, dict) and "response" in value:
        return value["response"]
    return value


def iso_from_timestamp(timestamp: int, permanent_expire: str) -> str:
    converted = iso_from_timestamp_optional(timestamp)
    return converted or permanent_expire


def iso_from_timestamp_optional(timestamp: int) -> str:
    timestamp = int(timestamp or 0)
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def desired_status(legacy_status: str, expire_at: int, used_bytes: int, limit_bytes: int, now: int) -> str:
    if expire_at > 0 and expire_at <= now:
        return "EXPIRED"
    if legacy_status != "active":
        return "DISABLED"
    if limit_bytes > 0 and used_bytes >= limit_bytes:
        return "LIMITED"
    return "ACTIVE"


def sanitize_username(email: str, legacy_id: int) -> str:
    local = email.split("@", 1)[0] if email else ""
    normalized = unicodedata.normalize("NFKD", local).encode("ascii", "ignore").decode("ascii")
    candidate = re.sub(r"[^A-Za-z0-9_-]+", "_", normalized).strip("_").lower()
    if len(candidate) < 3:
        candidate = f"user_{legacy_id}"
    if len(candidate) > 36:
        digest = hashlib.sha256(f"{legacy_id}:{email}".encode("utf-8")).hexdigest()[:8]
        candidate = f"{candidate[:27]}_{digest}"
    return candidate


def build_usernames(users: list[LegacyUser]) -> dict[int, str]:
    base_names = {user.legacy_id: sanitize_username(user.email, user.legacy_id) for user in users}
    seen: dict[str, int] = {}
    result: dict[int, str] = {}
    for user in users:
        base = base_names[user.legacy_id]
        if base not in seen:
            seen[base] = user.legacy_id
            result[user.legacy_id] = base
            continue
        suffix = f"_{user.legacy_id}"
        candidate = f"{base[:36 - len(suffix)]}{suffix}"
        if candidate in seen:
            digest = hashlib.sha256(f"{user.legacy_id}:{user.email}".encode("utf-8")).hexdigest()[:8]
            candidate = f"{base[:27]}_{digest}"
        seen[candidate] = user.legacy_id
        result[user.legacy_id] = candidate
    return result


def normalize_uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def deterministic_uuid(short_uuid: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"heixincloud:{short_uuid}"))


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?",
        (table,),
    ).fetchone()
    return row is not None


def load_legacy_users(db_path: Path) -> list[LegacyUser]:
    if not db_path.is_file():
        raise FileNotFoundError(f"legacy database not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        static_usage: dict[int, int] = {}
        managed_usage: dict[int, int] = {}
        client_uuids: dict[int, list[str]] = {}

        if table_exists(conn, "usage_records") and table_exists(conn, "nodes"):
            static_rows = conn.execute(
                """
                select usage_records.user_id as user_id,
                       sum((usage_records.upload + usage_records.download) * coalesce(nodes.rate, 1)) as used
                from usage_records
                join nodes on nodes.id = usage_records.node_id
                where coalesce(nodes.mode, 'static') != 'managed'
                group by usage_records.user_id
                """
            )
            static_usage = {int(row["user_id"]): int(row["used"] or 0) for row in static_rows}

        if table_exists(conn, "managed_clients"):
            if table_exists(conn, "usage_ledgers"):
                managed_rows = conn.execute(
                    """
                    select managed_clients.user_id as user_id,
                           sum(coalesce(usage_ledgers.weighted_up, 0) +
                               coalesce(usage_ledgers.weighted_down, 0)) as used
                    from managed_clients
                    left join usage_ledgers
                      on usage_ledgers.managed_client_id = managed_clients.id
                    group by managed_clients.user_id
                    """
                )
                managed_usage = {int(row["user_id"]): int(row["used"] or 0) for row in managed_rows}

            uuid_rows = conn.execute(
                """
                select user_id, client_uuid
                from managed_clients
                where client_uuid is not null and client_uuid != ''
                order by id
                """
            )
            for row in uuid_rows:
                client_uuids.setdefault(int(row["user_id"]), []).append(str(row["client_uuid"]))

        rows = conn.execute(
            """
                select id, email, role, plan_id, status, token, quota_bytes, balance_cents, expire_at,
                   created_at, admin_note
            from users
            where role = 'user'
            order by id
            """
        )
        users: list[LegacyUser] = []
        for row in rows:
            token = str(row["token"] or "").strip()
            if not token:
                continue
            legacy_id = int(row["id"])
            used = static_usage.get(legacy_id, 0) + managed_usage.get(legacy_id, 0)
            valid_uuids = [
                candidate
                for candidate in (normalize_uuid(value) for value in client_uuids.get(legacy_id, []))
                if candidate
            ]
            chosen_uuid = valid_uuids[0] if valid_uuids else None
            users.append(
                LegacyUser(
                    legacy_id=legacy_id,
                    email=str(row["email"] or "").strip(),
                    plan_id=int(row["plan_id"]) if row["plan_id"] is not None else None,
                    status=str(row["status"] or "disabled").strip().lower(),
                    token=token,
                    quota_bytes=max(0, int(row["quota_bytes"] or 0)),
                    balance_cents=max(0, int(row["balance_cents"] or 0)),
                    expire_at=max(0, int(row["expire_at"] or 0)),
                    created_at=max(0, int(row["created_at"] or 0)),
                    admin_note=str(row["admin_note"] or "").strip(),
                    used_traffic_bytes=used,
                    vless_uuid=chosen_uuid or deterministic_uuid(token),
                    multiple_vless_uuids=len(set(valid_uuids)) > 1,
                )
            )
        return users
    finally:
        conn.close()


def build_plan(
    users: list[LegacyUser],
    *,
    permanent_expire: str,
    internal_squads: list[str],
    plan_squads: dict[int, list[str]] | None = None,
    api: RemnawaveApi | None,
    now: int | None = None,
) -> list[MigrationItem]:
    now = int(time.time()) if now is None else int(now)
    plan_squads = plan_squads or {}
    usernames = build_usernames(users)
    items: list[MigrationItem] = []
    seen_short_uuids: dict[str, int] = {}
    seen_vless_uuids: dict[str, int] = {}

    for user in users:
        assigned_squads = plan_squads.get(user.plan_id or 0, internal_squads)
        item = MigrationItem(
            legacy_id=user.legacy_id,
            email=user.email,
            username=usernames[user.legacy_id],
            internal_squads=assigned_squads,
            short_uuid=user.token,
            vless_uuid=user.vless_uuid,
            desired_status=desired_status(
                user.status,
                user.expire_at,
                user.used_traffic_bytes,
                user.quota_bytes,
                now,
            ),
            traffic_limit_bytes=user.quota_bytes,
            balance_cents=user.balance_cents,
            expire_at=iso_from_timestamp(user.expire_at, permanent_expire),
            created_at=iso_from_timestamp_optional(user.created_at),
            description=(
                f"legacy_id={user.legacy_id}"
                + (f"; {user.admin_note}" if user.admin_note else "")
            ),
            used_traffic_bytes=user.used_traffic_bytes,
        )

        if not USERNAME_RE.fullmatch(item.username):
            item.conflict = f"invalid generated username: {item.username}"
        if item.short_uuid in seen_short_uuids:
            item.conflict = (
                f"shortUuid duplicates legacy user {seen_short_uuids[item.short_uuid]}"
            )
        else:
            seen_short_uuids[item.short_uuid] = item.legacy_id
        if item.vless_uuid in seen_vless_uuids:
            item.notices.append(
                f"vlessUuid duplicates legacy user {seen_vless_uuids[item.vless_uuid]}; "
                "replace this UUID with a deterministic UUID before apply"
            )
            item.vless_uuid = deterministic_uuid(f"{item.short_uuid}:{item.legacy_id}")
        seen_vless_uuids[item.vless_uuid] = item.legacy_id
        if user.multiple_vless_uuids:
            item.notices.append(
                "legacy managed clients contain multiple different VLESS UUIDs; "
                "Remnawave uses one vlessUuid per user, so clients must refresh the existing subscription"
            )

        if assigned_squads:
            item.notices.append(f"activeInternalSquads={','.join(assigned_squads)}")
        if item.balance_cents:
            item.notices.append(f"balanceCents={item.balance_cents}")
        items.append(item)

    if api is None:
        return items

    for item in items:
        if item.conflict:
            continue
        existing = api.get_user_by_username(item.username)
        short_uuid_owner = api.get_user_by_short_uuid(item.short_uuid)

        if short_uuid_owner is not None:
            owner_username = str(short_uuid_owner.get("username") or "")
            if owner_username != item.username:
                item.conflict = (
                    f"shortUuid already belongs to Remnawave user {owner_username or '<unknown>'}"
                )
                continue

        if existing is None:
            item.action = "create"
            continue

        existing_short_uuid = str(existing.get("shortUuid") or "")
        if existing_short_uuid != item.short_uuid:
            item.conflict = (
                f"username exists with shortUuid {existing_short_uuid}; "
                f"expected {item.short_uuid}"
            )
            continue

        item.action = "update"
        item.panel_id = int(existing["id"])
        existing_status = str(existing.get("status") or "")
        if item.desired_status in {"EXPIRED", "LIMITED"} and existing_status != item.desired_status:
            item.conflict = (
                f"existing status {existing_status or '<unknown>'} cannot be changed to "
                f"{item.desired_status} through PATCH; resolve the Panel user before apply"
            )
    return items


def create_payload(item: MigrationItem) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "username": item.username,
        "status": item.desired_status,
        "shortUuid": item.short_uuid,
        "vlessUuid": item.vless_uuid,
        "trafficLimitBytes": item.traffic_limit_bytes,
        "trafficLimitStrategy": "NO_RESET",
        "expireAt": item.expire_at,
        "description": item.description,
        "tag": "HEIXIN_LEGACY",
    }
    if item.created_at:
        payload["createdAt"] = item.created_at
    if item.email and EMAIL_RE.fullmatch(item.email):
        payload["email"] = item.email
    if item.internal_squads:
        payload["activeInternalSquads"] = item.internal_squads
    return payload


def update_payload(
    item: MigrationItem,
    existing: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": int(existing["id"]),
        "trafficLimitBytes": item.traffic_limit_bytes,
        "trafficLimitStrategy": "NO_RESET",
        "description": item.description,
        "tag": "HEIXIN_LEGACY",
    }
    if item.email and EMAIL_RE.fullmatch(item.email):
        payload["email"] = item.email
    if item.internal_squads:
        payload["activeInternalSquads"] = item.internal_squads

    if item.desired_status in {"ACTIVE", "DISABLED"}:
        payload["status"] = item.desired_status
        if parse_iso(item.expire_at) > datetime.now(timezone.utc):
            payload["expireAt"] = item.expire_at
    return payload


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "users": {}}
    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict) or not isinstance(state.get("users"), dict):
        raise ValueError(f"invalid migration state file: {path}")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temp_path.replace(path)


def write_traffic_sql(path: Path, items: list[MigrationItem]) -> None:
    """Emit SQL that works whether user_traffic keys on `id` or the old `t_id`."""
    targets = [item for item in items if item.panel_id is not None]
    with path.open("w", encoding="utf-8") as handle:
        handle.write("-- Generated by tools/heixin-migrate/migrate.py\n")
        handle.write("-- Column name is detected at runtime; safety of review still applies.\n")
        handle.write("BEGIN;\n")
        handle.write("DO $$\nDECLARE\n    id_col text;\nBEGIN\n")
        handle.write(
            "    SELECT column_name INTO id_col FROM information_schema.columns\n"
            "     WHERE table_schema = 'public' AND table_name = 'user_traffic'\n"
            "       AND column_name IN ('id', 't_id')\n"
            "     ORDER BY (column_name = 'id') DESC\n"
            "     LIMIT 1;\n"
        )
        handle.write(
            "    IF id_col IS NULL THEN\n"
            "        RAISE EXCEPTION 'user_traffic has neither id nor t_id column';\n"
            "    END IF;\n\n"
        )
        for item in targets:
            used = item.used_traffic_bytes
            handle.write(
                "    EXECUTE format('UPDATE public.user_traffic "
                f"SET used_traffic_bytes = {used}, "
                f"lifetime_used_traffic_bytes = GREATEST(lifetime_used_traffic_bytes, {used}) "
                f"WHERE %I = {item.panel_id}', id_col);\n"
            )
        handle.write("END $$;\nCOMMIT;\n")


def write_balance_metadata(api: RemnawaveApi, item: MigrationItem) -> None:
    metadata = api.get_user_metadata(item.panel_id or 0)
    heixincloud = metadata.get("heixincloud")
    if not isinstance(heixincloud, dict):
        heixincloud = {}
    heixincloud = {
        **heixincloud,
        "balanceCents": item.balance_cents,
        "legacyUserId": item.legacy_id,
    }
    metadata["heixincloud"] = heixincloud
    api.upsert_user_metadata(item.panel_id or 0, metadata)


def apply_plan(
    api: RemnawaveApi,
    items: list[MigrationItem],
    *,
    state_path: Path,
    sql_path: Path,
) -> None:
    conflicts = [item for item in items if item.conflict]
    if conflicts:
        details = "\n".join(f"  legacy #{item.legacy_id}: {item.conflict}" for item in conflicts)
        raise RuntimeError(f"migration aborted because conflicts exist:\n{details}")

    state = load_state(state_path)
    for index, item in enumerate(items, start=1):
        existing = api.get_user_by_username(item.username)
        if existing is None:
            user = api.create_user(create_payload(item))
            action = "created"
        else:
            existing_short_uuid = str(existing.get("shortUuid") or "")
            if existing_short_uuid != item.short_uuid:
                raise ApiError(
                    f"username {item.username} changed during migration; "
                    f"expected {item.short_uuid}, got {existing_short_uuid}"
                )
            user = api.update_user(update_payload(item, existing))
            action = "updated"

        item.panel_id = int(user["id"])
        write_balance_metadata(api, item)
        state["users"][str(item.legacy_id)] = {
            "username": item.username,
            "shortUuid": item.short_uuid,
            "panelId": item.panel_id,
            "usedTrafficBytes": item.used_traffic_bytes,
            "balanceCents": item.balance_cents,
        }
        save_state(state_path, state)
        print(f"[{index}/{len(items)}] {action} legacy #{item.legacy_id} -> {item.username}")

    write_traffic_sql(sql_path, items)


def parse_plan_squads(values: list[str]) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {}
    for value in values:
        plan_id_text, separator, squad_text = value.partition("=")
        if not separator or not plan_id_text.isdigit():
            raise ValueError(f"invalid --plan-squad value: {value}; expected PLAN_ID=SQUAD_UUID")
        squad = normalize_uuid(squad_text)
        if squad is None:
            raise ValueError(f"invalid internal squad UUID in --plan-squad: {value}")
        result.setdefault(int(plan_id_text), []).append(squad)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run or apply a 黑心云 SQLite to Remnawave user migration."
    )
    parser.add_argument("--db", required=True, type=Path, help="legacy app.db read-only copy")
    parser.add_argument("--api-url", help="Remnawave Panel base URL, for example https://panel.example.com")
    parser.add_argument(
        "--api-token",
        default=os.environ.get("REMNAWAVE_API_TOKEN", ""),
        help="Panel API token; defaults to REMNAWAVE_API_TOKEN",
    )
    parser.add_argument(
        "--internal-squad",
        action="append",
        default=[],
        help="fallback internal squad UUID; can be repeated",
    )
    parser.add_argument(
        "--plan-squad",
        action="append",
        default=[],
        metavar="PLAN_ID=SQUAD_UUID",
        help="map legacy plan_id to a Remnawave internal squad; can be repeated",
    )
    parser.add_argument(
        "--permanent-expire",
        default=DEFAULT_PERMANENT_EXPIRE,
        help=f"expiry for legacy expire_at=0 users (default: {DEFAULT_PERMANENT_EXPIRE})",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("heixin-migration-state.json"),
        help="idempotency state file written only with --apply",
    )
    parser.add_argument(
        "--traffic-sql",
        type=Path,
        default=Path("heixin-traffic-import.sql"),
        help="SQL file generated after --apply",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write to Remnawave; without this flag the command is read-only",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        args.plan_squads = parse_plan_squads(args.plan_squad)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        parse_iso(args.permanent_expire)
    except ValueError:
        print("--permanent-expire must be an ISO-8601 timestamp", file=sys.stderr)
        return 2

    api = None
    if args.api_url:
        if not args.api_token:
            print("--api-token or REMNAWAVE_API_TOKEN is required with --api-url", file=sys.stderr)
            return 2
        api = RemnawaveApi(args.api_url, args.api_token, args.timeout)
    elif args.apply:
        print("--apply requires --api-url and an API token", file=sys.stderr)
        return 2

    try:
        users = load_legacy_users(args.db)
        items = build_plan(
            users,
            permanent_expire=args.permanent_expire,
            internal_squads=args.internal_squad,
            plan_squads=args.plan_squads,
            api=api,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1

    creates = sum(1 for item in items if item.action == "create")
    updates = sum(1 for item in items if item.action == "update")
    conflicts = sum(1 for item in items if item.conflict)
    notices = sum(1 for item in items if item.notices)
    print(
        f"legacy_users={len(items)} create={creates} update={updates} "
        f"conflicts={conflicts} notices={notices}"
    )
    for item in items:
        if item.conflict:
            print(f"CONFLICT legacy #{item.legacy_id}: {item.conflict}")
        for notice in item.notices:
            print(f"NOTICE legacy #{item.legacy_id}: {notice}")

    if not args.apply:
        if conflicts:
            print("dry-run completed; conflicts must be resolved before --apply")
        else:
            print("dry-run completed; no changes were made")
        return 0 if not conflicts else 1

    if api is None:
        print("--apply requires API configuration", file=sys.stderr)
        return 2
    try:
        apply_plan(
            api,
            items,
            state_path=args.state,
            sql_path=args.traffic_sql,
        )
    except (ApiError, OSError, ValueError, RuntimeError) as exc:
        print(f"apply failed: {exc}", file=sys.stderr)
        return 1

    print(f"state written: {args.state}")
    print(f"historical traffic SQL written: {args.traffic_sql}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
