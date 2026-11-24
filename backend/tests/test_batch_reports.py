"""合作方批次报案的服务级回归。

需要可连接的 PostgreSQL（使用 DATABASE_URL，默认指向 compose 的 db 服务）。
本地无数据库或缺少 httpx 时整个模块跳过，不影响其他测试收集。
"""
from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("httpx")
psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

DSN = os.getenv("DATABASE_URL", "postgresql://claims:claims@localhost:17744/claims")

try:
    with psycopg.connect(DSN, connect_timeout=3):
        DB_AVAILABLE = True
except Exception:  # noqa: BLE001
    DB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="PostgreSQL 不可达，跳过批次报案回归")


@pytest.fixture(scope="module")
def client():
    from src.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_headers(client):
    resp = client.post("/auth/login", json={"username": "intake@claims.local", "password": "demo123"})
    if resp.status_code != 200:
        pytest.skip("演示账号未初始化，请先运行 scripts/seed_demo.py")
    token = resp.json()["token"]
    manager = client.post("/auth/login", json={"username": "manager@claims.local", "password": "demo123"}).json()["token"]
    return {"intake": {"Authorization": f"Bearer {token}"}, "manager": {"Authorization": f"Bearer {manager}"}}


def _item(run, key, rev=1, **over):
    body = {
        "report_no": f"RPT-PY-{run}-{key}",
        "revision_no": rev,
        "policy_no": f"PA-PY-{run}",
        "customer_name": "张三",
        "incident_type": "车损",
        "incident_date": "2026-11-20",
        "loss_amount": 12000,
        "region": "都市",
        "claimant_phone": "13900000001",
    }
    body.update(over)
    return body


def test_same_policy_same_day_different_accidents_create_two_claims(client, auth_headers):
    run = uuid.uuid4().hex[:8]
    resp = client.post("/report-batches", headers=auth_headers["intake"], json={
        "batch_no": f"PY-{run}-1",
        "items": [_item(run, "A", accident_no="ACC-1"), _item(run, "B", accident_no="ACC-2")],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["counts"]["created"] == 2
    assert data["items"][0]["claim_id"] != data["items"][1]["claim_id"]


def test_batch_retry_is_idempotent(client, auth_headers):
    run = uuid.uuid4().hex[:8]
    payload = {"batch_no": f"PY-{run}-IDEM", "items": [_item(run, "I")]}
    first = client.post("/report-batches", headers=auth_headers["intake"], json=payload)
    assert first.status_code == 200
    second = client.post("/report-batches", headers=auth_headers["intake"], json=payload)
    assert second.status_code == 200
    assert second.json().get("replayed") is True
    assert second.json()["items"][0]["claim_id"] == first.json()["items"][0]["claim_id"]


def test_same_key_different_content_rejects_whole_batch_atomically(client, auth_headers):
    run = uuid.uuid4().hex[:8]
    h = auth_headers["intake"]
    ok = client.post("/report-batches", headers=h, json={"batch_no": f"PY-{run}-BASE", "items": [_item(run, "C")]})
    assert ok.status_code == 200
    before = len(client.get("/claims", headers=h).json()["items"])

    conflict = client.post("/report-batches", headers=h, json={"batch_no": f"PY-{run}-CONFLICT", "items": [
        _item(run, "C", customer_name="被篡改"),
        _item(run, "NEW"),
    ]})
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["status"] == "rejected"
    statuses = [i["status"] for i in body["items"]]
    assert "conflict" in statuses and "not_processed" in statuses
    after = len(client.get("/claims", headers=h).json()["items"])
    assert after == before


def test_stale_revision_does_not_overwrite(client, auth_headers):
    run = uuid.uuid4().hex[:8]
    h = auth_headers["intake"]
    client.post("/report-batches", headers=h, json={"batch_no": f"PY-{run}-R3", "items": [
        _item(run, "S", rev=3, customer_name="新资料", loss_amount=30000),
    ]})
    stale = client.post("/report-batches", headers=h, json={"batch_no": f"PY-{run}-R1", "items": [
        _item(run, "S", rev=1, customer_name="旧资料", loss_amount=1),
    ]})
    assert stale.json()["items"][0]["status"] == "stale_revision"
    report = client.get(f"/partner-reports/RPT-PY-{run}-S", headers=h).json()
    assert report["current_revision"] == 3


def test_new_revision_on_graded_claim_waits_for_confirmation(client, auth_headers):
    run = uuid.uuid4().hex[:8]
    hi, hm = auth_headers["intake"], auth_headers["manager"]
    created = client.post("/report-batches", headers=hi, json={"batch_no": f"PY-{run}-G", "items": [_item(run, "G")]})
    claim_id = created.json()["items"][0]["claim_id"]
    client.post(f"/claims/{claim_id}/verify-documents", headers=hi,
                json={"received_documents": ["身份证明", "事故证明", "损失清单"]})
    graded = client.post(f"/claims/{claim_id}/model", headers=hm)
    assert graded.status_code == 200 and graded.json()["risk_level"]

    pending = client.post("/report-batches", headers=hi, json={"batch_no": f"PY-{run}-G2", "items": [
        _item(run, "G", rev=2, customer_name="分级后改名"),
    ]})
    assert pending.json()["items"][0]["status"] == "pending_confirmation"
    detail = client.get(f"/claims/{claim_id}", headers=hi).json()
    assert detail["pending_revision_count"] == 1
    assert detail["claim"]["customer_name"] == "张三"

    queue = client.get(f"/report-revisions/pending?claim_id={claim_id}", headers=hi).json()
    rev_id = queue["items"][0]["revision_id"]
    done = client.post(f"/report-revisions/{rev_id}/confirm", headers=hi, json={"note": "核实"})
    assert done.status_code == 200 and done.json()["status"] == "confirmed"
    assert client.get(f"/claims/{claim_id}", headers=hi).json()["claim"]["customer_name"] == "分级后改名"


def test_batch_receipt_queryable(client, auth_headers):
    run = uuid.uuid4().hex[:8]
    h = auth_headers["intake"]
    bno = f"PY-{run}-RCPT"
    client.post("/report-batches", headers=h, json={"batch_no": bno, "items": [_item(run, "Q")]})
    receipt = client.get(f"/report-batches/{bno}", headers=h)
    assert receipt.status_code == 200
    body = receipt.json()
    assert body["batch_no"] == bno and len(body["items"]) == 1
    assert body["original_receipt"]["batch_no"] == bno
