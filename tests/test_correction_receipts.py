"""端到端测试：校正回执签发与 HMAC 校验（含密钥轮换与 version=1 兼容）。

每个用例使用独立临时 SQLite 库。默认 fixture 模拟旧式单密钥部署（未配置键环时
回退为 {"dev": CORRECTION_RECEIPT_SECRET}）；keyring_client 模拟显式键环部署。
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

SECRET = "test-correction-receipt-secret"  # 旧版单密钥（CORRECTION_RECEIPT_SECRET）
RING_SECRET_A = "test-ring-secret-a"
RING_SECRET_B = "test-ring-secret-b"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CORRECTION_RECEIPT_SECRET", SECRET)
    monkeypatch.delenv("CORRECTION_RECEIPT_KEYS", raising=False)
    monkeypatch.delenv("CORRECTION_RECEIPT_ACTIVE_KEY", raising=False)
    from app.main import app  # 环境变量设置后再导入

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def keyring_client(tmp_path, monkeypatch):
    """显式键环部署：初始键环只含 key-a 且活跃键为 key-a。"""
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CORRECTION_RECEIPT_SECRET", SECRET)
    monkeypatch.setenv("CORRECTION_RECEIPT_KEYS", json.dumps({"key-a": RING_SECRET_A}))
    monkeypatch.setenv("CORRECTION_RECEIPT_ACTIVE_KEY", "key-a")
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


def make_two_points(client):
    # 1000 处 bias 1.0（20.0 判异常），2000 处 bias 3.0
    make_observation(client, 1000.0, reference=10.0, tolerance=0.5,
                     readings=[9.0, 9.1, 8.9, 20.0])
    make_observation(client, 2000.0, reference=10.0, tolerance=0.5,
                     readings=[7.0, 7.1, 6.9])


@pytest.fixture()
def two_points(client):
    make_two_points(client)
    return client


def expected_signature(key_id, secret, timestamp, reading, bias, corrected, issued_at):
    """version=2 签名：载荷 [2, key_id, ...]，密钥取自键环中 key_id 对应的密钥。"""
    body = json.dumps([2, key_id, timestamp, reading, bias, corrected, issued_at],
                      separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def expected_signature_v1(secret, timestamp, reading, bias, corrected, issued_at):
    """version=1 旧规则签名：载荷 [1, ...]（不含 key_id），密钥为 CORRECTION_RECEIPT_SECRET。"""
    body = json.dumps([1, timestamp, reading, bias, corrected, issued_at],
                      separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def issue(client, timestamp=1500.0, reading=5.0):
    resp = client.post("/correction-receipts",
                       json={"timestamp": timestamp, "reading": reading})
    assert resp.status_code == 200, resp.text
    return resp.json()


def make_v1_receipt(timestamp=1500.0, reading=5.0, bias=2.0, corrected=7.0,
                    issued_at=1789844210712, secret=SECRET):
    """手工构造一份 version=1 历史回执（签发端已升级为 v2，不再产出 v1）。"""
    return {
        "version": 1,
        "request": {"timestamp": timestamp, "reading": reading},
        "bias": bias,
        "corrected": corrected,
        "issued_at": issued_at,
        "receipt": expected_signature_v1(secret, timestamp, reading, bias, corrected, issued_at),
    }


# ---------- 签发 ----------

def test_issue_receipt_basic(two_points):
    body = issue(two_points)
    assert body["version"] == 2
    assert body["key_id"] == "dev"  # 未配置键环时回退为 dev 开发环
    assert body["request"] == {"timestamp": 1500.0, "reading": 5.0}
    assert body["bias"] == pytest.approx(2.0)
    assert body["corrected"] == pytest.approx(7.0)
    assert isinstance(body["issued_at"], int)
    now_ms = int(__import__("time").time() * 1000)
    assert abs(body["issued_at"] - now_ms) < 60_000
    assert isinstance(body["receipt"], str)
    assert len(body["receipt"]) == 64
    assert body["receipt"] == expected_signature(
        "dev", SECRET, 1500.0, 5.0, body["bias"], body["corrected"], body["issued_at"])


def test_issue_receipt_uses_active_key_from_ring(keyring_client):
    make_two_points(keyring_client)
    body = issue(keyring_client)
    assert body["version"] == 2
    assert body["key_id"] == "key-a"
    assert body["receipt"] == expected_signature(
        "key-a", RING_SECRET_A, 1500.0, 5.0, body["bias"], body["corrected"], body["issued_at"])


def test_issue_receipt_normalizes_iso8601_timestamp(two_points):
    body = issue(two_points, timestamp="1970-01-01T00:25:00Z")
    assert body["request"]["timestamp"] == pytest.approx(1500.0)
    # 签名覆盖的是归一化后的 Unix 秒
    assert body["receipt"] == expected_signature(
        "dev", SECRET, 1500.0, 5.0, body["bias"], body["corrected"], body["issued_at"])


def test_issue_receipt_reading_int_accepted(two_points):
    body = issue(two_points, reading=5)
    assert body["request"]["reading"] == pytest.approx(5.0)
    assert body["receipt"] == expected_signature(
        "dev", SECRET, 1500.0, 5.0, body["bias"], body["corrected"], body["issued_at"])


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
    ("version->1", lambda b: {**b, "version": 1}),
    ("version->3", lambda b: {**b, "version": 3}),
    ("key_id", lambda b: {**b, "key_id": "rotated"}),
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
    make_two_points(client)
    body = issue(client)
    assert body["key_id"] == "dev"
    assert client.post("/correction-receipts/verify", json=body).json() == {"valid": True}

    # 未配置键环时 dev 环密钥取自 CORRECTION_RECEIPT_SECRET：换密钥后旧回执无法通过校验
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


# ---------- 密钥轮换 ----------

def test_key_rotation_end_to_end(keyring_client, monkeypatch):
    make_two_points(keyring_client)

    # 轮换前：活跃键 key-a 签发
    old = issue(keyring_client)
    assert old["key_id"] == "key-a"
    assert keyring_client.post("/correction-receipts/verify", json=old).json() == {"valid": True}

    # 轮换：加入 key-b 并切换活跃键，旧键保留在环内 → 轮换前回执仍可验证
    monkeypatch.setenv("CORRECTION_RECEIPT_KEYS",
                       json.dumps({"key-a": RING_SECRET_A, "key-b": RING_SECRET_B}))
    monkeypatch.setenv("CORRECTION_RECEIPT_ACTIVE_KEY", "key-b")
    assert keyring_client.post("/correction-receipts/verify", json=old).json() == {"valid": True}
    new = issue(keyring_client)
    assert new["key_id"] == "key-b"
    assert new["receipt"] == expected_signature(
        "key-b", RING_SECRET_B, 1500.0, 5.0, new["bias"], new["corrected"], new["issued_at"])
    assert keyring_client.post("/correction-receipts/verify", json=new).json() == {"valid": True}

    # 旧键移出键环：key-a 签发的回执判无效，key-b 签发的回执不受影响
    monkeypatch.setenv("CORRECTION_RECEIPT_KEYS", json.dumps({"key-b": RING_SECRET_B}))
    assert keyring_client.post("/correction-receipts/verify", json=old).json() == {"valid": False}
    assert keyring_client.post("/correction-receipts/verify", json=new).json() == {"valid": True}


def test_verify_selects_key_exactly_by_key_id(keyring_client, monkeypatch):
    make_two_points(keyring_client)
    body = issue(keyring_client)  # key-a 签发
    monkeypatch.setenv("CORRECTION_RECEIPT_KEYS",
                       json.dumps({"key-a": RING_SECRET_A, "key-b": RING_SECRET_B}))

    # key_id 换成环内另一把键：按该键重算的签名与原回执不符
    flipped = {**body, "key_id": "key-b"}
    assert keyring_client.post("/correction-receipts/verify", json=flipped).json() == {"valid": False}

    # 用 key-b 正确签发的回执可以验证（验签严格按 key_id 选键）
    signed_by_b = {**body, "key_id": "key-b",
                   "receipt": expected_signature("key-b", RING_SECRET_B, 1500.0, 5.0,
                                                 body["bias"], body["corrected"], body["issued_at"])}
    assert keyring_client.post("/correction-receipts/verify", json=signed_by_b).json() == {"valid": True}


def test_verify_unknown_key_id_returns_false(keyring_client):
    make_two_points(keyring_client)
    body = issue(keyring_client)
    forged = {**body, "key_id": "no-such-key"}
    assert keyring_client.post("/correction-receipts/verify", json=forged).json() == {"valid": False}


# ---------- version=1 历史回执兼容 ----------

def test_verify_v1_receipt_still_accepted(client):
    resp = client.post("/correction-receipts/verify", json=make_v1_receipt())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"valid": True}


def test_verify_v1_receipt_uses_legacy_secret_not_key_ring(keyring_client):
    # v1 沿用 CORRECTION_RECEIPT_SECRET 的旧密钥规则，与键环无关：
    # 用旧密钥签的 v1 回执有效；即使用键环中的密钥签 v1 也判无效
    resp = keyring_client.post("/correction-receipts/verify", json=make_v1_receipt())
    assert resp.json() == {"valid": True}
    resp = keyring_client.post("/correction-receipts/verify",
                               json=make_v1_receipt(secret=RING_SECRET_A))
    assert resp.json() == {"valid": False}


def test_verify_v1_receipt_tampered_rejected(client):
    body = make_v1_receipt()
    body["bias"] += 0.01
    assert client.post("/correction-receipts/verify", json=body).json() == {"valid": False}


def test_verify_v1_receipt_ignores_key_id_field(client):
    # v1 签名载荷不含 key_id：携带该字段不影响旧回执校验
    body = {**make_v1_receipt(), "key_id": "anything"}
    assert client.post("/correction-receipts/verify", json=body).json() == {"valid": True}


# ---------- 结构/类型错误 → 422 ----------

def test_verify_missing_fields_return_422(two_points):
    body = issue(two_points)
    for key in ("version", "key_id", "request", "bias", "corrected", "issued_at", "receipt"):
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
        {**body, "version": "2"},
        {**body, "version": True},
        {**body, "key_id": 123},
        {**body, "key_id": True},
        {**body, "key_id": None},                           # v2 的 key_id 不可为 null
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
