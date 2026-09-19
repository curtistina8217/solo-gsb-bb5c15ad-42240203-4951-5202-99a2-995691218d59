"""校正回执：复用现有漂移补偿，对版本化的校正结果签发 HMAC-SHA256 回执。

签名字段固定按 version、timestamp、reading、bias、corrected、issued_at 顺序
组成 JSON 数组，数值取 JSON 最短十进制（Python repr 即 IEEE 754 最短往返表示），
UTF-8 编码且无任何分隔空白后计算 HMAC。verify 只重算签名做常量时间比较，
不重新跑漂移补偿（历史校准状态可能已变；回执保证的是载荷本身未被篡改）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

from pydantic import BaseModel, ConfigDict, Field

from . import service
from .models import Timestamp

RECEIPT_VERSION = 1

# 仅用于本地/Compose 开发的兜底密钥；生产必须经 CORRECTION_RECEIPT_SECRET 覆盖
DEV_DEFAULT_SECRET = "dev-only-insecure-correction-receipt-secret"

SECRET_ENV = "CORRECTION_RECEIPT_SECRET"


def _secret() -> bytes:
    """每次签发/校验时读取环境变量，便于测试与运行时注入。"""
    return os.environ.get(SECRET_ENV, DEV_DEFAULT_SECRET).encode("utf-8")


def _canonical_payload(
    version: int, timestamp: float, reading: float, bias: float, corrected: float, issued_at: int
) -> bytes:
    """按固定顺序构造签名载荷：JSON 数组、数值最短十进制、UTF-8、无空白。

    separators=(",", ":") 去掉可选空白；int 直接序列化为整数字面量（issued_at
    是毫秒整数），float 用 repr 的最短往返十进制，等价于 JSON 数值最短表示。
    """
    return json.dumps(
        [version, timestamp, reading, bias, corrected, issued_at],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign(
    version: int, timestamp: float, reading: float, bias: float, corrected: float, issued_at: int
) -> str:
    """对签名字段计算 HMAC-SHA256，返回小写十六进制摘要。"""
    return hmac.new(
        _secret(),
        _canonical_payload(version, timestamp, reading, bias, corrected, issued_at),
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
    version: int = Field(description="回执格式版本，当前固定为 1")
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
    request: _SignedFields
    bias: float
    corrected: float
    issued_at: int
    receipt: str


class CorrectionReceiptVerifyOut(BaseModel):
    valid: bool


# ---------- 签发 / 校验 ----------

def issue_receipt(ts: float, reading: float) -> dict:
    """复用 service.correct 做漂移补偿，并对结果签发版本化回执。"""
    result = service.correct(ts=ts, reading=reading)
    issued_at = time.time_ns() // 1_000_000  # UTC Unix 毫秒整数
    receipt = sign(
        RECEIPT_VERSION,
        result["timestamp"],
        reading,
        result["bias"],
        result["corrected"],
        issued_at,
    )
    return {
        "version": RECEIPT_VERSION,
        "request": {"timestamp": result["timestamp"], "reading": reading},
        "bias": result["bias"],
        "corrected": result["corrected"],
        "issued_at": issued_at,
        "receipt": receipt,
    }


def verify_receipt(payload: CorrectionReceiptVerifyIn) -> bool:
    """重算签名并常量时间比较；版本不支持或任一字段被改动均为 False。"""
    if payload.version != RECEIPT_VERSION:
        return False
    expected = sign(
        payload.version,
        payload.request.timestamp,
        payload.request.reading,
        payload.bias,
        payload.corrected,
        payload.issued_at,
    )
    # digest 与输入都按 ASCII 比较；非 ASCII 的伪造回执直接判无效而不抛异常
    candidate = payload.receipt
    if not candidate.isascii():
        return False
    return hmac.compare_digest(expected, candidate)
