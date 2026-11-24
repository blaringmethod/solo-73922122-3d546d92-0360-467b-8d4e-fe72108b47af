"""Application services: all validation, versioning and persistence rules.

Design notes
------------
* An area's ``version`` is bumped by every change to its inventory (a layer
  being added) or by a published relation batch.  A submitted batch carries the
  version its curator saw; if anything in the same area was published first,
  the batch is rejected wholesale.
* A site-wide ``published_version`` is bumped by every published change.  It is
  the marker used by rank queries to make clear which snapshot they describe.
* A record is never deleted.  Withdrawing it sets ``withdrawn_at``.  An edge
  between two layers exists while *at least one* active record states it, which
  is why withdrawing one duplicate record does not yet remove the constraint.
* Every write holds SQLite's write lock from before the version check until
  commit, so concurrent submissions are serialised.  Readers use WAL snapshots
  and keep seeing the last published state while a writer validates.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .db import read_transaction, write_transaction
from .errors import (
    BatchValidationError,
    ConflictError,
    CycleDetectedError,
    NotFoundError,
    StorageUnavailableError,
    VersionConflictError,
)
from .graph import CyclicGraphError, compute_rank, find_cycle

DB = Path | str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def _site_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT published_version FROM site_state WHERE id = 1"
    ).fetchone()
    return int(row["published_version"])


def _bump_site_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "UPDATE site_state SET published_version = published_version + 1 WHERE id = 1"
    )
    return _site_version(conn)


def _area_versions(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT code, version FROM areas ORDER BY code").fetchall()
    return {row["code"]: int(row["version"]) for row in rows}


def _area_row(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM areas WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise NotFoundError(f"分区 {code!r} 不存在")
    return row


def _layer_ref_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "area": row["area"],
        "code": row["code"],
        "name": row["name"],
    }


def _layer_refs(
    conn: sqlite3.Connection, ids: Iterable[int]
) -> dict[int, dict[str, Any]]:
    id_list = list(dict.fromkeys(ids))
    if not id_list:
        return {}
    placeholders = ",".join("?" for _ in id_list)
    rows = conn.execute(
        f"""
        SELECT l.id, a.code AS area, l.code, l.name
        FROM layers l JOIN areas a ON a.id = l.area_id
        WHERE l.id IN ({placeholders})
        """,
        id_list,
    ).fetchall()
    return {int(row["id"]): _layer_ref_from_row(row) for row in rows}


def _record_out(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "record_number": row["record_number"],
        "owner_area": row["owner_area"],
        "earlier": {
            "id": int(row["earlier_id"]),
            "area": row["earlier_area"],
            "code": row["earlier_code"],
            "name": row["earlier_name"],
        },
        "later": {
            "id": int(row["later_id"]),
            "area": row["later_area"],
            "code": row["later_code"],
            "name": row["later_name"],
        },
        "active": row["withdrawn_at"] is None,
        "created_at": row["created_at"],
        "withdrawn_at": row["withdrawn_at"],
        "withdrawal_reason": row["withdrawal_reason"],
        "created_by_batch_id": row["created_by_batch_id"],
        "withdrawn_by_batch_id": row["withdrawn_by_batch_id"],
    }


_RECORD_SELECT = """
SELECT r.id,
       r.record_number,
       a.code AS owner_area,
       r.earlier_layer_id AS earlier_id,
       ea.code AS earlier_area, el.code AS earlier_code, el.name AS earlier_name,
       r.later_layer_id AS later_id,
       la.code AS later_area, ll.code AS later_code, ll.name AS later_name,
       r.created_at,
       r.withdrawn_at,
       r.withdrawal_reason,
       r.created_by_batch_id,
       r.withdrawn_by_batch_id
