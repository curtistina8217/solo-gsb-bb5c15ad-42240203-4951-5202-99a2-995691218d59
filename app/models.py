"""Pydantic 请求/响应模型。时间戳对外同时提供 Unix 秒与 ISO 8601 两种形式。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field


def epoch_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def parse_timestamp(value: Any) -> float:
    """接受 Unix 秒（数字或数字字符串）或 ISO 8601 字符串，统一为 Unix 秒。"""
    if isinstance(value, bool):
        raise ValueError("invalid timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                raise ValueError(f"unparseable timestamp: {value!r}") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise ValueError(f"unsupported timestamp type: {type(value).__name__}")


Timestamp = Annotated[float, BeforeValidator(parse_timestamp)]


# ---------- 请求 ----------

class ObservationIn(BaseModel):
    reference: float = Field(description="参考值（真值）")
    tolerance: float = Field(ge=0, description="容差：读数偏离组中位数超过该值即判为异常")
    readings: list[float] = Field(min_length=3, description="同一时点的读数，至少 3 次")
    timestamp: Timestamp = Field(
        default_factory=lambda: datetime.now(timezone.utc).timestamp(),
        description="观测时点：Unix 秒或 ISO 8601，缺省为当前时间",
    )


class CorrectionIn(BaseModel):
    timestamp: Timestamp = Field(description="待校正读数的时点：Unix 秒或 ISO 8601")
    reading: float = Field(description="待校正的原始读数")


class ObservationReviseIn(ObservationIn):
    revision: int = Field(ge=1, description="客户端持有的当前版本号；与服务端不一致时拒绝修订（409）")


class ObservationImpactPreviewIn(ObservationIn):
    impact_limit: float = Field(description="影响阈值（正数）：|偏差变化| 严格大于该值才计入影响区间")


# ---------- 响应 ----------

class ReadingOut(BaseModel):
    value: float
    is_outlier: bool


class ObservationOut(BaseModel):
    id: int
    revision: int
    timestamp: float
    timestamp_iso: str
    reference: float
    tolerance: float
    group_median: float
    bias: float
    readings: list[ReadingOut]
    outliers: list[float]


class RevisionOut(BaseModel):
    revision: int = Field(description="该快照对应的版本（修订前版本）")
    changed_at: float
    changed_at_iso: str
    old_timestamp: float
    old_timestamp_iso: str
    old_reference: float
    old_tolerance: float
    old_bias: float
    old_readings: list[float] = Field(description="修订前原始读数（保持提交顺序）")


class IntervalOut(BaseModel):
    t_start: float
    t_end: float
    start_iso: str
    end_iso: str
    bias_start: float
    bias_end: float


class EndpointOut(BaseModel):
    observation_id: int
    timestamp: float
    timestamp_iso: str
    bias: float
    outliers: list[float]


class CorrectionOut(BaseModel):
    timestamp: float
    timestamp_iso: str
    reading: float
    bias: float
    corrected: float
    interval: IntervalOut | None = Field(description="所用插值区间；恰落在观测点上时为 null")
    endpoints: list[EndpointOut] = Field(description="区间端点观测及其异常读数明细")


class HistoricalEndpointOut(BaseModel):
    observation_id: int
    revision: int = Field(description="as_of 系统时点该观测所处的版本号")
    timestamp: float = Field(description="as_of 状态下重建出的观测时点（Unix 秒）")
    timestamp_iso: str
    bias: float = Field(description="as_of 状态下该观测的校准偏差")


class HistoricalCorrectionOut(BaseModel):
    effective_at: float = Field(description="目标观测时点（Unix 秒）")
    effective_at_iso: str
    as_of: float = Field(description="系统时点（Unix 秒）：校准状态按该时刻重建")
    as_of_iso: str
    reading: float
    bias: float
    corrected: float
    interval: IntervalOut | None = Field(description="重建状态下所用的插值区间；恰落在观测点上时为 null")
    endpoints: list[HistoricalEndpointOut] = Field(description="所用端点的观测 ID 与 as_of 时点版本")


class DriftContributionOut(BaseModel):
    t_start: float = Field(description="裁剪后子段起点（Unix 秒）")
    t_end: float = Field(description="裁剪后子段终点（Unix 秒）")
    start_iso: str
    end_iso: str
    bias_start: float = Field(description="子段起点处的插值偏差")
    bias_end: float = Field(description="子段终点处的插值偏差")
    signed_bias_seconds: float = Field(description="子段有符号面积（偏差·秒）")
    absolute_bias_seconds: float = Field(description="子段绝对值面积（偏差·秒），段内过零按解析交点拆分")


class DriftIntegralOut(BaseModel):
    from_: float = Field(alias="from", description="积分窗口下界（Unix 秒）")
    from_iso: str
    to: float = Field(description="积分窗口上界（Unix 秒）")
    to_iso: str
    signed_bias_seconds: float = Field(description="窗口内偏差的有符号积分（偏差·秒）")
    absolute_bias_seconds: float = Field(description="窗口内偏差绝对值的积分（偏差·秒）")
    contributions: list[DriftContributionOut] = Field(
        description="按插值区间裁剪后的分段贡献，按时间排序；各分段面积之和等于汇总值"
    )


class ExceedanceOut(BaseModel):
    t_start: float = Field(description="超限区间起点（Unix 秒）：解析交点或窗口边界")
    t_end: float = Field(description="超限区间终点（Unix 秒）")
    start_iso: str
    end_iso: str
    direction: str = Field(description="超限方向：high = bias > limit；low = bias < −limit")
    duration_seconds: float = Field(description="超限持续秒数（t_end − t_start）")
    peak_abs_deviation: float = Field(description="区间内 |bias| 的峰值")


class DriftExceedancesOut(BaseModel):
    from_: float = Field(alias="from", description="分析窗口下界（Unix 秒）")
    from_iso: str
    to: float = Field(description="分析窗口上界（Unix 秒）")
    to_iso: str
    limit: float = Field(description="超限阈值（正数）：|bias| 严格大于该值才计为超限")
    exceedances: list[ExceedanceOut] = Field(
        description="|bias| > limit 的连续区间，按时间排序；跨观测点仍连续且同向的片段已合并"
    )


class BudgetContributionOut(BaseModel):
    t_start: float = Field(description="裁剪后子段起点（Unix 秒）")
    t_end: float = Field(description="裁剪后子段终点（Unix 秒）：耗尽所在段截止于 depleted_at")
    start_iso: str
    end_iso: str
    bias_start: float = Field(description="子段起点处的（有符号）插值偏差")
    bias_end: float = Field(description="子段终点处的（有符号）插值偏差")
    absolute_bias_seconds: float = Field(description="子段消耗的预算（|bias|·秒）")


class DriftBudgetDepletionOut(BaseModel):
    from_: float = Field(alias="from", description="累积起点（Unix 秒）")
    from_iso: str
    budget: float = Field(description="漂移预算（正数）：可承受的 |bias|·秒 总量")
    depleted_at: float | None = Field(
        description="预算首次耗尽的精确时点（Unix 秒）；覆盖范围内未耗尽为 null"
    )
    depleted_at_iso: str | None
    consumed: float = Field(description="截至耗尽点（未耗尽时为覆盖终点）已消耗的预算（|bias|·秒）")
    remaining: float = Field(description="截至耗尽点（未耗尽时为覆盖终点）的剩余预算")
    coverage_end: float = Field(description="覆盖范围上界（Unix 秒）：累积推进的终点")
    coverage_end_iso: str
    contributions: list[BudgetContributionOut] = Field(
        description="按插值区间裁剪到 [from, 耗尽点或覆盖终点] 的分段消耗，按时间排序；"
        "各段 absolute_bias_seconds 之和等于 consumed"
    )


class DriftStabilityWindowOut(BaseModel):
    from_: float = Field(alias="from", description="分析窗口下界（Unix 秒）")
    from_iso: str
    to: float = Field(description="分析窗口上界（Unix 秒）")
    to_iso: str
    max_spread: float = Field(description="允许的最大波动幅度（正数）：窗口内 max(bias) − min(bias) ≤ max_spread")
    t_start: float = Field(description="最长稳定子区间起点（Unix 秒）：解析交点或窗口边界")
    t_end: float = Field(description="最长稳定子区间终点（Unix 秒）：解析交点或窗口边界")
    start_iso: str
    end_iso: str
    duration_seconds: float = Field(description="稳定持续秒数（t_end − t_start）")
    min_bias: float = Field(description="子区间内 bias 实际达到的最小值")
    max_bias: float = Field(description="子区间内 bias 实际达到的最大值")


class ImpactIntervalOut(BaseModel):
    t_start: float = Field(description="影响区间起点（Unix 秒）：|偏差变化| 与 impact_limit 的解析交点")
    t_end: float = Field(description="影响区间终点（Unix 秒）")
    start_iso: str
    end_iso: str
    duration_seconds: float = Field(description="超限持续秒数（t_end − t_start）")


class ObservationImpactPreviewOut(BaseModel):
    timestamp: float = Field(description="候选观测时点（Unix 秒）")
    timestamp_iso: str
    impact_limit: float = Field(description="影响阈值（正数）：|偏差变化| 严格大于该值才计入影响区间")
    candidate_bias: float = Field(description="按现有异常判定算出的候选观测偏差（不落库）")
    current_bias: float = Field(description="当前分段线性曲线在候选时点的插值偏差")
    affected_from: float = Field(description="受影响时间范围下界（Unix 秒）：候选点左侧最近观测时点")
    affected_from_iso: str
    affected_to: float = Field(description="受影响时间范围上界（Unix 秒）：候选点右侧最近观测时点")
    affected_to_iso: str
    max_abs_change: float = Field(description="受影响范围内 |新曲线 − 当前曲线| 的最大值")
    max_change_at: float = Field(description="最大绝对偏差变化的最早发生时点（Unix 秒）")
    max_change_at_iso: str
    impact_intervals: list[ImpactIntervalOut] = Field(
        description="|偏差变化| > impact_limit 的连续区间，按时间排序；阈值交点解析求解"
    )
