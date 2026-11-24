"""Demo dataset used by ``POST /demo/seed`` and the /docs walkthrough.

Scenario
--------
Zones ``T1`` and ``T2``:

    T1: L1 -> L2 -> L3 -> L4        (R-T1-001..003)
    T1: M2 -> L4                    (R-T1-004, cross-trench record)
    T2: M1 -> M2, twice             (R-T2-001, R-T2-002: duplicate records)

Rank of ``M2``:

* earliest 2 (``M1`` must precede it),
* latest   5 (``L4`` must follow it).

Withdrawing R-T2-002 alone changes nothing: the duplicate R-T2-001 still
states ``M1 -> M2``.  Only after R-T2-001 is withdrawn too does the earliest
rank become 1.  Withdrawing R-T1-004 then removes the last successor of M2 and
the latest rank becomes 6.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from .db import read_transaction, write_transaction
from .service import _bump_site_version, utc_now

DEMO_AREAS = {
    "T1": {
        "name": "北区 T1 探方",
        "layers": [
            ("L1", "表土层", "近代扰动"),
            ("L2", "汉代文化层", None),
            ("L3", "战国文化层", None),
            ("L4", "生土", None),
        ],
    },
    "T2": {
        "name": "南区 T2 探方",
        "layers": [("M1", "宋代文化层", None), ("M2", "唐代文化层", None)],
    },
}


def seed_demo(db: Path | str, busy_timeout_ms: int) -> dict[str, Any]:
    """Create the demo inventory once; calling again is a no-op.

    If either demo area already exists the function returns the current state
    without writing anything, so an operator's own data is never mixed with a
    half-seeded demonstration.
    """
    # Seed in one write transaction so a crash or lock timeout cannot leave
    # half the demonstration behind.
    with write_transaction(db, busy_timeout_ms) as conn:
        existing = {
            row["code"] for row in conn.execute("SELECT code FROM areas")
        }
        if set(DEMO_AREAS) & existing:
            state = _demo_state(conn)
            state["seeded"] = False
            return state

        area_ids: dict[str, int] = {}
        layer_ids: dict[str, dict[str, int]] = {}
        now = utc_now()
        for code, spec in DEMO_AREAS.items():
            cur = conn.execute(
                "INSERT INTO areas (code, name, version, created_at) "
                "VALUES (?, ?, 0, ?)",
                (code, spec["name"], now),
            )
            area_id = int(cur.lastrowid)
            area_ids[code] = area_id
            layer_ids[code] = {}
            for layer_code, layer_name, description in spec["layers"]:
                layer_cur = conn.execute(
                    """
                    INSERT INTO layers
                        (area_id, code, name, description, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (area_id, layer_code, layer_name, description, now),
                )
                layer_ids[code][layer_code] = int(layer_cur.lastrowid)
            conn.execute(
                "UPDATE areas SET version = version + 1 WHERE id = ?", (area_id,)
            )

        t1, t2 = layer_ids["T1"], layer_ids["T2"]
        site_version = _bump_site_version(conn)
        _insert_batch(
            conn,
            area_ids["T1"],
            base_version=0,
            site_version=site_version,
            note="演示：T1 初始地层清单",
            additions=[
                ("R-T1-001", t1["L1"], t1["L2"]),
                ("R-T1-002", t1["L2"], t1["L3"]),
                ("R-T1-003", t1["L3"], t1["L4"]),
                # Cross-trench relation owned by T1; endpoints are T2's M2 and
                # T1's L4.  This is what keeps M2's latest rank at 5.
                ("R-T1-004", t2["M2"], t1["L4"]),
            ],
            now=now,
        )
        _insert_batch(
            conn,
            area_ids["T2"],
            base_version=0,
            site_version=site_version,
            note="演示：M1 早于 M2，两条重复记录",
            additions=[
                ("R-T2-001", t2["M1"], t2["M2"]),
                ("R-T2-002", t2["M1"], t2["M2"]),
            ],
            now=now,
        )
        state = _demo_state(conn)
        state["seeded"] = True
        return state


