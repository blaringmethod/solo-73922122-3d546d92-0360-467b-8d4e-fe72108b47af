"""Optimistic publishing: out-of-lock checks, overlay on retry, cache reuse."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app import service
from app.db import init_db
from app.errors import (
    CycleDetectedError,
    StorageUnavailableError,
    VersionConflictError,
)


def _area(db: Path, code: str) -> None:
    service.create_area(db, code=code, name=code, busy_timeout_ms=5_000)


def _layer(db: Path, area: str, code: str) -> int:
    return int(
        service.create_layer(
            db,
            area,
            code=code,
            name=code,
            description=None,
            busy_timeout_ms=5_000,
        )["id"]
    )


def _add(number: str, earlier: int, later: int) -> dict:
    return {
        "record_number": number,
        "earlier_layer_id": earlier,
        "later_layer_id": later,
    }


def _submit(
    db: Path,
    area: str,
    *,
    base_version: int,
    additions: list[dict] | None = None,
    withdrawals: list[dict] | None = None,
) -> dict:
    return service.submit_batch(
        db,
        area,
        base_version=base_version,
        additions=additions or [],
        withdrawals=withdrawals or [],
        note=None,
        busy_timeout_ms=5_000,
    )


def _version(db: Path, area: str) -> int:
    return int(service.get_area(db, area, 5_000)["version"])


def _records(db: Path, area: str) -> set[str]:
    return {
        item["record_number"]
        for item in service.list_records(
            db, area, include_withdrawn=True, busy_timeout_ms=5_000
        )
    }


@pytest.fixture()
def two_trenches(db_path: Path) -> dict:
    """Two disjoint chains, one per area, with no cross-area relation yet."""

    init_db(db_path)
    for code in ("A", "B"):
        _area(db_path, code)
    layers = {
        code: _layer(db_path, code[0], code) for code in ("A1", "A2", "B1", "B2")
    }
    assert _version(db_path, "A") == 2
    assert _version(db_path, "B") == 2
    _submit(
        db_path,
        "A",
        base_version=2,
        additions=[_add("R-A-001", layers["A1"], layers["A2"])],
    )
    _submit(
        db_path,
        "B",
        base_version=2,
        additions=[_add("R-B-001", layers["B1"], layers["B2"])],
    )
    return {"db": db_path, "layers": layers}


def _block_next_check(monkeypatch: pytest.MonkeyPatch) -> tuple[dict, threading.Event, threading.Event]:
    """Make the creating thread block inside the out-of-lock cycle check once."""

    state = {"thread": None, "armed": True}
    entered = threading.Event()
    release = threading.Event()
    original = service._check_cycle

    def slow_check(edges):
        if (
            state["armed"]
            and threading.current_thread() is state["thread"]
        ):
            state["armed"] = False
            entered.set()
            assert release.wait(10), "test did not release the cycle check"
        return original(edges)

    monkeypatch.setattr(service, "_check_cycle", slow_check)
    return state, entered, release


def test_site_wide_check_runs_without_blocking_other_areas(
    two_trenches: dict, monkeypatch: pytest.MonkeyPatch
):
    db, layers = two_trenches["db"], two_trenches["layers"]
    state, entered, release = _block_next_check(monkeypatch)
    outcome: dict[str, object] = {}

    def held_submission() -> None:
        state["thread"] = threading.current_thread()
        try:
            outcome["result"] = _submit(
                db,
                "A",
                base_version=_version(db, "A"),
                additions=[_add("R-A-101", layers["A2"], layers["B1"])],
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            outcome["error"] = exc

    thread = threading.Thread(target=held_submission)
    thread.start()
    assert entered.wait(10), "submission never reached the cycle check"

    # While the first submission is inside its out-of-lock check, another
    # trench must still be able to publish (it would time out if the check
    # held SQLite's write lock).
    other = _submit(
        db,
        "B",
        base_version=_version(db, "B"),
        additions=[_add("R-B-101", layers["B1"], layers["B2"])],
    )
    assert other["area_version"] == 4

    release.set()
    thread.join(15)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")
    # The held batch retried against the freshly published state and, its own
    # edge still being acyclic, committed afterwards.
    assert outcome["result"]["area_version"] == 4
    assert {"R-A-101", "R-B-101"} <= _records(db, "A") | _records(db, "B")


def test_second_batch_is_judged_on_combined_graph_after_retry(
    two_trenches: dict, monkeypatch: pytest.MonkeyPatch
):
    db, layers = two_trenches["db"], two_trenches["layers"]
    state, entered, release = _block_next_check(monkeypatch)
    outcome: dict[str, object] = {}

    def held_submission() -> None:
        state["thread"] = threading.current_thread()
        try:
            _submit(
                db,
                "B",
                base_version=_version(db, "B"),
                additions=[_add("R-B-201", layers["B2"], layers["A1"])],
            )
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=held_submission)
    thread.start()
    assert entered.wait(10)

    # Each edge is acyclic on its own; the first one publishes while the
    # second is still validating outside the lock.
    first = _submit(
        db,
        "A",
        base_version=_version(db, "A"),
        additions=[_add("R-A-201", layers["A2"], layers["B1"])],
    )
    assert first["area_version"] == 4

    release.set()
    thread.join(15)
    assert not thread.is_alive()

    error = outcome.get("error")
    assert isinstance(error, CycleDetectedError), error
    cycle = error.extra["cycle"]
    # The report is in terms of the published records overlaid with this
    # batch, so the other trench's just-published record number appears.
    assert {"R-A-201", "R-B-201"} <= set(cycle["record_numbers"])
    for edge in cycle["edges"]:
        assert edge["record_numbers"]

    # The rejected batch changed nothing in B.
    assert _version(db, "B") == 3
    assert "R-B-201" not in _records(db, "B")
    assert _version(db, "A") == 4


def test_cycle_report_uses_replaced_supporting_record(
    db_path: Path,
):
    init_db(db_path)
    _area(db_path, "A")
    _area(db_path, "B")
    a1 = _layer(db_path, "A", "A1")
    a2 = _layer(db_path, "A", "A2")
    _layer(db_path, "B", "B1")

    _submit(db_path, "A", base_version=2, additions=[_add("R-A-011", a1, a2)])
    # Another curator swaps the supporting evidence for the same edge: the old
    # record is withdrawn and a new number is published.
    swap = _submit(
        db_path,
        "A",
        base_version=3,
        additions=[_add("R-A-012", a1, a2)],
        withdrawals=[{"record_number": "R-A-011", "reason": "以新记录替换"}],
    )
    assert swap["effective_relation_count"] == 1

    with pytest.raises(CycleDetectedError) as caught:
        _submit(db_path, "B", base_version=1, additions=[_add("R-B-011", a2, a1)])

    cycle = caught.value.extra["cycle"]
    assert "R-A-012" in cycle["record_numbers"]
    assert "R-A-011" not in cycle["record_numbers"]
    edge = next(
        item
        for item in cycle["edges"]
        if item["earlier"]["code"] == "A1" and item["later"]["code"] == "A2"
    )
    assert edge["record_numbers"] == ["R-A-012"]


def test_unchanged_effective_edges_reuse_previous_cycle_verdict(
    seeded: dict, db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service._CYCLE_CACHE.clear()
    calls: list[int] = []
    original = service.find_cycle

    def counting_find_cycle(nodes, adjacency):
        calls.append(1)
        return original(nodes, adjacency)

    monkeypatch.setattr(service, "find_cycle", counting_find_cycle)
    layers = {
        item["code"]: item["id"]
        for item in service.list_layers(db_path, "T2", 5_000)
    }
    try:
        # Both batches duplicate the already active M1 -> M2 edge, so the
        # effective edge set is unchanged between the two checks.
        _submit(
            db_path,
            "T2",
            base_version=_version(db_path, "T2"),
            additions=[_add("R-T2-901", layers["M1"], layers["M2"])],
        )
        _submit(
            db_path,
            "T2",
            base_version=_version(db_path, "T2"),
            additions=[_add("R-T2-902", layers["M1"], layers["M2"])],
        )
        assert len(calls) == 1
    finally:
        service._CYCLE_CACHE.clear()


def test_write_failure_rolls_back_and_keeps_published_state(
    seeded: dict, db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    before = service.site_relations(db_path, 5_000)
    version = _version(db_path, "T2")
    layers = {
        item["code"]: item["id"]
        for item in service.list_layers(db_path, "T2", 5_000)
    }

    def failing_fetch(conn, ids):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(service, "_fetch_records_by_ids", failing_fetch)
    with pytest.raises(sqlite3.OperationalError):
        _submit(
            db_path,
            "T2",
            base_version=version,
            additions=[_add("R-T2-950", layers["M1"], layers["M2"])],
        )

    assert _version(db_path, "T2") == version
    assert service.site_relations(db_path, 5_000) == before
    assert "R-T2-950" not in _records(db_path, "T2")


def test_publish_retry_exhaustion_leaves_state_untouched(
    seeded: dict, db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    before = service.site_relations(db_path, 5_000)
    version = _version(db_path, "T2")
    layers = {
        item["code"]: item["id"]
        for item in service.list_layers(db_path, "T2", 5_000)
    }

    def always_stale(conn, prepared, *, note, now):
        raise service._SnapshotChanged()

    monkeypatch.setattr(service, "_apply_prepared", always_stale)
    with pytest.raises(StorageUnavailableError):
        _submit(
            db_path,
            "T2",
            base_version=version,
            additions=[_add("R-T2-951", layers["M1"], layers["M2"])],
        )

    assert _version(db_path, "T2") == version
    assert service.site_relations(db_path, 5_000) == before


def test_same_area_version_conflict_still_wins_over_overlay(
    seeded: dict, db_path: Path
):
    layers = {
        item["code"]: item["id"]
        for item in service.list_layers(db_path, "T2", 5_000)
    }
    base = _version(db_path, "T2")
    _submit(
        db_path,
        "T2",
        base_version=base,
        additions=[_add("R-T2-960", layers["M1"], layers["M2"])],
    )
    with pytest.raises(VersionConflictError):
        _submit(
            db_path,
            "T2",
            base_version=base,
            additions=[_add("R-T2-961", layers["M1"], layers["M2"])],
        )
    assert "R-T2-961" not in _records(db_path, "T2")
