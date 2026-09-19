"""键环加载与启动校验测试：空键环、未知活跃键、非法 key_id 等配置错误拒绝启动。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.receipt_keys import (
    ACTIVE_KEY_ENV,
    DEV_DEFAULT_KEY_ID,
    DEV_DEFAULT_SECRET,
    KEYS_ENV,
    LEGACY_SECRET_ENV,
    KeyRingError,
    load_key_ring,
)


@pytest.fixture()
def clean_env(monkeypatch):
    for var in (KEYS_ENV, ACTIVE_KEY_ENV, LEGACY_SECRET_ENV):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# ---------- load_key_ring 单元测试 ----------

def test_unset_keys_falls_back_to_legacy_secret(clean_env):
    clean_env.setenv(LEGACY_SECRET_ENV, "legacy-secret")
    ring = load_key_ring()
    assert ring.active_key_id == DEV_DEFAULT_KEY_ID
    assert ring.active_secret == b"legacy-secret"
    assert ring.secret_for(DEV_DEFAULT_KEY_ID) == b"legacy-secret"
    assert ring.secret_for("other") is None


def test_unset_keys_and_secret_falls_back_to_dev_default(clean_env):
    ring = load_key_ring()
    assert ring.active_key_id == DEV_DEFAULT_KEY_ID
    assert ring.active_secret == DEV_DEFAULT_SECRET.encode("utf-8")


def test_configured_ring_and_active_key(clean_env):
    clean_env.setenv(KEYS_ENV, json.dumps({"k1": "s1", "k2": "s2"}))
    clean_env.setenv(ACTIVE_KEY_ENV, "k2")
    ring = load_key_ring()
    assert ring.active_key_id == "k2"
    assert ring.active_secret == b"s2"
    assert ring.secret_for("k1") == b"s1"
    assert ring.secret_for("unknown") is None


def test_active_key_defaults_to_dev(clean_env):
    clean_env.setenv(KEYS_ENV, json.dumps({"dev": "s1", "k2": "s2"}))
    assert load_key_ring().active_key_id == "dev"


@pytest.mark.parametrize("raw", [
    "",                 # 空键环
    "   ",              # 纯空白
    "{}",               # 空键环（JSON 空对象）
    "not-json",         # 非 JSON
    "[1, 2]",           # 非 JSON 对象
    '"just-a-string"',
    "123",
])
def test_empty_or_malformed_key_ring_rejected(clean_env, raw):
    clean_env.setenv(KEYS_ENV, raw)
    with pytest.raises(KeyRingError):
        load_key_ring()


@pytest.mark.parametrize("raw", [
    json.dumps({"k": ""}),    # 空密钥
    json.dumps({"k": 123}),   # 非字符串密钥
    json.dumps({"k": None}),
])
def test_invalid_secret_rejected(clean_env, raw):
    clean_env.setenv(KEYS_ENV, raw)
    with pytest.raises(KeyRingError):
        load_key_ring()


@pytest.mark.parametrize("key_id", [
    "", "-x", ".x", "_x", "key id", "key/id", "key:id", "键", "a" * 65,
])
def test_invalid_key_id_rejected(clean_env, key_id):
    clean_env.setenv(KEYS_ENV, json.dumps({key_id: "secret"}))
    with pytest.raises(KeyRingError, match="invalid key_id"):
        load_key_ring()


@pytest.mark.parametrize("key_id", [
    "dev", "2026-09", "key_a.1", "A", "a" * 64, "key-2026.09_01",
])
def test_valid_key_id_accepted(clean_env, key_id):
    clean_env.setenv(KEYS_ENV, json.dumps({key_id: "secret"}))
    clean_env.setenv(ACTIVE_KEY_ENV, key_id)
    assert load_key_ring().active_key_id == key_id


def test_unknown_active_key_rejected(clean_env):
    clean_env.setenv(KEYS_ENV, json.dumps({"k1": "s1"}))
    clean_env.setenv(ACTIVE_KEY_ENV, "ghost")
    with pytest.raises(KeyRingError, match="unknown active key"):
        load_key_ring()


def test_active_key_without_key_ring_rejected(clean_env):
    # 未配置键环时只有 dev 环：指定其它活跃键同样属于未知活跃键
    clean_env.setenv(ACTIVE_KEY_ENV, "prod")
    with pytest.raises(KeyRingError, match="unknown active key"):
        load_key_ring()


# ---------- 服务启动校验（lifespan 即加载键环，配置非法拒绝启动） ----------

@pytest.mark.parametrize("keys_env,active", [
    ("", None),                        # 空键环
    ("{}", None),                      # 空键环（JSON 空对象）
    ('{"bad key": "secret"}', None),   # 非法 key_id
    ('{"k1": "secret"}', "ghost"),     # 未知活跃键
])
def test_startup_rejects_invalid_key_ring(tmp_path, monkeypatch, keys_env, active):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv(KEYS_ENV, keys_env)
    if active is None:
        monkeypatch.delenv(ACTIVE_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(ACTIVE_KEY_ENV, active)
    from app.main import app  # 环境变量设置后再导入

    with pytest.raises(KeyRingError):
        with TestClient(app):
            pass


def test_startup_accepts_valid_key_ring(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv(KEYS_ENV, json.dumps({"2026-09": "secret-1", "2026-08": "secret-0"}))
    monkeypatch.setenv(ACTIVE_KEY_ENV, "2026-09")
    from app.main import app  # 环境变量设置后再导入

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
