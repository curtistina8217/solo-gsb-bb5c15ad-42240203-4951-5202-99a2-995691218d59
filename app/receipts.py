"""校正回执：复用现有漂移补偿，对版本化的校正结果签发 HMAC-SHA256 回执。

version=2（当前）：回执携带 key_id，签名载荷按
[version, key_id, timestamp, reading, bias, corrected, issued_at] 顺序组成 JSON
数组，以 CORRECTION_RECEIPT_KEYS 键环中该 key_id 对应的密钥计算 HMAC；验签按
key_id 精确选键，未知或已轮换出环的键一律判无效（旧键保留在环内时，轮换前
签发的回执仍可验证）。

version=1（历史兼容）：载荷为 [version, timestamp, reading, bias, corrected,
issued_at]（不含 key_id），密钥沿用 CORRECTION_RECEIPT_SECRET 的旧规则，
与升级前逐字节一致。

数值取 JSON 最短十进制（Python repr 即 IEEE 754 最短往返表示），UTF-8 编码且
无任何分隔空白。verify 只重算签名做常量时间比较，不重新跑漂移补偿（历史校准
状态可能已变；回执保证的是载荷本身未被篡改）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import receipt_keys, service
from .models import Timestamp

RECEIPT_VERSION = 2
LEGACY_RECEIPT_VERSION = 1

# 仅用于本地/Compose 开发的兜底密钥；生产必须经 CORRECTION_RECEIPT_SECRET 覆盖
DEV_DEFAULT_SECRET = "dev-only-insecure-correction-receipt-secret"

SECRET_ENV = "CORRECTION_RECEIPT_SECRET"


def _secret() -> bytes:
    """version=1 历史回执的密钥：每次校验时读取环境变量，便于测试与运行时注入。"""
    return os.environ.get(SECRET_ENV, DEV_DEFAULT_SECRET).encode("utf-8")


def _canonical_payload(
    version: int,
    key_id: str,
    timestamp: float,
    reading: float,
    bias: float,
    corrected: float,
    issued_at: int,
) -> bytes:
    """version=2 签名载荷：JSON 数组、数值最短十进制、UTF-8、无空白。

    key_id 紧随 version 纳入签名，回执无法被张冠李戴到别的密钥；separators=(",", ":")
    去掉可选空白；int 直接序列化为整数字面量（issued_at 是毫秒整数），float 用
    repr 的最短往返十进制，等价于 JSON 数值最短表示。
    """
    return json.dumps(
        [version, key_id, timestamp, reading, bias, corrected, issued_at],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _legacy_canonical_payload(
    version: int, timestamp: float, reading: float, bias: float, corrected: float, issued_at: int
) -> bytes:
    """version=1 签名载荷：与升级前逐字节一致（不含 key_id）。"""
    return json.dumps(
        [version, timestamp, reading, bias, corrected, issued_at],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign(
    version: int,
    key_id: str,
    secret: bytes,
    timestamp: float,
    reading: float,
    bias: float,
    corrected: float,
    issued_at: int,
) -> str:
    """version=2：以键环中指定密钥计算 HMAC-SHA256，返回小写十六进制摘要。"""
    return hmac.new(
        secret,
        _canonical_payload(version, key_id, timestamp, reading, bias, corrected, issued_at),
        hashlib.sha256,
    ).hexdigest()


def sign_legacy(
    version: int, timestamp: float, reading: float, bias: float, corrected: float, issued_at: int
) -> str:
    """version=1：沿用 CORRECTION_RECEIPT_SECRET 的旧签名规则。"""
    return hmac.new(
        _secret(),
        _legacy_canonical_payload(version, timestamp, reading, bias, corrected, issued_at),
        hashlib.sha256,
    ).hexdigest()


# ---------- 请求/响应模型 ----------

class CorrectionReceiptIn(BaseModel):
    timestamp: Timestamp = Field(description="待校正读数的时点：Unix 秒或 ISO 8601")
    reading: float = Field(description="待校正的原始读数")


class CorrectionRequestOut(BaseModel):
    """归一化后的请求：timestamp 统一为 Unix 秒（float），reading 为数值。"""

    timestamp: float
    reading: float


class CorrectionReceiptOut(BaseModel):
    version: int = Field(description="回执格式版本，当前固定为 2")
    key_id: str = Field(description="签发密钥 ID：CORRECTION_RECEIPT_KEYS 键环中的键名")
    request: CorrectionRequestOut = Field(description="归一化请求（timestamp 为 Unix 秒）")
    bias: float = Field(description="复用 /corrections 漂移补偿得到的插值偏差")
    corrected: float = Field(description="校正读数 = 原始读数 + 偏差")
    issued_at: int = Field(description="签发时间：UTC Unix 毫秒整数")
    receipt: str = Field(description="HMAC-SHA256 签名的小写十六进制摘要")


class _SignedFields(BaseModel):
    """verify 请求中的签名字段；strict 模式拒绝字符串/布尔冒充数值等类型错误。"""

    model_config = ConfigDict(strict=True)

    timestamp: float
    reading: float


class CorrectionReceiptVerifyIn(BaseModel):
    model_config = ConfigDict(strict=True)

    version: int
    # version=2 必带（见模型校验器）；version=1 回执没有该字段，出现时被忽略
    key_id: str | None = None
    request: _SignedFields
    bias: float
    corrected: float
    issued_at: int
    receipt: str

    @model_validator(mode="after")
    def _require_key_id_for_v2(self):
        if self.version == RECEIPT_VERSION and self.key_id is None:
            raise ValueError("key_id is required for version=2 receipts")
        return self


class CorrectionReceiptVerifyOut(BaseModel):
    valid: bool


# ---------- 签发 / 校验 ----------

def issue_receipt(ts: float, reading: float) -> dict:
    """复用 service.correct 做漂移补偿，并以键环当前活跃键签发 version=2 回执。"""
    result = service.correct(ts=ts, reading=reading)
    issued_at = time.time_ns() // 1_000_000  # UTC Unix 毫秒整数
    ring = receipt_keys.load_key_ring()
    key_id = ring.active_key_id
    receipt = sign(
        RECEIPT_VERSION,
        key_id,
        ring.active_secret,
        result["timestamp"],
        reading,
        result["bias"],
        result["corrected"],
        issued_at,
    )
    return {
        "version": RECEIPT_VERSION,
        "key_id": key_id,
        "request": {"timestamp": result["timestamp"], "reading": reading},
        "bias": result["bias"],
        "corrected": result["corrected"],
        "issued_at": issued_at,
        "receipt": receipt,
    }


def verify_receipt(payload: CorrectionReceiptVerifyIn) -> bool:
    """重算签名并常量时间比较；版本不支持、key_id 不在键环或任一字段被改动均为 False。"""
    if payload.version == LEGACY_RECEIPT_VERSION:
        expected = sign_legacy(
            payload.version,
            payload.request.timestamp,
            payload.request.reading,
            payload.bias,
            payload.corrected,
            payload.issued_at,
        )
    elif payload.version == RECEIPT_VERSION:
        # version=2 的 key_id 由请求模型保证非空；未知或已轮换出环的键判无效
        secret = receipt_keys.load_key_ring().secret_for(payload.key_id)
        if secret is None:
            return False
        expected = sign(
            payload.version,
            payload.key_id,
            secret,
            payload.request.timestamp,
            payload.request.reading,
            payload.bias,
            payload.corrected,
            payload.issued_at,
        )
    else:
        return False
    # digest 与输入都按 ASCII 比较；非 ASCII 的伪造回执直接判无效而不抛异常
    candidate = payload.receipt
    if not candidate.isascii():
        return False
    return hmac.compare_digest(expected, candidate)
