"""校正回执：复用现有漂移补偿，对版本化的校正结果签发 HMAC-SHA256 回执。

- v2（当前签发版本）：签名字段固定按 version、key_id、timestamp、reading、
  bias、corrected、issued_at 顺序组成 JSON 数组，数值取 JSON 最短十进制
  （Python repr 即 IEEE 754 最短往返表示），UTF-8 编码且无任何分隔空白后，
  以 key_id 对应的键环密钥计算 HMAC。响应携带 key_id；验签严格按回执中的
  key_id 精确选键，未知或已移除的键直接判 valid=false。
- v1（旧回执，仅继续接受）：载荷按 version、timestamp、reading、bias、
  corrected、issued_at 顺序组成，密钥沿用旧规则（CORRECTION_RECEIPT_SECRET
  或开发默认值），与 key_id 无关。

verify 只重算签名做常量时间比较，不重新跑漂移补偿（历史校准状态可能已变；
回执保证的是载荷本身未被篡改）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from pydantic import BaseModel, ConfigDict, Field

from . import keyrotation, service
from .models import Timestamp

# 当前新签发回执的格式版本；旧版本仍可被 verify 接受
RECEIPT_VERSION = 2
# 仍接受验签的最早回执版本
SUPPORTED_VERSIONS = (1, 2)


def _canonical_payload(
    version: int,
    timestamp: float,
    reading: float,
    bias: float,
    corrected: float,
    issued_at: int,
    key_id: str | None = None,
) -> bytes:
    """按固定顺序构造签名载荷：JSON 数组、数值最短十进制、UTF-8、无空白。

    separators=(",", ":") 去掉可选空白；int 直接序列化为整数字面量（version、
    issued_at 是整数），float 用 repr 的最短往返十进制，等价于 JSON 数值最短
    表示。v2 在 version 之后追加 key_id 字符串参与签名；v1 不含 key_id。
    """
    fields: list[object]
    if version >= 2:
        fields = [version, key_id, timestamp, reading, bias, corrected, issued_at]
    else:
        fields = [version, timestamp, reading, bias, corrected, issued_at]
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _hmac(secret: bytes, payload: bytes) -> str:
    """对签名载荷计算 HMAC-SHA256，返回小写十六进制摘要。"""
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


# ---------- 请求/响应模型 ----------

class CorrectionReceiptIn(BaseModel):
    timestamp: Timestamp = Field(description="待校正读数的时点：Unix 秒或 ISO 8601")
    reading: float = Field(description="待校正的原始读数")


class CorrectionRequestOut(BaseModel):
    """归一化后的请求：timestamp 统一为 Unix 秒（float），reading 为数值。"""

    timestamp: float
    reading: float


class CorrectionReceiptOut(BaseModel):
    version: int = Field(description="回执格式版本，当前签发为 2")
    key_id: str = Field(description="签发所用键环密钥的 key_id（验签时按它精确选键）")
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
    # v2 必需；v1 回执没有该字段，故缺省为 None（携带时不参与 v1 校验）
    key_id: str | None = None
    request: _SignedFields
    bias: float
    corrected: float
    issued_at: int
    receipt: str


class CorrectionReceiptVerifyOut(BaseModel):
    valid: bool


# ---------- 签发 / 校验 ----------

def issue_receipt(ts: float, reading: float) -> dict:
    """复用 service.correct 做漂移补偿，并以当前活跃键签发 v2 回执。"""
    result = service.correct(ts=ts, reading=reading)
    issued_at = time.time_ns() // 1_000_000  # UTC Unix 毫秒整数
    keyring = keyrotation.load_keyring()
    key_id = keyring.active_key_id
    receipt = _hmac(
        keyring.active_key(),
        _canonical_payload(
            RECEIPT_VERSION,
            result["timestamp"],
            reading,
            result["bias"],
            result["corrected"],
            issued_at,
            key_id,
        ),
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
    """重算签名并常量时间比较。

    - v2：按回执 key_id 精确选键；key_id 缺失、未知或已移除（含轮换后删除的
      旧键）一律 False。
    - v1：沿用旧密钥规则（CORRECTION_RECEIPT_SECRET/开发默认值），与 key_id
      及现行键环无关。
    其它版本或任一字段被改动均为 False。
    """
    candidate = payload.receipt
    # digest 与输入都按 ASCII 比较；非 ASCII 的伪造回执直接判无效而不抛异常
    if not candidate.isascii():
        return False

    if payload.version == 2:
        key_id = payload.key_id
        if key_id is None:
            return False
        keyring = keyrotation.load_keyring()
        secret = keyring.get(key_id)
        if secret is None:
            return False
    elif payload.version == 1:
        secret = keyrotation.legacy_secret_bytes()
        key_id = None
    else:
        return False

    expected = _hmac(
        secret,
        _canonical_payload(
            payload.version,
            payload.request.timestamp,
            payload.request.reading,
            payload.bias,
            payload.corrected,
            payload.issued_at,
            key_id,
        ),
    )
    return hmac.compare_digest(expected, candidate)
