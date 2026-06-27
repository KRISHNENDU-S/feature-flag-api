"""Integration tests for the HTTP API using FastAPI's TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client():
    # Reset module-level singletons between tests.
    main.storage.clear()
    main.cache.clear()
    with TestClient(main.app) as c:
        yield c


def _flag_payload(name="checkout-v2", default_state=False, **extra):
    payload = {"name": name, "default_state": default_state, "rules": []}
    payload.update(extra)
    return payload


def test_create_flag_returns_201(client):
    resp = client.post("/flags", json=_flag_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "checkout-v2"
    assert "id" in body and "created_at" in body


def test_duplicate_name_returns_409(client):
    client.post("/flags", json=_flag_payload(name="dup"))
    resp = client.post("/flags", json=_flag_payload(name="dup"))
    assert resp.status_code == 409


def test_invalid_payload_returns_422(client):
    # missing required default_state
    resp = client.post("/flags", json={"name": "bad"})
    assert resp.status_code == 422


def test_list_and_get_flag(client):
    created = client.post("/flags", json=_flag_payload(name="list-me")).json()
    assert client.get("/flags").json()[0]["id"] == created["id"]
    assert client.get(f"/flags/{created['id']}").status_code == 200


def test_get_missing_flag_returns_404(client):
    assert client.get("/flags/nope").status_code == 404


def test_list_filter_by_default_state(client):
    client.post("/flags", json=_flag_payload(name="on-flag", default_state=True))
    client.post("/flags", json=_flag_payload(name="off-flag", default_state=False))

    on = client.get("/flags", params={"default_state": "true"}).json()
    assert [f["name"] for f in on] == ["on-flag"]

    off = client.get("/flags", params={"default_state": "false"}).json()
    assert [f["name"] for f in off] == ["off-flag"]


def test_list_filter_by_partial_name_case_insensitive(client):
    client.post("/flags", json=_flag_payload(name="checkout-v1"))
    client.post("/flags", json=_flag_payload(name="checkout-v2"))
    client.post("/flags", json=_flag_payload(name="search-banner"))

    # Partial, case-insensitive substring match.
    matches = client.get("/flags", params={"name": "CHECKOUT"}).json()
    assert sorted(f["name"] for f in matches) == ["checkout-v1", "checkout-v2"]

    none = client.get("/flags", params={"name": "missing"}).json()
    assert none == []


def test_list_filters_combine_with_and(client):
    client.post("/flags", json=_flag_payload(name="checkout-on", default_state=True))
    client.post("/flags", json=_flag_payload(name="checkout-off", default_state=False))
    client.post("/flags", json=_flag_payload(name="search-on", default_state=True))

    result = client.get(
        "/flags", params={"name": "checkout", "default_state": "true"}
    ).json()
    assert [f["name"] for f in result] == ["checkout-on"]


def test_list_without_filters_returns_all(client):
    client.post("/flags", json=_flag_payload(name="a"))
    client.post("/flags", json=_flag_payload(name="b"))
    assert len(client.get("/flags").json()) == 2


def test_delete_flag_returns_204_and_404_after(client):
    created = client.post("/flags", json=_flag_payload(name="del-me")).json()
    assert client.delete(f"/flags/{created['id']}").status_code == 204
    assert client.get(f"/flags/{created['id']}").status_code == 404


def test_delete_missing_returns_404(client):
    assert client.delete("/flags/nope").status_code == 404


def test_evaluate_flag(client):
    payload = _flag_payload(
        name="eval-flag",
        default_state=False,
        rules=[
            {"field": "subscription_tier", "operator": "equals", "value": "premium"}
        ],
    )
    flag = client.post("/flags", json=payload).json()

    ctx = {"user_id": "u1", "subscription_tier": "premium", "region": "us"}
    resp = client.post(f"/flags/{flag['id']}/evaluate", json=ctx)
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    ctx_free = {"user_id": "u2", "subscription_tier": "free", "region": "us"}
    resp2 = client.post(f"/flags/{flag['id']}/evaluate", json=ctx_free)
    assert resp2.json()["enabled"] is False


def test_evaluate_missing_flag_returns_404(client):
    ctx = {"user_id": "u1", "subscription_tier": "free", "region": "us"}
    assert client.post("/flags/nope/evaluate", json=ctx).status_code == 404


def test_delete_invalidates_cache(client):
    flag = client.post("/flags", json=_flag_payload(name="cache-inv", default_state=True)).json()
    ctx = {"user_id": "u1", "subscription_tier": "free", "region": "us"}
    client.post(f"/flags/{flag['id']}/evaluate", json=ctx)  # cache populated
    client.delete(f"/flags/{flag['id']}")  # should invalidate
    # Re-creating with the same id is not possible; just verify cache shrank.
    assert main.cache.stats()["size"] == 0


def test_update_flag_success_returns_200(client):
    created = client.post(
        "/flags", json=_flag_payload(name="patch-me", default_state=False)
    ).json()

    resp = client.patch(
        f"/flags/{created['id']}",
        json={"name": "patched", "default_state": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == created["id"]          # id is preserved
    assert body["name"] == "patched"            # updated field
    assert body["default_state"] is True        # updated field
    assert body["created_at"] == created["created_at"]  # untouched


def test_update_partial_only_changes_provided_fields(client):
    created = client.post(
        "/flags",
        json=_flag_payload(name="partial", default_state=True, percentage=10),
    ).json()

    resp = client.patch(f"/flags/{created['id']}", json={"default_state": False})
    body = resp.json()
    assert body["default_state"] is False       # changed
    assert body["name"] == "partial"            # unchanged
    assert body["percentage"] == 10             # unchanged


def test_update_missing_flag_returns_404(client):
    assert client.patch("/flags/nope", json={"name": "x"}).status_code == 404


def test_update_name_conflict_returns_409(client):
    client.post("/flags", json=_flag_payload(name="taken"))
    other = client.post("/flags", json=_flag_payload(name="other")).json()

    resp = client.patch(f"/flags/{other['id']}", json={"name": "taken"})
    assert resp.status_code == 409


def test_update_same_name_is_allowed(client):
    created = client.post("/flags", json=_flag_payload(name="keep-name")).json()
    # Re-sending the flag's own name must not be treated as a conflict.
    resp = client.patch(
        f"/flags/{created['id']}", json={"name": "keep-name", "default_state": True}
    )
    assert resp.status_code == 200


def test_update_invalidates_cache(client):
    created = client.post(
        "/flags", json=_flag_payload(name="cache-update", default_state=True)
    ).json()
    ctx = {"user_id": "u1", "subscription_tier": "free", "region": "us"}

    # Populate the cache for this flag.
    client.post(f"/flags/{created['id']}/evaluate", json=ctx)
    assert main.cache.stats()["size"] == 1

    # Updating the flag must drop its cached evaluations.
    resp = client.patch(f"/flags/{created['id']}", json={"default_state": False})
    assert resp.status_code == 200
    assert main.cache.stats()["size"] == 0

    # Re-evaluating now reflects the new default_state (no stale cache hit).
    result = client.post(f"/flags/{created['id']}/evaluate", json=ctx).json()
    assert result["enabled"] is False


def test_health_reports_counts_and_hit_rate(client):
    client.post("/flags", json=_flag_payload(name="h1"))
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_flags"] == 1
    assert "hit_rate" in body["cache"]
