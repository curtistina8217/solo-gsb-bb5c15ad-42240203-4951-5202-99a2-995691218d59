"""端到端测试：校正回执签发、HMAC 校验、密钥轮换与 v1 向后兼容。

每个用例使用独立临时 SQLite 库；默认 fixture 配置双密钥环（old+new），
当前签发键为 new，旧密钥规则由 CORRECTION_RECEIPT_SECRET 提供。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

V1_SECRET = "test-correction-receipt-secret"
KEY_ID_OLD = "2026-03"
KEY_ID_NEW = "2026-09"
KEY_OLD = "key-2026-03-secret"
KEY_NEW = "key-2026-09-secret"


def sign_v2(key_id: str, secret: str, fields: tuple) -> str:
    body = json.dumps([2, key_id, *fields], separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def sign_v1(secret: str, fields: tuple) -> str:
    body = json.dumps([1, *fields], separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    # 旧版单密钥规则（v1 验签）始终可用
    monkeypatch.setenv("CORRECTION_RECEIPT_SECRET", V1_SECRET)
    monkeypatch.setenv(
        "CORRECTION_RECEIPT_KEYS",
        json.dumps({KEY_ID_OLD: KEY_OLD, KEY_ID_NEW: KEY_NEW}),
    )
    monkeypatch.setenv("CORRECTION_RECEIPT_ACTIVE_KEY", KEY_ID_NEW)
    from app.main import app  # 环境变量设置后再导入

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def legacy_client(tmp_path, monkeypatch):
    """未配置密钥环：回退旧版单密钥（等价于裸 docker compose up）。"""
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("CORRECTION_RECEIPT_SECRET", V1_SECRET)
    monkeypatch.delenv("CORRECTION_RECEIPT_KEYS", raising=False)
    monkeypatch.delenv("CORRECTION_RECEIPT_ACTIVE_KEY", raising=False)
    from app.main import app

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


@pytest.fixture()
def legacy_two_points(legacy_client):
    make_observation(legacy_client, 1000.0, reference=10.0, tolerance=0.5,
                     readings=[9.0, 9.1, 8.9, 20.0])
    make_observation(legacy_client, 2000.0, reference=10.0, tolerance=0.5,
                     readings=[7.0, 7.1, 6.9])
    return legacy_client


def issue(two_points, timestamp=1500.0, reading=5.0):
    resp = two_points.post("/correction-receipts",
                           json={"timestamp": timestamp, "reading": reading})
    assert resp.status_code == 200, resp.text
    return resp.json()


def signed_fields(body):
    return (
        body["request"]["timestamp"],
        body["request"]["reading"],
        body["bias"],
        body["corrected"],
        body["issued_at"],
    )


def v1_receipt_from(body, secret=V1_SECRET, *, issued_at=None, key_id_field=False):
    """用 v2 回执的载荷手工构造一份 v1 回执（模拟轮换升级前已签发的旧回执）。"""
    v1 = {
        "version": 1,
        "request": body["request"],
        "bias": body["bias"],
        "corrected": body["corrected"],
        "issued_at": body["issued_at"] if issued_at is None else issued_at,
        "receipt": sign_v1(
            secret,
            (
                body["request"]["timestamp"],
                body["request"]["reading"],
                body["bias"],
                body["corrected"],
                body["issued_at"] if issued_at is None else issued_at,
            ),
        ),
    }
    if key_id_field:
        v1["key_id"] = KEY_ID_NEW  # v1 中该字段不参与签名也不参与选键
    return v1


# ---------- v2 签发 ----------

def test_issue_receipt_basic(two_points):
    body = issue(two_points)
    assert body["version"] == 2
    assert body["key_id"] == KEY_ID_NEW
    assert body["request"] == {"timestamp": 1500.0, "reading": 5.0}
    assert body["bias"] == pytest.approx(2.0)
    assert body["corrected"] == pytest.approx(7.0)
    assert isinstance(body["issued_at"], int)
    now_ms = int(time.time() * 1000)
    assert abs(body["issued_at"] - now_ms) < 60_000
    assert isinstance(body["receipt"], str)
    assert len(body["receipt"]) == 64
    assert body["receipt"] == sign_v2(KEY_ID_NEW, KEY_NEW, signed_fields(body))


def test_issue_receipt_normalizes_iso8601_timestamp(two_points):
    body = issue(two_points, timestamp="1970-01-01T00:25:00Z")
    assert body["request"]["timestamp"] == pytest.approx(1500.0)
    # 签名覆盖的是归一化后的 Unix 秒
    assert body["receipt"] == sign_v2(KEY_ID_NEW, KEY_NEW, signed_fields(body))


def test_issue_receipt_reading_int_accepted(two_points):
    body = issue(two_points, reading=5)
    assert body["request"]["reading"] == pytest.approx(5.0)
    assert body["receipt"] == sign_v2(KEY_ID_NEW, KEY_NEW, signed_fields(body))


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


# ---------- v2 校验 ----------

def test_verify_accepts_untampered_receipt(two_points):
    body = issue(two_points)
    resp = two_points.post("/correction-receipts/verify", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"valid": True}


@pytest.mark.parametrize("field,mutate", [
    ("version=1", lambda b: {**b, "version": 1}),
    ("version=3", lambda b: {**b, "version": 3}),
    ("key_id=old", lambda b: {**b, "key_id": KEY_ID_OLD}),
    ("key_id=unknown", lambda b: {**b, "key_id": "2099-01"}),
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


def test_verify_missing_key_id_returns_false(two_points):
    # key_id 是 v2 签名字段：缺失时模型给 None，判 valid=false（而非 422）
    body = issue(two_points)
    body.pop("key_id")
    resp = two_points.post("/correction-receipts/verify", json=body)
    assert resp.status_code == 200
    assert resp.json() == {"valid": False}


def test_verify_ignores_unsigned_extra_fields(two_points):
    body = issue(two_points)
    body["extra"] = {"note": "not signed"}
    body["request"]["trace_id"] = "abc"
    resp = two_points.post("/correction-receipts/verify", json=body)
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}


def test_verify_uses_constant_time_comparison(two_points):
    # 同长度伪造签名也必须判无效（常量时间比较不影响结果）
    body = issue(two_points)
    forged = body["receipt"][:-1] + ("0" if body["receipt"][-1] != "0" else "1")
    body["receipt"] = forged
    assert two_points.post("/correction-receipts/verify", json=body).json() == {"valid": False}


# ---------- 密钥轮换 ----------

def test_rotate_active_key_keeps_old_receipts_valid(two_points, monkeypatch):
    # 以 new 签发
    new_receipt = issue(two_points)
    assert new_receipt["key_id"] == KEY_ID_NEW

    # 轮换：活跃键切到 old，new 仍保留在键环中
    monkeypatch.setenv("CORRECTION_RECEIPT_ACTIVE_KEY", KEY_ID_OLD)
    old_receipt = issue(two_points)
    assert old_receipt["key_id"] == KEY_ID_OLD
    assert old_receipt["receipt"] == sign_v2(KEY_ID_OLD, KEY_OLD, signed_fields(old_receipt))

    # 新（old 签发）旧（new 签发）回执都可验证：验签严格按各自 key_id 选键
    assert two_points.post("/correction-receipts/verify",
                           json=old_receipt).json() == {"valid": True}
    assert two_points.post("/correction-receipts/verify",
                           json=new_receipt).json() == {"valid": True}


def test_removed_key_receipt_becomes_invalid(two_points, monkeypatch):
    body = issue(two_points)  # 以 new 签发
    assert two_points.post("/correction-receipts/verify", json=body).json() == {"valid": True}

    # 从键环移除 new（模拟旧键退役），换一个全新的活跃键
    monkeypatch.setenv(
        "CORRECTION_RECEIPT_KEYS",
        json.dumps({KEY_ID_OLD: KEY_OLD, "2027-01": "future"}),
    )
    monkeypatch.setenv("CORRECTION_RECEIPT_ACTIVE_KEY", KEY_ID_OLD)

    # 已移除键的回执立刻无法通过校验
    assert two_points.post("/correction-receipts/verify", json=body).json() == {"valid": False}
    # 未知 key_id 的伪造回执同理
    forged = {**body, "key_id": "not-in-ring", "receipt": "0" * 64}
    assert two_points.post("/correction-receipts/verify", json=forged).json() == {"valid": False}
    # 服务照常以保留下来的键签发
    fresh = issue(two_points)
    assert fresh["key_id"] == KEY_ID_OLD
    assert two_points.post("/correction-receipts/verify", json=fresh).json() == {"valid": True}


def test_key_id_selects_exact_key(two_points):
    # 用 old 密钥给声明 key_id=new 的回执签名：选键按 key_id 而非遍历全环，必须失败
    body = issue(two_points)
    body["receipt"] = sign_v2(KEY_ID_NEW, KEY_OLD, signed_fields(body))
    assert two_points.post("/correction-receipts/verify", json=body).json() == {"valid": False}

    # 声明 old 却用 new 密钥签名，同样失败
    body2 = issue(two_points)
    body2["key_id"] = KEY_ID_OLD
    body2["receipt"] = sign_v2(KEY_ID_OLD, KEY_NEW, signed_fields(body2))
    assert two_points.post("/correction-receipts/verify", json=body2).json() == {"valid": False}


def test_wrong_secret_for_key_fails(two_points, monkeypatch):
    # 键环里的密钥被换掉（key_id 不变）后，旧签名失效
    body = issue(two_points)
    monkeypatch.setenv(
        "CORRECTION_RECEIPT_KEYS",
        json.dumps({KEY_ID_OLD: KEY_OLD, KEY_ID_NEW: "rotated-new-secret"}),
    )
    assert two_points.post("/correction-receipts/verify", json=body).json() == {"valid": False}


# ---------- 旧版单密钥回退（保持一键启动） ----------

def test_legacy_fallback_issues_v2_with_legacy_key(legacy_two_points):
    body = issue(legacy_two_points)
    assert body["version"] == 2
    assert body["key_id"] == "legacy"
    assert body["receipt"] == sign_v2("legacy", V1_SECRET, signed_fields(body))
    assert legacy_two_points.post("/correction-receipts/verify",
                                  json=body).json() == {"valid": True}


def test_dev_default_secret_still_boots(tmp_path, monkeypatch):
    # KEYS / SECRET 全部缺省（裸 compose 场景）：不拒绝启动，用开发默认密钥
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "dev.db"))
    monkeypatch.delenv("CORRECTION_RECEIPT_KEYS", raising=False)
    monkeypatch.delenv("CORRECTION_RECEIPT_ACTIVE_KEY", raising=False)
    monkeypatch.delenv("CORRECTION_RECEIPT_SECRET", raising=False)
    from app.main import app

    with TestClient(app) as c:
        make_observation(c, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])
        make_observation(c, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])
        body = issue(c)
        assert body["version"] == 2 and body["key_id"] == "legacy"
        from app.keyrotation import DEV_DEFAULT_SECRET

        assert body["receipt"] == sign_v2("legacy", DEV_DEFAULT_SECRET, signed_fields(body))
        assert c.post("/correction-receipts/verify", json=body).json() == {"valid": True}


# ---------- v1 向后兼容 ----------

def test_verify_accepts_legacy_v1_receipt(two_points):
    body = issue(two_points)
    v1 = v1_receipt_from(body)
    assert two_points.post("/correction-receipts/verify", json=v1).json() == {"valid": True}


def test_verify_v1_receipt_with_extra_key_id_field(two_points):
    # 历史客户端可能回传多余字段；v1 校验忽略 key_id
    body = issue(two_points)
    v1 = v1_receipt_from(body, key_id_field=True)
    assert two_points.post("/correction-receipts/verify", json=v1).json() == {"valid": True}


def test_v1_receipt_survives_keyring_rotation(two_points, monkeypatch):
    body = issue(two_points)
    v1 = v1_receipt_from(body)

    # 键环轮换、移除键都不影响 v1：v1 始终走 CORRECTION_RECEIPT_SECRET 旧规则
    monkeypatch.setenv("CORRECTION_RECEIPT_ACTIVE_KEY", KEY_ID_OLD)
    assert two_points.post("/correction-receipts/verify", json=v1).json() == {"valid": True}
    monkeypatch.setenv("CORRECTION_RECEIPT_KEYS", json.dumps({KEY_ID_NEW: KEY_NEW}))
    assert two_points.post("/correction-receipts/verify", json=v1).json() == {"valid": True}


def test_v1_receipt_uses_legacy_secret_rule(two_points, monkeypatch):
    body = issue(two_points)
    v1 = v1_receipt_from(body)

    # 旧密钥被更换后，v1 回执失效；更换键环密钥不影响它
    monkeypatch.setenv("CORRECTION_RECEIPT_SECRET", "rotated-v1-secret")
    assert two_points.post("/correction-receipts/verify", json=v1).json() == {"valid": False}
    # 用新旧密钥重签后恢复
    assert two_points.post("/correction-receipts/verify",
                           json=v1_receipt_from(body, secret="rotated-v1-secret")
                           ).json() == {"valid": True}


@pytest.mark.parametrize("field,mutate", [
    ("version", lambda v: {**v, "version": 2}),
    ("request.timestamp", lambda v: {**v, "request": {**v["request"], "timestamp": 1501.0}}),
    ("bias", lambda v: {**v, "bias": v["bias"] + 0.01}),
    ("issued_at", lambda v: {**v, "issued_at": v["issued_at"] + 1}),
    ("receipt", lambda v: {**v, "receipt": "0" * 64}),
])
def test_verify_rejects_tampered_v1_fields(two_points, field, mutate):
    v1 = v1_receipt_from(issue(two_points))
    assert two_points.post("/correction-receipts/verify",
                           json=mutate(v1)).json() == {"valid": False}, field


# ---------- 启动时配置校验 ----------

def _chain(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _expect_startup_failure(monkeypatch, tmp_path, keys, active):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "bad.db"))
    monkeypatch.setenv("CORRECTION_RECEIPT_SECRET", V1_SECRET)
    if keys is None:
        monkeypatch.delenv("CORRECTION_RECEIPT_KEYS", raising=False)
    else:
        monkeypatch.setenv("CORRECTION_RECEIPT_KEYS", keys)
    if active is None:
        monkeypatch.delenv("CORRECTION_RECEIPT_ACTIVE_KEY", raising=False)
    else:
        monkeypatch.setenv("CORRECTION_RECEIPT_ACTIVE_KEY", active)
    from app.keyrotation import KeyringConfigError
    from app.main import app

    with pytest.raises(BaseException) as exc_info:
        with TestClient(app):
            pytest.fail("app must refuse to start with an invalid receipt key ring")
    assert any(isinstance(e, KeyringConfigError) for e in _chain(exc_info.value)), exc_info.value


def test_startup_rejects_empty_key_ring(monkeypatch, tmp_path):
    _expect_startup_failure(monkeypatch, tmp_path, "{}", KEY_ID_NEW)


def test_startup_rejects_unknown_active_key(monkeypatch, tmp_path):
    _expect_startup_failure(
        monkeypatch, tmp_path,
        json.dumps({KEY_ID_OLD: KEY_OLD, KEY_ID_NEW: KEY_NEW}),
        "2099-01",
    )


def test_startup_rejects_missing_active_key(monkeypatch, tmp_path):
    _expect_startup_failure(
        monkeypatch, tmp_path, json.dumps({KEY_ID_NEW: KEY_NEW}), None
    )


@pytest.mark.parametrize("bad_key_id", ["", "bad/id", "key with space", "kéy", "k" * 65])
def test_startup_rejects_illegal_key_id(monkeypatch, tmp_path, bad_key_id):
    _expect_startup_failure(
        monkeypatch, tmp_path, json.dumps({bad_key_id: "secret"}), bad_key_id or "x"
    )


def test_startup_rejects_illegal_active_key_id(monkeypatch, tmp_path):
    _expect_startup_failure(
        monkeypatch, tmp_path, json.dumps({"ok": "secret"}), "not-ok!"
    )


def test_startup_rejects_empty_secret(monkeypatch, tmp_path):
    _expect_startup_failure(
        monkeypatch, tmp_path, json.dumps({KEY_ID_NEW: ""}), KEY_ID_NEW
    )


def test_startup_rejects_non_string_secret(monkeypatch, tmp_path):
    _expect_startup_failure(
        monkeypatch, tmp_path, json.dumps({KEY_ID_NEW: 123}), KEY_ID_NEW
    )


def test_startup_rejects_malformed_keys_json(monkeypatch, tmp_path):
    _expect_startup_failure(monkeypatch, tmp_path, "{not json", KEY_ID_NEW)


def test_startup_rejects_non_object_keys(monkeypatch, tmp_path):
    _expect_startup_failure(monkeypatch, tmp_path, json.dumps([["k", "s"]]), "k")


# ---------- 结构/类型错误 → 422 ----------

def test_verify_missing_fields_return_422(two_points):
    body = issue(two_points)
    # key_id 缺失判 valid=false（见专门用例），不在这里
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
        {**body, "key_id": 123},
        {**body, "key_id": True},
        {**body, "key_id": ["2026-09"]},
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
