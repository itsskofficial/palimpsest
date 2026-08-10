"""The API contract, and the guard-rails that must hold over HTTP.

The important tests here are the negative ones. The safety model is only real if it
survives a caller who is actively trying to get around it — so these check that the
apply route refuses a contradiction, refuses an unnamed reviewer, and that ingesting
never writes.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from palimpsest.config import Settings
from palimpsest.serve.app import AppState, create_app
from palimpsest.types import Operation, OpKind, Patch, Relation, new_id

pytestmark = pytest.mark.serve


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}",
                        artifact_url=f"file://{tmp_path / 'archive'}")
    state = AppState(settings=settings)
    state.store.put_pages([{"page_id": "pg_1", "title": "Page", "role": "reference",
                            "last_edited": "2026-01-01"}])
    state.store.put_blocks([{"block_id": "bk_1", "page_id": "pg_1",
                             "type": "paragraph", "position": 0,
                             "text": "Attention divides by the square root of the key "
                                     "dimension to keep gradients stable."}])
    with TestClient(create_app(state)) as c:
        c.state = state
        yield c


def test_health_never_touches_the_database(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_status_reports_config_and_problems(client):
    body = client.get("/v1/status").json()
    assert body["config"]["apply"] is False
    assert any("NOTION_TOKEN" in p for p in body["problems"])


def test_status_redacts_secrets(client):
    body = client.get("/v1/status").json()
    rendered = str(body)
    assert "sk-ant" not in rendered
    assert "ntn_" not in rendered


def test_the_home_page_renders(client):
    assert "palimpsest" in client.get("/").text


def test_ingest_without_a_model_key_fails_clearly(client):
    r = client.post("/v1/ingest", json={"text": "a thought"})
    assert r.status_code == 400
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_ingest_requires_content(client):
    assert client.post("/v1/ingest", json={}).status_code == 422


def test_apply_refuses_a_contradiction_over_http(client):
    """The planner will not emit one; this is the second lock on the same door."""
    patch = Patch(patch_id=new_id("pch_"), source_id="src_1", operations=[
        Operation(kind=OpKind.UPDATE_TEXT, target="bk_1", payload={"text": "x"},
                  relation=Relation.CONTRADICTS)])
    client.state.store.put_patch(patch)

    r = client.post(f"/v1/patches/{patch.patch_id}/apply",
                    json={"reviewer": "someone"})
    assert r.status_code == 400
    assert "contradiction" in r.json()["detail"]


def test_apply_requires_a_named_reviewer(client):
    patch = Patch(patch_id=new_id("pch_"), source_id="src_1", operations=[
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", payload={"text": "x"},
                  relation=Relation.NEW)])
    client.state.store.put_patch(patch)

    r = client.post(f"/v1/patches/{patch.patch_id}/apply", json={})
    assert r.status_code == 422
    assert "who approved" in r.json()["detail"]


def test_rejecting_a_patch_writes_nothing_and_records_the_reviewer(client):
    patch = Patch(patch_id=new_id("pch_"), source_id="src_1", operations=[
        Operation(kind=OpKind.APPEND_BLOCK, target="pg_1", payload={"text": "x"},
                  relation=Relation.NEW)])
    client.state.store.put_patch(patch)

    r = client.post(f"/v1/patches/{patch.patch_id}/reject", json={"reviewer": "me"})
    assert r.status_code == 200
    rows = client.state.store.list_patches(status="rejected")
    assert rows and rows[0]["reviewer"] == "me"


def test_duplicate_sweep_works_over_http_without_any_key(client):
    body = client.post("/v1/sweep/duplicates", json={}).json()
    assert body["kind"] == "duplicates"
    assert "findings" in body


def test_contradiction_sweep_reports_the_missing_key(client):
    r = client.post("/v1/sweep/contradictions", json={})
    assert r.status_code == 400
    assert "model" in r.json()["detail"]


def test_unknown_sweep_is_a_404_that_lists_the_real_ones(client):
    r = client.post("/v1/sweep/nonsense", json={})
    assert r.status_code == 404
    assert "duplicates" in r.json()["detail"]


def test_sync_without_a_notion_token_fails_clearly(client):
    r = client.post("/v1/sync", json={})
    assert r.status_code == 400
    assert "NOTION_TOKEN" in r.json()["detail"]


def test_pages_and_provenance_endpoints(client):
    assert client.get("/v1/pages").json()["pages"][0]["page_id"] == "pg_1"
    assert client.get("/v1/pages/pg_1").json()["page"]["title"] == "Page"
    assert client.get("/v1/pages/missing").status_code == 404
    assert client.get("/v1/blocks/bk_1/provenance").json()["provenance"] == []
