"""FastAPI 入口：仪器校准漂移补偿 API。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query

from . import receipts
from . import service
from .calibration import CalibrationError
from .db import init_db
from .models import (
    CorrectionIn,
    CorrectionOut,
    DriftBudgetDepletionOut,
    DriftExceedancesOut,
    DriftIntegralOut,
    DriftStabilityWindowOut,
    HistoricalCorrectionOut,
    IntervalOut,
    ObservationImpactPreviewIn,
    ObservationImpactPreviewOut,
    ObservationIn,
    ObservationOut,
    ObservationReviseIn,
    RevisionOut,
    Timestamp,
)
from .receipts import (
    CorrectionReceiptIn,
    CorrectionReceiptOut,
    CorrectionReceiptVerifyIn,
    CorrectionReceiptVerifyOut,
)
from .service import (
    DuplicateTimestampError,
    InsufficientNeighborsError,
    InvalidBudgetError,
    InvalidLimitError,
    InvalidSpreadError,
    InvalidWindowError,
    ObservationNotFoundError,
    OutOfCoverageError,
    RevisionConflictError,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Instrument Calibration Drift Compensation API",
    version="1.0.0",
    description=(
        "提交参考值与同一时点至少 3 次读数，服务标记异常读数并计算校准偏差；"
        "在相邻观测时点间线性插值偏差，对待校正读数施加补偿。"
    ),
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/observations", response_model=ObservationOut, status_code=201)
def post_observation(payload: ObservationIn) -> dict:
    """登记一次校准观测：标记异常读数、计算偏差、重算相邻区间。"""
    try:
        return service.create_observation(
            reference=payload.reference,
            tolerance=payload.tolerance,
            readings=payload.readings,
            ts=payload.timestamp,
        )
    except DuplicateTimestampError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CalibrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/observations", response_model=list[ObservationOut])
def get_observations() -> list[dict]:
    """按时点列出全部观测（含原始读数与异常标记）。"""
    return service.list_observations()


@app.put("/observations/{obs_id}", response_model=ObservationOut)
def put_observation(obs_id: int, payload: ObservationReviseIn) -> dict:
    """修订一次校准观测：重新判定异常、重算偏差并重建受影响相邻区间。

    请求体与登记观测一致，额外携带客户端持有的 revision；版本过期或目标时点
    已被其它观测占用均返回 409。观测 ID 不变，成功后 revision +1，
    修订前输入与偏差归档到修订历史。
    """
    try:
        return service.revise_observation(
            obs_id=obs_id,
            reference=payload.reference,
            tolerance=payload.tolerance,
            readings=payload.readings,
            ts=payload.timestamp,
            expected_revision=payload.revision,
        )
    except ObservationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RevisionConflictError, DuplicateTimestampError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CalibrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/observations/{obs_id}/revisions", response_model=list[RevisionOut])
def get_observation_revisions(obs_id: int) -> list[dict]:
    """按版本倒序返回观测的修订前快照（变更时间、旧输入与旧偏差）。"""
    try:
        return service.list_revisions(obs_id)
    except ObservationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/observation-impact-preview", response_model=ObservationImpactPreviewOut)
def post_observation_impact_preview(payload: ObservationImpactPreviewIn) -> dict:
    """预演插入一条观测对当前分段线性偏差曲线的影响；候选观测不落库。

    请求体与登记观测一致（沿用同一套异常判定计算候选偏差），额外携带正数
    impact_limit。仅在候选时点两侧都已有观测时预演：插入只会把左右最近观测之间
    的区间重新分段，受影响范围即两侧邻点之间；将插入后的虚拟分段线性曲线与当前
    曲线解析比较，返回受影响时间范围、最大绝对偏差变化及其最早发生时点，并按时间
    列出 |变化| > impact_limit 的连续区间（阈值交点精确求解，不做固定步长采样）。
    查询只读，不改写观测、修订历史或区间表；候选时点已占用或无法形成双侧邻点时
    返回 409。
    """
    try:
        return service.observation_impact_preview(
            reference=payload.reference,
            tolerance=payload.tolerance,
            readings=payload.readings,
            ts=payload.timestamp,
            impact_limit=payload.impact_limit,
        )
    except (DuplicateTimestampError, InsufficientNeighborsError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidLimitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CalibrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/intervals", response_model=list[IntervalOut])
def get_intervals() -> list[dict]:
    """列出当前相邻观测时点构成的插值区间。"""
    return service.list_intervals()


@app.post("/corrections", response_model=CorrectionOut)
def post_correction(payload: CorrectionIn) -> dict:
    """对指定时点的读数做漂移补偿；覆盖范围外拒绝。"""
    try:
        return service.correct(ts=payload.timestamp, reading=payload.reading)
    except OutOfCoverageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/correction-receipts", response_model=CorrectionReceiptOut)
def post_correction_receipt(payload: CorrectionReceiptIn) -> dict:
    """对漂移补偿结果签发版本化 HMAC 回执（复用 /corrections 的补偿逻辑）。

    返回 version=1、归一化请求（timestamp 统一为 Unix 秒）、bias、corrected、
    UTC Unix 毫秒整数 issued_at，以及对
    [version,timestamp,reading,bias,corrected,issued_at] 做 HMAC-SHA256
    （密钥取自 CORRECTION_RECEIPT_SECRET）得到的小写十六进制 receipt。
    覆盖范围外与 /corrections 一样返回 400。
    """
    try:
        return receipts.issue_receipt(ts=payload.timestamp, reading=payload.reading)
    except OutOfCoverageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/correction-receipts/verify", response_model=CorrectionReceiptVerifyOut)
def post_correction_receipt_verify(payload: CorrectionReceiptVerifyIn) -> dict:
    """校验完整回执：重算签名并以常量时间比较。

    任一签名字段（version/request.timestamp/request.reading/bias/corrected/
    issued_at/receipt）被改动返回 {"valid": false}；结构缺失或类型错误
    （如字符串冒充数值、issued_at 为小数）由 Pydantic strict 校验拒绝为 422。
    """
    return {"valid": receipts.verify_receipt(payload)}


@app.get("/historical-correction", response_model=HistoricalCorrectionOut)
def get_historical_correction(
    effective_at: Annotated[Timestamp, Query(description="目标观测时点：Unix 秒或 ISO 8601")],
    as_of: Annotated[Timestamp, Query(description="系统时点：重建该时刻可见的校准状态，Unix 秒或 ISO 8601")],
    reading: Annotated[float, Query(description="待校正的原始读数")],
) -> dict:
    """按 as_of 系统时点重建校准状态，对 effective_at 时点的读数做漂移补偿。

    依据 observations.created_at、当前版本与 revisions 快照重建 as_of 状态：
    尚未创建的观测不参与，修订自 changed_at 起生效（恰等于变更时刻用新版本）；
    随后按重建出的观测时点排序，沿用与 /corrections 相同的覆盖范围与线性插值
    规则。查询只读，不改写观测、修订历史或区间表；响应附带 as_of 及所用端点
    的观测 ID 与版本。
    """
    try:
        return service.historical_correct(effective_at=effective_at, as_of=as_of, reading=reading)
    except OutOfCoverageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/drift-integral", response_model=DriftIntegralOut)
def get_drift_integral(
    from_: Annotated[Timestamp, Query(alias="from", description="积分窗口下界：Unix 秒或 ISO 8601")],
    to: Annotated[Timestamp, Query(description="积分窗口上界：Unix 秒或 ISO 8601")],
) -> dict:
    """在观测覆盖范围内对当前分段线性 bias 做精确积分。

    返回有符号/绝对值累计漂移量（偏差·秒）及按插值区间裁剪的分段贡献；
    窗口边界可落在区间内部，段内 bias 过零时按解析交点拆分绝对值面积。
    """
    try:
        return service.drift_integral(frm=from_, to=to)
    except (InvalidWindowError, OutOfCoverageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/drift-exceedances", response_model=DriftExceedancesOut)
def get_drift_exceedances(
    from_: Annotated[Timestamp, Query(alias="from", description="分析窗口下界：Unix 秒或 ISO 8601")],
    to: Annotated[Timestamp, Query(description="分析窗口上界：Unix 秒或 ISO 8601")],
    limit: Annotated[float, Query(description="超限阈值（正数）：|bias| 严格大于该值才计为超限")],
) -> dict:
    """在观测覆盖范围内解析求解分段线性 bias 与 ±limit 的交点，返回超限连续区间。

    每段给出方向（high/low）、持续秒数与区间内峰值绝对偏差；跨观测点仍连续
    且同向的片段合并为一段，恰等于阈值的瞬时不计入超限。from ≥ to、limit ≤ 0
    或窗口边界越出覆盖范围均返回 400。
    """
    try:
        return service.drift_exceedances(frm=from_, to=to, limit=limit)
    except (InvalidWindowError, InvalidLimitError, OutOfCoverageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/drift-budget-depletion", response_model=DriftBudgetDepletionOut)
def get_drift_budget_depletion(
    from_: Annotated[Timestamp, Query(alias="from", description="累积起点：Unix 秒或 ISO 8601")],
    budget: Annotated[float, Query(description="漂移预算（正数）：可承受的 |bias|·秒 总量")],
) -> dict:
    """从 from 起沿当前分段线性 bias 累积 |bias|·秒，解析求预算首次耗尽的精确时点。

    逐插值区间推进：段内 bias 过零时先在解析零点拆分（|bias| 仅在拆分后的子段上
    才是线性函数），目标面积落入某子段时反解其累积二次式，全程无固定步长采样。
    返回 depleted_at（覆盖范围内未耗尽为 null）、截至耗尽点（未耗尽时为覆盖终点）
    的 consumed/remaining，以及按插值区间裁剪的分段贡献。from 越出覆盖范围或
    budget ≤ 0 返回 400。
    """
    try:
        return service.drift_budget_depletion(frm=from_, budget=budget)
    except (InvalidBudgetError, OutOfCoverageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/drift-stability-window", response_model=DriftStabilityWindowOut)
def get_drift_stability_window(
    from_: Annotated[Timestamp, Query(alias="from", description="分析窗口下界：Unix 秒或 ISO 8601")],
    to: Annotated[Timestamp, Query(description="分析窗口上界：Unix 秒或 ISO 8601")],
    max_spread: Annotated[float, Query(description="允许的最大波动幅度（正数）：窗口内 max(bias) − min(bias) 不超过该值")],
) -> dict:
    """在观测覆盖范围内解析求出 max(bias) − min(bias) ≤ max_spread 的最长连续子区间。

    最优窗口必是某临界带宽 [m, m+max_spread] 的连通分量（m 取遍折点取值及其下移
    max_spread 的值），分量边界由线性段与带宽边缘的解析交点精确定位，可落在插值
    段内部，不做固定步长采样；时长并列时返回起点最早者。响应含起止时刻、
    duration_seconds 与窗口内实际达到的 min_bias/max_bias。from ≥ to、
    max_spread ≤ 0 或窗口边界越出覆盖范围均返回 400。
    """
    try:
        return service.drift_stability_window(frm=from_, to=to, max_spread=max_spread)
    except (InvalidWindowError, InvalidSpreadError, OutOfCoverageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
