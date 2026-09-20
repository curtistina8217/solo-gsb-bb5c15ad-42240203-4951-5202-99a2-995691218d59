"""校正回执密钥环：key_id → HMAC 密钥映射与当前签发键。

配置（均为环境变量）：

- ``CORRECTION_RECEIPT_KEYS``：JSON 对象，key_id 到密钥字符串的映射，例如
  ``{"2026-09":"<hex>","2026-12":"<hex>"}``。
- ``CORRECTION_RECEIPT_ACTIVE_KEY``：当前新签发回执使用的 key_id，必须是
  密钥环中的成员。

key_id 合法规则：非空、长度 1–64、仅允许 ``[A-Za-z0-9_-]``（可安全出现在
JSON/日志/URL 中，避免定界符歧义）；密钥必须是非空字符串。

兼容回退：``CORRECTION_RECEIPT_KEYS`` 未设置（或为空串）时，沿用旧版
单密钥配置——密钥取 ``CORRECTION_RECEIPT_SECRET``（再缺省为仅用于本地/Compose
开发的固定默认值），合成只有一个 ``legacy`` 键的密钥环，保证 ``docker compose
up`` 一键启动与旧部署平滑升级；此时 ``CORRECTION_RECEIPT_ACTIVE_KEY`` 被忽略。

启动时由 :func:`validate_config` 严格校验，拒绝空键环、未知活跃键及非法
key_id（配置错误应让进程启动失败，而不是带病运行）。签发/校验每次都重新读取
环境变量，因此测试与运行时注入无需重启即可生效；密钥轮换的典型做法是把新旧
密钥同时保留在键环中、仅切换 ACTIVE，待旧回执到期后再移除旧键。
"""
from __future__ import annotations

import json
import os
import re
from typing import NamedTuple

# 仅用于本地/Compose 开发的兜底密钥；生产必须经 CORRECTION_RECEIPT_SECRET
# （旧配置）或 CORRECTION_RECEIPT_KEYS 覆盖
DEV_DEFAULT_SECRET = "dev-only-insecure-correction-receipt-secret"

KEYS_ENV = "CORRECTION_RECEIPT_KEYS"
ACTIVE_KEY_ENV = "CORRECTION_RECEIPT_ACTIVE_KEY"

# 兼容旧版单密钥配置；KEYS 未设置时以该值合成 legacy 键
SECRET_ENV = "CORRECTION_RECEIPT_SECRET"

# 回退密钥环使用的固定 key_id（与业务方自定义 key_id 命名空间隔开）
LEGACY_KEY_ID = "legacy"

KEY_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


class KeyringConfigError(ValueError):
    """密钥环配置非法：无法在启动时构造出可用的 key_id → 密钥映射。"""


class Keyring(NamedTuple):
    """不可变的已校验密钥环快照。"""

    keys: dict[str, bytes]
    active_key_id: str

    def active_key(self) -> bytes:
        return self.keys[self.active_key_id]

    def get(self, key_id: str) -> bytes | None:
        """按 key_id 精确取键；未知或已移除的键返回 None（验签据此判 valid=false）。"""
        return self.keys.get(key_id)


def _legacy_secret() -> bytes:
    """旧版单密钥规则：CORRECTION_RECEIPT_SECRET 缺省为开发固定值。"""
    return os.environ.get(SECRET_ENV, DEV_DEFAULT_SECRET).encode("utf-8")


def legacy_secret_bytes() -> bytes:
    """v1 回执的验签密钥：沿用旧版单密钥规则（与现行键环无关）。"""
    return _legacy_secret()


def _load_keyring(raw_keys: str | None, raw_active: str | None) -> Keyring:
    """解析并校验原始环境变量值。"""
    # 未设置或纯空白视为缺省，走旧版单密钥回退（Compose 注入空串同样如此）
    if raw_keys is None or not raw_keys.strip():
        return Keyring(keys={LEGACY_KEY_ID: _legacy_secret()}, active_key_id=LEGACY_KEY_ID)

    try:
        parsed = json.loads(raw_keys)
    except json.JSONDecodeError as exc:
        raise KeyringConfigError(
            f"{KEYS_ENV} must be a JSON object mapping key_id to secret string: {exc.msg}"
        ) from exc
    if not isinstance(parsed, dict):
        raise KeyringConfigError(f"{KEYS_ENV} must be a JSON object, got {type(parsed).__name__}")
    if not parsed:
        raise KeyringConfigError(f"{KEYS_ENV} must not be an empty key ring")

    keys: dict[str, bytes] = {}
    for key_id, secret in parsed.items():
        if not isinstance(key_id, str) or not KEY_ID_RE.fullmatch(key_id):
            raise KeyringConfigError(
                f"illegal key_id {key_id!r}: must be 1-64 chars of [A-Za-z0-9_-]"
            )
        if not isinstance(secret, str) or not secret:
            raise KeyringConfigError(f"secret for key_id {key_id!r} must be a non-empty string")
        keys[key_id] = secret.encode("utf-8")

    if raw_active is None or not raw_active.strip():
        raise KeyringConfigError(
            f"{ACTIVE_KEY_ENV} must name the signing key_id (one of: {', '.join(sorted(keys))})"
        )
    active = raw_active.strip()
    if not KEY_ID_RE.fullmatch(active):
        raise KeyringConfigError(
            f"illegal {ACTIVE_KEY_ENV} {active!r}: must be 1-64 chars of [A-Za-z0-9_-]"
        )
    if active not in keys:
        raise KeyringConfigError(
            f"{ACTIVE_KEY_ENV}={active!r} is not present in {KEYS_ENV} "
            f"(known key ids: {', '.join(sorted(keys))})"
        )
    return Keyring(keys=keys, active_key_id=active)


def load_keyring() -> Keyring:
    """每次签发/校验时读取环境变量构造密钥环，便于运行时注入与轮换。"""
    return _load_keyring(os.environ.get(KEYS_ENV), os.environ.get(ACTIVE_KEY_ENV))


def validate_config() -> Keyring:
    """启动时校验密钥环配置；非法时抛 KeyringConfigError（lifespan 据此拒绝启动）。"""
    return load_keyring()
