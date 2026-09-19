"""校正回执密钥轮换：键环解析、key_id 校验与活跃键选择。

配置（均为环境变量）：

- ``CORRECTION_RECEIPT_KEYS``：JSON 对象，key_id → 密钥，构成签名/验签键环。
  未设置时回退为单键开发环 ``{"dev": <CORRECTION_RECEIPT_SECRET 或开发默认值>}``，
  便于本地调试与旧版单密钥配置平滑升级；生产部署必须显式配置。
- ``CORRECTION_RECEIPT_ACTIVE_KEY``：当前签发键的 key_id，缺省为 ``dev``。

加载键环时（服务启动即加载一次，此后每次签发/校验重新读取）逐项校验，
以下配置一律抛出 KeyRingError（启动阶段即拒绝服务）：

- 空键环：``""``、纯空白或 ``{}``；
- 非 JSON 对象、密钥为空或非字符串；
- 非法 key_id（须为 1–64 字符：字母或数字开头，仅含字母、数字、``.``、``_``、``-``）；
- 活跃键不在键环中。

键环每次使用时重新读取环境变量（与 receipts._secret 同一惯例，便于测试与运行
时注入）。轮换只需更新环境变量并重启服务：旧键保留在环内则轮换前签发的回执
仍可验证；key_id 被移出键环后，携带该 key_id 的回执验签返回 valid=false。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

KEYS_ENV = "CORRECTION_RECEIPT_KEYS"
ACTIVE_KEY_ENV = "CORRECTION_RECEIPT_ACTIVE_KEY"
# 与 receipts.SECRET_ENV 一致：未配置键环时回退沿用旧版单密钥配置
LEGACY_SECRET_ENV = "CORRECTION_RECEIPT_SECRET"

DEV_DEFAULT_KEY_ID = "dev"
# 仅用于本地/Compose 开发的兜底密钥；生产必须经 CORRECTION_RECEIPT_KEYS 覆盖
DEV_DEFAULT_SECRET = "dev-only-insecure-correction-receipt-secret"

# key_id：1–64 字符，字母或数字开头，仅含字母、数字、.、_、-
KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class KeyRingError(Exception):
    """回执键环配置非法（空键环、未知活跃键、非法 key_id 等）。"""


@dataclass(frozen=True)
class KeyRing:
    """校验通过的键环：key_id → 密钥字节，以及当前签发键。"""

    secrets: dict[str, bytes]
    active_key_id: str

    @property
    def active_secret(self) -> bytes:
        return self.secrets[self.active_key_id]

    def secret_for(self, key_id: str) -> bytes | None:
        """按 key_id 精确取键；未知或已轮换出环返回 None。"""
        return self.secrets.get(key_id)


def _parse_keys(raw: str) -> dict[str, str]:
    if not raw.strip():
        raise KeyRingError(f"{KEYS_ENV} is an empty key ring: configure at least one key")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KeyRingError(f"{KEYS_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise KeyRingError(f"{KEYS_ENV} must be a JSON object mapping key_id to secret")
    if not data:
        raise KeyRingError(f"{KEYS_ENV} is an empty key ring: configure at least one key")
    secrets: dict[str, str] = {}
    for key_id, secret in data.items():
        if not KEY_ID_PATTERN.fullmatch(key_id):
            raise KeyRingError(
                f"invalid key_id {key_id!r} in {KEYS_ENV}: must be 1-64 chars, start with "
                "a letter or digit, and contain only letters, digits, '.', '_', '-'"
            )
        if not isinstance(secret, str) or not secret:
            raise KeyRingError(
                f"secret for key_id {key_id!r} in {KEYS_ENV} must be a non-empty string"
            )
        secrets[key_id] = secret
    return secrets


def load_key_ring() -> KeyRing:
    """读取环境变量构造键环并校验；配置非法时抛出 KeyRingError。"""
    raw = os.environ.get(KEYS_ENV)
    if raw is None:
        # 未配置键环：回退为单键 dev 环，密钥沿用旧版 CORRECTION_RECEIPT_SECRET（再兜底开发默认值）
        secrets = {DEV_DEFAULT_KEY_ID: os.environ.get(LEGACY_SECRET_ENV, DEV_DEFAULT_SECRET)}
    else:
        secrets = _parse_keys(raw)
    active = os.environ.get(ACTIVE_KEY_ENV, DEV_DEFAULT_KEY_ID)
    if active not in secrets:
        raise KeyRingError(
            f"unknown active key {active!r}: not in {KEYS_ENV} key ring "
            f"(available: {', '.join(sorted(secrets))})"
        )
    return KeyRing({key_id: secret.encode("utf-8") for key_id, secret in secrets.items()}, active)
