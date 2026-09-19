"""端到端测试：校正回执签发与 HMAC 校验。每个用例使用独立临时 SQLite 库。"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

SECRET = "test-correction-receipt-secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CORRECTION_RECEIPT_SECRET", SECRET)
    from app.main import app  # 环境变量设置后再导入

    with TestClient(app) as c:
        yield c


def make_observation(client, ts, reference, tolerance, readings):
    resp = client.post(
        "/observations",
        json={"timestamp": ts, "reference": reference, "tolerance": tolerance, "readings": readings},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture()
def two_points(client):
    # 1000 处 bias 1.0（20.0 判异常），2000 处 bias 3.0
    make_observation(client, 1000.0, reference=10.0, tolerance=0.5,
                     readings=[9.0, 9.1, 8.9, 20.0])
    make_observation(client, 2000.0, reference=10.0, tolerance=0.5,
                     readings=[7.0, 7.1, 6.9])
    return client


def expected_signature(version, timestamp, reading, bias, corrected, issued_at):
    body = json.dumps([version, timestamp, reading, bias, corrected, issued_at],
                      separators=(",", ":")).encode("utf-8")
    return hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def issue(two_points, timestamp=1500.0, reading=5.0):
    resp = two_points.post("/correction-receipts",
                           json={"timestamp": timestamp, "reading": reading})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 签发 ----------

def test_issue_receipt_basic(two_points):
    body = issue(two_points)
    assert body["version"] == 1
    assert body["request"] == {"timestamp": 1500.0, "reading": 5.0}
    assert body["bias"] == pytest.approx(2.0)
    assert body["corrected"] == pytest.approx(7.0)
    assert isinstance(body["issued_at"], int)
    now_ms = int(__import__("time").time() * 1000)
    assert abs(body["issued_at"] - now_ms) < 60_000
    assert isinstance(body["receipt"], str)
    assert len(body["receipt"]) == 64
    assert body["receipt"] == expected_signature(
        1, 1500.0, 5.0, body["bias"], body["corrected"], body["issued_at"])


def test_issue_receipt_normalizes_iso8601_timestamp(two_points):
    body = issue(two_points, timestamp="1970-01-01T00:25:00Z")
    assert body["request"]["timestamp"] == pytest.approx(1500.0)
    # 签名覆盖的是归一化后的 Unix 秒
    assert body["receipt"] == expected_signature(
        1, 1500.0, 5.0, body["bias"], body["corrected"], body["issued_at"])


def test_issue_receipt_reading_int_accepted(two_points):
    body = issue(two_points, reading=5)
    assert body["request"]["reading"] == pytest.approx(5.0)
    assert body["receipt"] == expected_signature(
        1, 1500.0, 5.0, body["bias"], body["corrected"], body["issued_at"])


def test_issue_receipt_reuses_drift_compensation(two_points):
    # 与 /corrections 完全一致：中点插值 2.0，恰落在观测点上直接用该点偏差
    midpoint = issue(two_points, timestamp=1500.0, reading=5.0)
    assert midpoint["bias"] == pytest.approx(2.0)
    assert midpoint["corrected"] == pytest.approx(7.0)
    exact = issue(two_points, timestamp=2000.0, reading=5.0)
    assert exact["bias"] == pytest.approx(3.0)
    assert exact["corrected"] == pytest.approx(8.0)


def test_issue_receipt_outside_coverage_rejected(two_points):
    resp = two_points.post("/correction-receipts",
                           json={"timestamp": 42.0, "reading": 5.0})
    assert resp.status_code == 400
    assert "outside calibration coverage" in resp.json()["detail"]


def test_issue_receipt_invalid_payload_returns_422(two_points):
    for payload in (
        {"reading": 5.0},                                   # 缺 timestamp
        {"timestamp": 1500.0},                              # 缺 reading
        {"timestamp": None, "reading": 5.0},                # timestamp 为 null
        {"timestamp": 1500.0, "reading": "x"},              # reading 非数值
        {},                                                 # 空对象
    ):
        resp = two_points.post("/correction-receipts", json=payload)
        assert resp.status_code == 422, payload


# ---------- 校验 ----------

def test_verify_accepts_untampered_receipt(two_points):
    body = issue(two_points)
    resp = two_points.post("/correction-receipts/verify", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"valid": True}


@pytest.mark.parametrize("field,mutate", [
    ("version", lambda b: {**b, "version": 2}),
    ("request.timestamp", lambda b: {**b, "request": {**b["request"], "timestamp": 1501.0}}),
    ("request.reading", lambda b: {**b, "request": {**b["request"], "reading": 5.1}}),
    ("bias", lambda b: {**b, "bias": b["bias"] + 0.01}),
    ("corrected", lambda b: {**b, "corrected": b["corrected"] + 0.01}),
    ("issued_at", lambda b: {**b, "issued_at": b["issued_at"] + 1}),
    ("receipt", lambda b: {**b, "receipt": "0" * 64}),
])
def test_verify_rejects_tampered_signed_fields(two_points, field, mutate):
    body = issue(two_points)
    tampered = mutate(body)
    resp = two_points.post("/correction-receipts/verify", json=tampered)
    assert resp.status_code == 200
    assert resp.json() == {"valid": False}, field


def test_verify_ignores_unsigned_extra_fields(two_points):
    body = issue(two_points)
    body["extra"] = {"note": "not signed"}
    body["request"]["trace_id"] = "abc"
    resp = two_points.post("/correction-receipts/verify", json=body)
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}


def test_verify_secret_mismatch_fails(client, monkeypatch):
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9, 20.0])
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])
    body = issue(client)
    assert client.post("/correction-receipts/verify", json=body).json() == {"valid": True}

    # 换密钥后旧回执无法通过校验
    monkeypatch.setenv("CORRECTION_RECEIPT_SECRET", "rotated-secret")
    resp = client.post("/correction-receipts/verify", json=body)
    assert resp.json() == {"valid": False}

    # 新密钥签发的回执恢复有效
    resp = client.post("/correction-receipts", json={"timestamp": 1500.0, "reading": 5.0})
    assert resp.status_code == 200
    assert client.post("/correction-receipts/verify", json=resp.json()).json() == {"valid": True}


def test_verify_uses_constant_time_comparison(two_points):
    # 同长度伪造签名也必须判无效（常量时间比较不影响结果）
    body = issue(two_points)
    forged = body["receipt"][:-1] + ("0" if body["receipt"][-1] != "0" else "1")
    body["receipt"] = forged
    assert two_points.post("/correction-receipts/verify", json=body).json() == {"valid": False}


# ---------- 结构/类型错误 → 422 ----------

def test_verify_missing_fields_return_422(two_points):
    body = issue(two_points)
    for key in ("version", "request", "bias", "corrected", "issued_at", "receipt"):
        partial = {k: v for k, v in body.items() if k != key}
        resp = two_points.post("/correction-receipts/verify", json=partial)
        assert resp.status_code == 422, key
    for key in ("timestamp", "reading"):
        partial = {**body, "request": {k: v for k, v in body["request"].items() if k != key}}
        resp = two_points.post("/correction-receipts/verify", json=partial)
        assert resp.status_code == 422, key


def test_verify_wrong_types_return_422(two_points):
    body = issue(two_points)
    invalid_bodies = [
        {**body, "version": "1"},
        {**body, "version": True},
        {**body, "request": [1500.0, 5.0]},
        {**body, "request": {"timestamp": "1500.0", "reading": 5.0}},
        {**body, "request": {"timestamp": True, "reading": 5.0}},
        {**body, "request": {"timestamp": None, "reading": 5.0}},
        {**body, "request": {"timestamp": 1500.0, "reading": "5.0"}},
        {**body, "bias": "2.0"},
        {**body, "corrected": None},
        {**body, "issued_at": body["issued_at"] + 0.5},   # 毫秒必须是整数
        {**body, "issued_at": str(body["issued_at"])},
        {**body, "receipt": 123},
        {**body, "receipt": None},
        "not-an-object",
        [body],
    ]
    for invalid in invalid_bodies:
        resp = two_points.post("/correction-receipts/verify", json=invalid)
        assert resp.status_code == 422, invalid