def _insert_batch(
    conn: sqlite3.Connection,
    area_id: int,
    *,
    base_version: int,
    site_version: int,
    note: str,
    additions: list[tuple[str, int, int]],
    now: str,
) -> None:
    """Insert a batch directly, bypassing version checks, for seeding only."""

    cur = conn.execute(
        """
        INSERT INTO batches
            (area_id, base_version, area_version, site_version,
             additions_count, withdrawals_count, relation_count, note, created_at)
        VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)
        """,
        (
            area_id,
            base_version,
            base_version + 1,
            site_version,
            len(additions),
            note,
            now,
        ),
    )
    batch_id = int(cur.lastrowid)
    for record_number, earlier, later in additions:
        conn.execute(
            """
            INSERT INTO records
                (area_id, record_number, earlier_layer_id, later_layer_id,
                 created_by_batch_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (area_id, record_number, earlier, later, batch_id, now),
        )
    relation_count = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT earlier_layer_id, "
        "later_layer_id FROM records WHERE withdrawn_at IS NULL)"
    ).fetchone()["n"]
    conn.execute(
        "UPDATE batches SET relation_count = ? WHERE id = ?",
        (int(relation_count), batch_id),
    )
    conn.execute(
        "UPDATE areas SET version = version + 1 WHERE id = ?", (area_id,)
    )


def demo_state(db: Path | str, busy_timeout_ms: int) -> dict[str, Any]:
    with read_transaction(db, busy_timeout_ms) as conn:
        return _demo_state(conn)


def _demo_state(conn: sqlite3.Connection) -> dict[str, Any]:
    area_rows = conn.execute(
        "SELECT id, code, name, version, created_at FROM areas ORDER BY code"
    ).fetchall()
    layer_rows = conn.execute(
        "SELECT id, area_id, code FROM layers ORDER BY area_id, code"
    ).fetchall()
    counts = {
        int(row["area_id"]): (int(row["layers"]), int(row["records"]))
        for row in conn.execute(
            """
            SELECT a.id AS area_id,
                   (SELECT COUNT(*) FROM layers l WHERE l.area_id = a.id) AS layers,
                   (SELECT COUNT(*) FROM records r
                    WHERE r.area_id = a.id AND r.withdrawn_at IS NULL) AS records
            FROM areas a
            """
        )
    }
    layer_ids: dict[str, dict[str, int]] = defaultdict(dict)
    area_code = {int(row["id"]): row["code"] for row in area_rows}
    for row in layer_rows:
        layer_ids[area_code[int(row["area_id"])]][row["code"]] = int(row["id"])

    state: dict[str, Any] = {
        "areas": [
            {
                "id": int(row["id"]),
                "code": row["code"],
                "name": row["name"],
                "version": int(row["version"]),
                "layer_count": counts[int(row["id"])][0],
                "active_record_count": counts[int(row["id"])][1],
                "created_at": row["created_at"],
            }
            for row in area_rows
        ],
        "layers": {
            area: [
                {"id": layer_id, "code": code}
                for code, layer_id in sorted(items.items())
            ]
            for area, items in sorted(layer_ids.items())
        },
        "versions": {row["code"]: int(row["version"]) for row in area_rows},
        "demo_rank_layer": None,
        "conflict_demo": None,
        "suggested_steps": [
            "GET /ranks/{M2.id} -> earliest_rank=2, latest_rank=5",
            "POST /areas/T2/batches with base_version from above, "
            "withdraw R-T2-002 -> rank unchanged",
            "same T2 batch withdraw R-T2-001 -> earliest_rank becomes 1",
            "POST /areas/T1/batches withdraw R-T1-004 -> latest_rank becomes 6",
            "conflict demo: POST T1 {M1 -> L1} as R-T1-905, then POST T2 "
            "{L1 -> M1} as R-T2-905 -> second batch rejected with "
            "409 cycle_detected naming both record numbers",
        ],
    }
    m2 = layer_ids.get("T2", {}).get("M2")
    if m2 is not None:
        state["demo_rank_layer"] = {"id": m2, "area": "T2", "code": "M2"}
    l1 = layer_ids.get("T1", {}).get("L1")
    m1 = layer_ids.get("T2", {}).get("M1")
    if l1 is not None and m1 is not None:
        t1_version = state["versions"].get("T1")
        t2_version = state["versions"].get("T2")
        state["conflict_demo"] = {
            "instructions": (
                "先提交 T1，再提交方向相反的 T2；两单合起来成环，后一单退回。"
                "把下面两个 base_version 都取提交前的值，即可复现。"
            ),
            "first_batch": {
                "area": "T1",
                "body": {
                    "base_version": t1_version,
                    "additions": [
                        {
                            "record_number": "R-T1-905",
                            "earlier_layer_id": m1,
                            "later_layer_id": l1,
                        }
                    ],
                },
            },
            "second_batch": {
                "area": "T2",
                "body": {
                    "base_version": t2_version,
                    "additions": [
                        {
                            "record_number": "R-T2-905",
                            "earlier_layer_id": l1,
                            "later_layer_id": m1,
                        }
                    ],
                },
            },
            "expected": (
                "first_batch -> 201; second_batch -> 409 cycle_detected with "
                "cycle.record_numbers = [\"R-T1-905\", \"R-T2-905\"]"
            ),
        }
    return state