FROM records r
JOIN areas a ON a.id = r.area_id
JOIN layers el ON el.id = r.earlier_layer_id
JOIN areas ea ON ea.id = el.area_id
JOIN layers ll ON ll.id = r.later_layer_id
JOIN areas la ON la.id = ll.area_id
"""


def _fetch_records_by_ids(
    conn: sqlite3.Connection, ids: Iterable[int]
) -> list[dict[str, Any]]:
    id_list = list(dict.fromkeys(ids))
    if not id_list:
        return []
    placeholders = ",".join("?" for _ in id_list)
    rows = conn.execute(
        f"{_RECORD_SELECT} WHERE r.id IN ({placeholders}) ORDER BY r.id",
        id_list,
    ).fetchall()
    return [_record_out(row) for row in rows]


# ---------------------------------------------------------------------------
# Areas and layers
# ---------------------------------------------------------------------------


def create_area(db: DB, *, code: str, name: str, busy_timeout_ms: int) -> dict[str, Any]:
    now = utc_now()
    try:
        with write_transaction(db, busy_timeout_ms) as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO areas (code, name, version, created_at) "
                    "VALUES (?, ?, 0, ?)",
                    (code, name, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"分区编码 {code!r} 已存在") from exc
            area_id = int(cur.lastrowid)
            site_version = _bump_site_version(conn)
            return {
                "id": area_id,
                "code": code,
                "name": name,
                "version": 0,
                "layer_count": 0,
                "active_record_count": 0,
                "created_at": now,
                "site_version": site_version,
            }
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc):
            raise StorageUnavailableError(
                "数据库正忙，分区未创建，请稍后重试"
            ) from exc
        raise


def list_areas(db: DB, busy_timeout_ms: int) -> list[dict[str, Any]]:
    with read_transaction(db, busy_timeout_ms) as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.code, a.name, a.version, a.created_at,
                   (SELECT COUNT(*) FROM layers l WHERE l.area_id = a.id) AS layer_count,
                   (SELECT COUNT(*) FROM records r
                    WHERE r.area_id = a.id AND r.withdrawn_at IS NULL) AS active_record_count
            FROM areas a ORDER BY a.code
            """
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "code": row["code"],
                "name": row["name"],
                "version": int(row["version"]),
                "layer_count": int(row["layer_count"]),
                "active_record_count": int(row["active_record_count"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]


def get_area(db: DB, code: str, busy_timeout_ms: int) -> dict[str, Any]:
    with read_transaction(db, busy_timeout_ms) as conn:
        row = _area_row(conn, code)
        layer_count = conn.execute(
            "SELECT COUNT(*) AS n FROM layers WHERE area_id = ?", (row["id"],)
        ).fetchone()["n"]
        active_count = conn.execute(
            "SELECT COUNT(*) AS n FROM records "
            "WHERE area_id = ? AND withdrawn_at IS NULL",
            (row["id"],),
        ).fetchone()["n"]
        return {
            "id": int(row["id"]),
            "code": row["code"],
            "name": row["name"],
            "version": int(row["version"]),
            "layer_count": int(layer_count),
            "active_record_count": int(active_count),
            "created_at": row["created_at"],
        }


def create_layer(
    db: DB,
    area_code: str,
    *,
    code: str,
    name: str,
    description: str | None,
    busy_timeout_ms: int,
) -> dict[str, Any]:
    now = utc_now()
    try:
        with write_transaction(db, busy_timeout_ms) as conn:
            area = _area_row(conn, area_code)
            try:
                cur = conn.execute(
                    """
                    INSERT INTO layers (area_id, code, name, description, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (area["id"], code, name, description, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"分区 {area_code!r} 内已存在地层编号 {code!r}"
                ) from exc
            layer_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE areas SET version = version + 1 WHERE id = ?", (area["id"],)
            )
            _bump_site_version(conn)
            new_version = int(
                conn.execute(
                    "SELECT version FROM areas WHERE id = ?", (area["id"],)
                ).fetchone()["version"]
            )
            return {
                "id": layer_id,
                "area": area_code,
                "code": code,
                "name": name,
                "description": description,
                "created_at": now,
                "area_version": new_version,
            }
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc):
            raise StorageUnavailableError(
                "数据库正忙，地层未写入，请稍后重试"
            ) from exc
        raise


def list_layers(db: DB, area_code: str, busy_timeout_ms: int) -> list[dict[str, Any]]:
    with read_transaction(db, busy_timeout_ms) as conn:
        area = _area_row(conn, area_code)
        rows = conn.execute(
            "SELECT id, code, name, description, created_at FROM layers "
            "WHERE area_id = ? ORDER BY code",
            (area["id"],),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "area": area_code,
                "code": row["code"],
                "name": row["name"],
                "description": row["description"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]


def get_layer(db: DB, layer_id: int, busy_timeout_ms: int) -> dict[str, Any]:
    with read_transaction(db, busy_timeout_ms) as conn:
        row = conn.execute(
            """
            SELECT l.id, a.code AS area, l.code, l.name, l.description, l.created_at
            FROM layers l JOIN areas a ON a.id = l.area_id WHERE l.id = ?
            """,
            (layer_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"地层 {layer_id} 不存在")
        return {
            "id": int(row["id"]),
            "area": row["area"],
            "code": row["code"],
            "name": row["name"],
            "description": row["description"],
            "created_at": row["created_at"],
        }


def list_records(
    db: DB, area_code: str, *, include_withdrawn: bool, busy_timeout_ms: int
) -> list[dict[str, Any]]:
    with read_transaction(db, busy_timeout_ms) as conn:
        area = _area_row(conn, area_code)
        sql = f"{_RECORD_SELECT} WHERE r.area_id = ?"
        params: list[Any] = [area["id"]]
        if not include_withdrawn:
            sql += " AND r.withdrawn_at IS NULL"
        sql += " ORDER BY r.record_number, r.id"
        rows = conn.execute(sql, params).fetchall()
        return [_record_out(row) for row in rows]


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


def _active_edges(
    conn: sqlite3.Connection,
) -> tuple[
    dict[tuple[int, int], list[int]],
    dict[int, str],
]:
    """Return active edges -> record ids, plus record id -> number.

    An edge is present while at least one active record states it; duplicate
    records therefore keep it alive until all of them are withdrawn.
    """

    rows = conn.execute(
        "SELECT id, record_number, earlier_layer_id, later_layer_id "
        "FROM records WHERE withdrawn_at IS NULL ORDER BY id"
    ).fetchall()
    edges: dict[tuple[int, int], list[int]] = defaultdict(list)
    numbers: dict[int, str] = {}
    for row in rows:
        rid = int(row["id"])
        edges[
            (int(row["earlier_layer_id"]), int(row["later_layer_id"]))
        ].append(rid)
        numbers[rid] = row["record_number"]
    return dict(edges), numbers


def _cycle_payload(
    conn: sqlite3.Connection,
    cycle_nodes: list[int],
    edges: dict[tuple[int, int], list[int]],
    numbers: dict[int, str],
) -> dict[str, Any]:
    refs = _layer_refs(conn, cycle_nodes)
    pairs: list[tuple[int, int]] = [
        (cycle_nodes[index], cycle_nodes[index + 1])
        for index in range(len(cycle_nodes) - 1)
    ]
    pairs.append((cycle_nodes[-1], cycle_nodes[0]))

    edge_models = []
    all_numbers: list[str] = []
    for earlier, later in pairs:
        pair_numbers = sorted(numbers[rid] for rid in edges.get((earlier, later), []))
        all_numbers.extend(pair_numbers)
        edge_models.append(
            {
                "earlier": refs[earlier],
                "later": refs[later],
                "record_numbers": pair_numbers,
            }
        )
    return {
        "layers": [refs[node] for node in cycle_nodes],
        "edges": edge_models,
        "record_numbers": sorted(set(all_numbers)),
    }


def submit_batch(
    db: DB,
    area_code: str,
    *,
    base_version: int,
    additions: list[dict[str, Any]],
    withdrawals: list[dict[str, Any]],
    note: str | None,
    busy_timeout_ms: int,
) -> dict[str, Any]:
    """Apply a whole batch atomically or reject it whole."""

    now = utc_now()
    try:
        with write_transaction(db, busy_timeout_ms) as conn:
            area = _area_row(conn, area_code)
            current_version = int(area["version"])
            if current_version != base_version:
                raise VersionConflictError(
                    "分区版本已变更，整单退回；请重新读取后提交",
                    base_version=base_version,
                    current_version=current_version,
                )

            errors: list[str] = []

            add_numbers = [item["record_number"] for item in additions]
            withdraw_numbers = [item["record_number"] for item in withdrawals]
            duplicate_adds = sorted(
                number
                for number, count in Counter(add_numbers).items()
                if count > 1
            )
            duplicate_withdrawals = sorted(
                number
                for number, count in Counter(withdraw_numbers).items()
                if count > 1
            )
            overlap = sorted(set(add_numbers) & set(withdraw_numbers))
            if duplicate_adds:
                errors.append(f"批次内补录记录号重复: {duplicate_adds}")
            if duplicate_withdrawals:
                errors.append(f"批次内撤回记录号重复: {duplicate_withdrawals}")
            if overlap:
                errors.append(f"同一记录号不能在批次内又补录又撤回: {overlap}")

            # Record numbers are historical identifiers: withdrawn numbers may
            # not be reused, keeping citations in the archive unambiguous.
            existing_numbers: dict[str, sqlite3.Row] = {}
            if add_numbers:
                placeholders = ",".join("?" for _ in add_numbers)
                for row in conn.execute(
                    f"SELECT id, record_number, withdrawn_at FROM records "
                    f"WHERE area_id = ? AND record_number IN ({placeholders})",
                    [area["id"], *add_numbers],
                ):
                    existing_numbers[row["record_number"]] = row
            reused = sorted(existing_numbers)
            if reused:
                errors.append(
                    "记录号在分区内已存在（含已撤回，记录号不可复用）: "
                    f"{reused}"
                )

            # Endpoints must exist; relations may join layers of other areas
            # because the archive merges trench sequences.
            endpoint_ids = {item["earlier_layer_id"] for item in additions} | {
                item["later_layer_id"] for item in additions
            }
            existing_layers: set[int] = set()
            if endpoint_ids:
                id_list = sorted(endpoint_ids)
                placeholders = ",".join("?" for _ in id_list)
                existing_layers = {
                    int(row["id"])
                    for row in conn.execute(
                        f"SELECT id FROM layers WHERE id IN ({placeholders})", id_list
                    )
                }
            missing_layers = sorted(endpoint_ids - existing_layers)
            if missing_layers:
                errors.append(f"引用了不存在的地层 ID: {missing_layers}")

            # Active records targeted by withdrawals.
            withdraw_rows: dict[str, sqlite3.Row] = {}
            if withdraw_numbers:
                placeholders = ",".join("?" for _ in withdraw_numbers)
                for row in conn.execute(
                    f"SELECT id, record_number, withdrawn_at FROM records "
                    f"WHERE area_id = ? AND record_number IN ({placeholders})",
                    [area["id"], *withdraw_numbers],
                ):
                    withdraw_rows[row["record_number"]] = row
            missing_or_inactive = sorted(
                number
                for number in withdraw_numbers
                if number not in withdraw_rows
                or withdraw_rows[number]["withdrawn_at"] is not None
            )
            if missing_or_inactive:
                errors.append(
                    "以下记录号在该分区不存在或已撤回，无法撤回: "
                    f"{missing_or_inactive}"
                )

            if errors:
                raise BatchValidationError("批次校验失败，整单未生效", errors=errors)

            # Hypothetical active graph after this batch.
            edges, numbers = _active_edges(conn)
            record_pair = {
                rid: pair for pair, rids in edges.items() for rid in rids
            }
            for number in withdraw_numbers:
                rid = int(withdraw_rows[number]["id"])
                pair = record_pair.pop(rid, None)
                if pair is not None:
                    edges[pair] = [item for item in edges[pair] if item != rid]
                    if not edges[pair]:
                        edges.pop(pair, None)

            for index, item in enumerate(additions, start=1):
                # Negative placeholder ids distinguish not-yet-inserted records.
                synthetic_id = -index
                pair = (item["earlier_layer_id"], item["later_layer_id"])
                edges.setdefault(pair, []).append(synthetic_id)
                numbers[synthetic_id] = item["record_number"]

            adjacency: dict[int, set[int]] = defaultdict(set)
            for (earlier, later) in edges:
                adjacency[earlier].add(later)
                adjacency.setdefault(later, set())

            cycle_nodes = find_cycle(sorted(adjacency), adjacency)
            if cycle_nodes is not None:
                data = _cycle_payload(conn, cycle_nodes, edges, numbers)
                raise CycleDetectedError(
                    "批次生效后会形成地层先后环，整单退回", cycle=data
                )

            cursor = conn.execute(
                """
                INSERT INTO batches
                    (area_id, base_version, area_version, site_version,
                     additions_count, withdrawals_count, relation_count, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    area["id"],
                    base_version,
                    current_version + 1,
                    0,
                    len(additions),
                    len(withdrawals),
                    len(edges),
                    note,
                    now,
                ),
            )
            batch_id = int(cursor.lastrowid)

            added_ids: list[int] = []
            for item in additions:
                cur = conn.execute(
                    """
                    INSERT INTO records
                        (area_id, record_number, earlier_layer_id, later_layer_id,
                         created_by_batch_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        area["id"],
                        item["record_number"],
                        item["earlier_layer_id"],
                        item["later_layer_id"],
                        batch_id,
                        now,
                    ),
                )
                added_ids.append(int(cur.lastrowid))

            withdrawn_ids: list[int] = []
            for withdrawal in withdrawals:
                row = withdraw_rows[withdrawal["record_number"]]
                cur = conn.execute(
                    """
                    UPDATE records
                    SET withdrawn_at = ?, withdrawn_by_batch_id = ?,
                        withdrawal_reason = ?
                    WHERE id = ? AND withdrawn_at IS NULL
                    """,
                    (now, batch_id, withdrawal.get("reason"), row["id"]),
                )
                if cur.rowcount != 1:  # pragma: no cover - write lock prevents this
                    raise ConflictError(
                        f"记录 {withdrawal['record_number']!r} 状态已变化，整单退回"
                    )
                withdrawn_ids.append(int(row["id"]))

            conn.execute(
                "UPDATE areas SET version = version + 1 WHERE id = ?", (area["id"],)
            )
            new_site_version = _bump_site_version(conn)
            conn.execute(
                "UPDATE batches SET site_version = ? WHERE id = ?",
                (new_site_version, batch_id),
            )

            return {
                "batch_id": batch_id,
                "area_code": area_code,
                "base_version": base_version,
                "area_version": current_version + 1,
                "site_version": new_site_version,
                "note": note,
                "created_at": now,
                "additions": _fetch_records_by_ids(conn, added_ids),
                "withdrawals": _fetch_records_by_ids(conn, withdrawn_ids),
                "effective_relation_count": len(edges),
            }
    except sqlite3.IntegrityError as exc:
        raise ConflictError(
            "数据唯一性或外键约束冲突，整批未生效", detail=str(exc)
        ) from exc
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc):
            raise StorageUnavailableError(
                "数据库正忙，等待写锁超时，整单未生效，请稍后重试"
            ) from exc
        raise


def get_batch(db: DB, batch_id: int, busy_timeout_ms: int) -> dict[str, Any]:
    with read_transaction(db, busy_timeout_ms) as conn:
        row = conn.execute(
            """
            SELECT b.*, a.code AS area_code
            FROM batches b JOIN areas a ON a.id = b.area_id
            WHERE b.id = ?
            """,
            (batch_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"批次 {batch_id} 不存在")
        additions = conn.execute(
            f"{_RECORD_SELECT} WHERE r.created_by_batch_id = ? ORDER BY r.id",
            (batch_id,),
        ).fetchall()
        withdrawals = conn.execute(
            f"{_RECORD_SELECT} WHERE r.withdrawn_by_batch_id = ? ORDER BY r.id",
            (batch_id,),
        ).fetchall()
        pair_count = (
            int(row["relation_count"])
            if row["relation_count"] is not None
            else conn.execute(
                """
                SELECT COUNT(*) AS n FROM (
                    SELECT DISTINCT earlier_layer_id, later_layer_id
                    FROM records WHERE withdrawn_at IS NULL
                )
                """
            ).fetchone()["n"]
        )
        return {
            "batch_id": int(row["id"]),
            "area_code": row["area_code"],
            "base_version": int(row["base_version"]),
            "area_version": int(row["area_version"]),
            "site_version": int(row["site_version"]),
            "note": row["note"],
            "created_at": row["created_at"],
            "additions": [_record_out(item) for item in additions],
            "withdrawals": [_record_out(item) for item in withdrawals],
            "effective_relation_count": pair_count,
        }


def list_batches(db: DB, area_code: str, busy_timeout_ms: int) -> list[dict[str, Any]]:
    with read_transaction(db, busy_timeout_ms) as conn:
        area = _area_row(conn, area_code)
        rows = conn.execute(
            """
            SELECT id, base_version, area_version, site_version,
                   additions_count, withdrawals_count, note, created_at
            FROM batches WHERE area_id = ? ORDER BY id
            """,
            (area["id"],),
        ).fetchall()
        return [
            {
                "batch_id": int(row["id"]),
                "base_version": int(row["base_version"]),
                "area_version": int(row["area_version"]),
                "site_version": int(row["site_version"]),
                "additions_count": int(row["additions_count"]),
                "withdrawals_count": int(row["withdrawals_count"]),
                "note": row["note"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]


# ---------------------------------------------------------------------------
# Rank queries
# ---------------------------------------------------------------------------


def _load_site_graph(
    conn: sqlite3.Connection,
) -> tuple[list[int], list[tuple[int, int]], dict[int, dict[str, Any]]]:
    layer_rows = conn.execute(
        """
        SELECT l.id, a.code AS area, l.code, l.name
        FROM layers l JOIN areas a ON a.id = l.area_id
        ORDER BY l.id
        """
    ).fetchall()
    nodes = [int(row["id"]) for row in layer_rows]
    refs = {int(row["id"]): _layer_ref_from_row(row) for row in layer_rows}
    edges = [
        (int(row["earlier_layer_id"]), int(row["later_layer_id"]))
        for row in conn.execute(
            """
            SELECT DISTINCT earlier_layer_id, later_layer_id
            FROM records WHERE withdrawn_at IS NULL
            """
        )
    ]
    return nodes, edges, refs


def get_rank(db: DB, layer_id: int, busy_timeout_ms: int) -> dict[str, Any]:
    with read_transaction(db, busy_timeout_ms) as conn:
        nodes, edges, refs = _load_site_graph(conn)
        if layer_id not in refs:
            raise NotFoundError(f"地层 {layer_id} 不存在")
        try:
            result = compute_rank(nodes, edges, layer_id)
        except CyclicGraphError as exc:
            active_edges, numbers = _active_edges(conn)
            data = _cycle_payload(conn, exc.cycle_nodes, active_edges, numbers)
            raise CycleDetectedError(
                "已发布的关系中存在环，无法核定名次", cycle=data
            ) from exc

        def ranked(order: list[int]) -> list[dict[str, Any]]:
            return [
                {"rank": index + 1, **refs[node]}
                for index, node in enumerate(order)
            ]

        return {
            "layer": refs[layer_id],
            "site_version": _site_version(conn),
            "area_versions": _area_versions(conn),
            "total_layers": len(nodes),
            "earliest_rank": result.min_rank,
            "latest_rank": result.max_rank,
            "ancestor_count": len(result.ancestors),
            "descendant_count": len(result.descendants),
            "earliest_order": ranked(result.min_order),
            "latest_order": ranked(result.max_order),
            "method": "reachability",
        }


def site_relations(db: DB, busy_timeout_ms: int) -> dict[str, Any]:
    """Return the active relation groups visible to readers.

    Useful for auditing the merged graph and for showing that duplicate records
    keep an edge alive until all of them are withdrawn.
    """

    with read_transaction(db, busy_timeout_ms) as conn:
        _, _, refs = _load_site_graph(conn)
        rows = conn.execute(
            """
            SELECT earlier_layer_id, later_layer_id, record_number, area_id
            FROM records WHERE withdrawn_at IS NULL
            ORDER BY earlier_layer_id, later_layer_id, record_number
            """
        ).fetchall()
        groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[
                (int(row["earlier_layer_id"]), int(row["later_layer_id"]))
            ].append(
                {
                    "record_number": row["record_number"],
                    "owner_area_id": int(row["area_id"]),
                }
            )
        relations = [
            {
                "earlier": refs[earlier],
                "later": refs[later],
                "record_numbers": [item["record_number"] for item in items],
                "records": items,
            }
            for (earlier, later), items in sorted(groups.items())
        ]
        return {
            "site_version": _site_version(conn),
            "area_versions": _area_versions(conn),
            "layer_count": len(refs),
            "relation_count": len(relations),
            "relations": relations,
        }


def health(db: DB, busy_timeout_ms: int) -> dict[str, Any]:
    with read_transaction(db, busy_timeout_ms) as conn:
        return {"status": "ok", "site_version": _site_version(conn)}
