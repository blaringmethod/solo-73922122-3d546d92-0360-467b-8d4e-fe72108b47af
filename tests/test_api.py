"""API behaviour: versions, atomic batches, duplicate records and cycles."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _version(client: TestClient, area: str) -> int:
    response = client.get(f"/areas/{area}")
    assert response.status_code == 200, response.text
    return response.json()["version"]


def _layers(client: TestClient, area: str) -> dict[str, int]:
    response = client.get(f"/areas/{area}/layers")
    assert response.status_code == 200, response.text
    return {item["code"]: item["id"] for item in response.json()}


def _rank(client: TestClient, layer_id: int) -> dict:
    response = client.get(f"/ranks/{layer_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _submit(client: TestClient, area: str, base_version: int, **payload):
    body = {"base_version": base_version, **payload}
    return client.post(f"/areas/{area}/batches", json=body)


def _add(number: str, earlier: int, later: int) -> dict:
    return {
        "record_number": number,
        "earlier_layer_id": earlier,
        "later_layer_id": later,
    }


def _withdraw(number: str, reason: str | None = None) -> dict:
    return {"record_number": number, "reason": reason}


# ---------------------------------------------------------------------------
# Demo walkthrough
# ---------------------------------------------------------------------------


def test_seed_is_idempotent(client: TestClient):
    first = client.post("/demo/seed")
    second = client.post("/demo/seed")
    assert first.status_code == second.status_code == 200
    assert first.json()["seeded"] is True
    assert second.json()["seeded"] is False
    assert first.json()["layers"] == second.json()["layers"]
    assert len(client.get("/areas").json()) == 2
    assert len(client.get("/areas/T1/records").json()) == 4


def test_demo_rank_bounds(client: TestClient, seeded: dict):
    layer_id = seeded["demo_rank_layer"]["id"]
    body = _rank(client, layer_id)
    assert body["layer"]["code"] == "M2"
    assert body["total_layers"] == 6
    assert (body["earliest_rank"], body["latest_rank"]) == (2, 5)
    assert [item["rank"] for item in body["earliest_order"]] == [1, 2, 3, 4, 5, 6]
    assert body["earliest_order"][1]["id"] == layer_id
    assert body["latest_order"][4]["id"] == layer_id
    assert body["method"] == "reachability"


def test_withdrawing_one_of_two_duplicate_records_keeps_relation(
    client: TestClient, seeded: dict
):
    layer_id = seeded["demo_rank_layer"]["id"]
    t2_version = _version(client, "T2")
    response = _submit(
        client, "T2", t2_version, withdrawals=[_withdraw("R-T2-002")]
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["withdrawals"][0]["record_number"] == "R-T2-002"
    assert body["withdrawals"][0]["active"] is False
    # The remaining duplicate still constrains M2.
    assert _rank(client, layer_id)["earliest_rank"] == 2

    relations = client.get("/relations").json()["relations"]
    pair = next(
        item
        for item in relations
        if item["earlier"]["code"] == "M1" and item["later"]["code"] == "M2"
    )
    assert pair["record_numbers"] == ["R-T2-001"]


def test_withdrawing_all_duplicates_releases_relation(client: TestClient, seeded: dict):
    layer_id = seeded["demo_rank_layer"]["id"]
    assert _submit(
        client, "T2", _version(client, "T2"), withdrawals=[_withdraw("R-T2-002")]
    ).status_code == 201
    assert _rank(client, layer_id)["earliest_rank"] == 2

    response = _submit(
        client, "T2", _version(client, "T2"), withdrawals=[_withdraw("R-T2-001")]
    )
    assert response.status_code == 201, response.text
    before = _rank(client, layer_id)
    assert before["earliest_rank"] == 1
    assert before["latest_rank"] == 5
    assert before["earliest_order"][0]["id"] == layer_id

    # Withdraw the cross-trench successor of M2 in T1.
    response = _submit(
        client, "T1", _version(client, "T1"), withdrawals=[_withdraw("R-T1-004")]
    )
    assert response.status_code == 201, response.text
    after = _rank(client, layer_id)
    assert after["latest_rank"] == 6
    assert after["latest_order"][-1]["id"] == layer_id
    assert after["site_version"] > before["site_version"]


def test_withdrawn_record_numbers_cannot_be_reused(client: TestClient, seeded: dict):
    assert _submit(
        client, "T2", _version(client, "T2"), withdrawals=[_withdraw("R-T2-002")]
    ).status_code == 201
    t2 = _layers(client, "T2")
    response = _submit(
        client,
        "T2",
        _version(client, "T2"),
        additions=[_add("R-T2-002", t2["M2"], t2["M1"])],
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "batch_rejected"
    assert "不可复用" in " ".join(response.json()["detail"]["errors"])


# ---------------------------------------------------------------------------
# Version conflicts and atomicity
# ---------------------------------------------------------------------------


def test_stale_base_version_rejects_whole_batch(client: TestClient, seeded: dict):
    t1 = _layers(client, "T1")
    stale = _version(client, "T1") - 1
    before = client.get("/relations").json()
    response = _submit(
        client, "T1", stale, additions=[_add("R-T1-900", t1["L1"], t1["L4"])]
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "area_version_conflict"
    assert detail["base_version"] == stale
    assert detail["current_version"] == stale + 1
    after = client.get("/relations").json()
    assert before["relations"] == after["relations"]
    assert before["site_version"] == after["site_version"]


def test_second_batch_in_same_area_with_stale_snapshot_is_rejected(
    client: TestClient, seeded: dict
):
    t1 = _layers(client, "T1")
    version_seen = _version(client, "T1")
    first = _submit(
        client, "T1", version_seen, additions=[_add("R-T1-101", t1["L1"], t1["L4"])]
    )
    assert first.status_code == 201
    second = _submit(
        client, "T1", version_seen, additions=[_add("R-T1-102", t1["L2"], t1["L4"])]
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "area_version_conflict"
    numbers = {
        record["record_number"]
        for record in client.get("/areas/T1/records").json()
    }
    assert "R-T1-101" in numbers
    assert "R-T1-102" not in numbers


def test_other_area_can_submit_while_one_area_has_pending_changes(
    client: TestClient, seeded: dict
):
    t1 = _layers(client, "T1")
    t2 = _layers(client, "T2")
    # T1's curator snapshots the version, but T2 publishes first.
    t1_version_seen = _version(client, "T1")
    assert (
        _submit(
            client, "T2", _version(client, "T2"), additions=[_add("R-T2-101", t2["M1"], t2["M2"])]
        ).status_code
        == 201
    )
    # A cross-area record owned by T1 is still accepted at T1's own version.
    assert (
        _submit(
            client,
            "T1",
            t1_version_seen,
            additions=[_add("R-T1-201", t1["L1"], t2["M2"])],
        ).status_code
        == 201
    )


def test_validation_failure_on_one_item_applies_nothing(client: TestClient, seeded: dict):
    t1 = _layers(client, "T1")
    version = _version(client, "T1")
    before = client.get("/relations").json()
    response = _submit(
        client,
        "T1",
        version,
        additions=[
            _add("R-T1-301", t1["L1"], t1["L4"]),
            _add("R-T1-302", t1["L2"], 999_999),
        ],
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "batch_rejected"
    assert any("999999" in error for error in detail["errors"])
    after = client.get("/relations").json()
    assert before["relations"] == after["relations"]
    assert _version(client, "T1") == version
    numbers = {item["record_number"] for item in client.get("/areas/T1/records").json()}
    assert "R-T1-301" not in numbers


def test_withdrawing_unknown_or_inactive_record_rejects_batch(
    client: TestClient, seeded: dict
):
    version = _version(client, "T2")
    response = _submit(client, "T2", version, withdrawals=[_withdraw("R-T2-404")])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "batch_rejected"

    assert _submit(
        client, "T2", _version(client, "T2"), withdrawals=[_withdraw("R-T2-001")]
    ).status_code == 201
    response = _submit(
        client, "T2", _version(client, "T2"), withdrawals=[_withdraw("R-T2-001")]
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "batch_rejected"


def test_records_of_other_areas_cannot_be_withdrawn_by_this_area(
    client: TestClient, seeded: dict
):
    response = _submit(
        client, "T2", _version(client, "T2"), withdrawals=[_withdraw("R-T1-001")]
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "batch_rejected"


def test_record_numbers_are_unique_per_area_not_globally(client: TestClient, seeded: dict):
    t2 = _layers(client, "T2")
    # T2 may reuse T1's numbering scheme; the composite unique index permits it.
    response = _submit(
        client,
        "T2",
        _version(client, "T2"),
        additions=[_add("R-T1-001", t2["M1"], t2["M2"])],
    )
    assert response.status_code == 201, response.text
    numbers = [
        item["record_number"]
        for item in client.get("/areas/T2/records", params={"include_withdrawn": True}).json()
    ]
    assert numbers.count("R-T1-001") == 1


def test_duplicate_numbers_inside_batch_rejected(client: TestClient, seeded: dict):
    t2 = _layers(client, "T2")
    response = _submit(
        client,
        "T2",
        _version(client, "T2"),
        additions=[
            _add("R-T2-501", t2["M1"], t2["M2"]),
            _add("R-T2-501", t2["M1"], t2["M2"]),
        ],
    )
    assert response.status_code == 409
    assert "批次内补录记录号重复" in " ".join(response.json()["detail"]["errors"])


def test_self_relation_is_rejected_by_schema(client: TestClient, seeded: dict):
    t2 = _layers(client, "T2")
    response = _submit(
        client, "T2", _version(client, "T2"), additions=[_add("R-T2-555", t2["M1"], t2["M1"])]
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "request_validation_error"


def test_same_number_added_and_withdrawn_in_one_batch_rejected(
    client: TestClient, seeded: dict
):
    t2 = _layers(client, "T2")
    response = _submit(
        client,
        "T2",
        _version(client, "T2"),
        additions=[_add("R-T2-001", t2["M1"], t2["M2"])],
        withdrawals=[_withdraw("R-T2-001")],
    )
    assert response.status_code == 409
    assert "又补录又撤回" in " ".join(response.json()["detail"]["errors"])


def test_empty_batch_and_bad_payload_are_rejected(client: TestClient, seeded: dict):
    response = client.post(
        "/areas/T2/batches", json={"base_version": 0, "additions": [], "withdrawals": []}
    )
    assert response.status_code == 422
    payload = response.json()["detail"]
    assert payload["code"] == "request_validation_error"

    response = client.post(
        "/areas/NOPE/batches",
        json={"base_version": 0, "withdrawals": [_withdraw("R-X")]},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------


def test_cycle_is_rejected_with_layers_and_record_numbers(client: TestClient, seeded: dict):
    t1 = _layers(client, "T1")
    # L4 -> L1 closes the T1 chain L1 -> L2 -> L3 -> L4.
    response = _submit(
        client,
        "T1",
        _version(client, "T1"),
        additions=[_add("R-T1-777", t1["L4"], t1["L1"])],
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "cycle_detected"
    cycle = detail["cycle"]
    assert {layer["code"] for layer in cycle["layers"]} == {"L1", "L2", "L3", "L4"}
    assert "R-T1-777" in cycle["record_numbers"]
    assert {"R-T1-001", "R-T1-002", "R-T1-003"} <= set(cycle["record_numbers"])
    assert len(cycle["edges"]) == len(cycle["layers"])
    for edge in cycle["edges"]:
        assert edge["record_numbers"]

    # Nothing changed.
    numbers = {item["record_number"] for item in client.get("/areas/T1/records").json()}
    assert "R-T1-777" not in numbers


def test_cross_area_cycle_is_rejected(client: TestClient, seeded: dict):
    t1 = _layers(client, "T1")
    t2 = _layers(client, "T2")
    # M1 -> M2 -> L4 -> ... -> and M2 -/> anything else; close M2 -> M1 is a
    # 2-cycle, while L4 -> M2 closes a longer cross-trench loop.
    response = _submit(
        client,
        "T2",
        _version(client, "T2"),
        additions=[_add("R-T2-888", t2["M2"], t1["L4"])],
    )
    assert response.status_code == 201, response.text

    response = _submit(
        client,
        "T2",
        _version(client, "T2"),
        additions=[_add("R-T2-889", t1["L4"], t2["M2"])],
    )
    assert response.status_code == 409
    cycle = response.json()["detail"]["cycle"]
    codes = [layer["code"] for layer in cycle["layers"]]
    assert "M2" in codes and "L4" in codes
    assert "R-T2-889" in cycle["record_numbers"]


def test_withdrawal_that_would_only_be_possible_in_cycle_is_fine(
    client: TestClient, seeded: dict
):
    # A batch that both withdraws the closing edge and adds a duplicate one is
    # judged on the resulting graph, not on the intermediate state.
    t1 = _layers(client, "T1")
    response = _submit(
        client,
        "T1",
        _version(client, "T1"),
        additions=[_add("R-T1-401", t1["L1"], t1["L4"])],
        withdrawals=[_withdraw("R-T1-001"), _withdraw("R-T1-002"), _withdraw("R-T1-003")],
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# Query semantics
# ---------------------------------------------------------------------------


def test_rank_uses_published_snapshot_for_other_areas(client: TestClient, seeded: dict):
    layer_id = seeded["demo_rank_layer"]["id"]
    before = _rank(client, layer_id)
    # A batch in T1 does not disturb the rank query in T2.
    t1 = _layers(client, "T1")
    assert (
        _submit(
            client, "T1", _version(client, "T1"), additions=[_add("R-T1-555", t1["L1"], t1["L3"])]
        ).status_code
        == 201
    )
    after = _rank(client, layer_id)
    assert after["earliest_rank"] == before["earliest_rank"]
    assert after["latest_rank"] == before["latest_rank"]
    assert after["site_version"] == before["site_version"] + 1


def test_rank_404_for_unknown_layer(client: TestClient, seeded: dict):
    assert client.get("/ranks/999999").status_code == 404


def test_rank_before_any_relations_returns_full_range(client: TestClient):
    area = client.post("/areas", json={"code": "T9", "name": "T9"}).json()
    layers = [
        client.post(
            "/areas/T9/layers", json={"code": code, "name": code}
        ).json()
        for code in ("A", "B", "C")
    ]
    body = _rank(client, layers[1]["id"])
    assert (body["earliest_rank"], body["latest_rank"]) == (1, 3)
    assert area["version"] == 0


def test_layer_creation_bumps_version_and_blocks_stale_batches(
    client: TestClient, seeded: dict
):
    seen = _version(client, "T2")
    created = client.post("/areas/T2/layers", json={"code": "M3", "name": "更早层"})
    assert created.status_code == 201
    assert created.json()["area_version"] == seen + 1
    t2 = _layers(client, "T2")
    response = _submit(
        client, "T2", seen, additions=[_add("R-T2-601", t2["M3"], t2["M1"])]
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "area_version_conflict"


def test_batch_detail_round_trips(client: TestClient, seeded: dict):
    t2 = _layers(client, "T2")
    response = _submit(
        client,
        "T2",
        _version(client, "T2"),
        additions=[_add("R-T2-701", t2["M2"], t2["M1"])],
        withdrawals=[_withdraw("R-T2-001"), _withdraw("R-T2-002")],
    )
    assert response.status_code == 201, response.text
    batch_id = response.json()["batch_id"]
    detail = client.get(f"/batches/{batch_id}").json()
    assert detail["additions"][0]["record_number"] == "R-T2-701"
    assert [item["record_number"] for item in detail["withdrawals"]] == [
        "R-T2-001",
        "R-T2-002",
    ]
    listed = client.get("/areas/T2/batches").json()
    assert [item["batch_id"] for item in listed][-1] == batch_id


def test_relations_groups_duplicate_records(client: TestClient, seeded: dict):
    relations = client.get("/relations").json()
    pair = next(
        item
        for item in relations["relations"]
        if item["earlier"]["code"] == "M1" and item["later"]["code"] == "M2"
    )
    assert pair["record_numbers"] == ["R-T2-001", "R-T2-002"]
    assert relations["relation_count"] == 5
