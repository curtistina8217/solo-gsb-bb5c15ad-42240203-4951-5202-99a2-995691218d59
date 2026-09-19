"""纯计算逻辑：异常判定、校准偏差、线性插值、分段线性积分、超限区间求解、预算累积反解与稳定窗口求解。不触碰数据库，便于单测。"""
from __future__ import annotations

import math
from statistics import median


class CalibrationError(ValueError):
    """输入读数无法计算有效校准偏差时抛出。"""


def split_outliers(readings: list[float], tolerance: float) -> tuple[float, list[float], list[float]]:
    """按“偏离组中位数超过容差即异常”拆分读数。

    返回 (组中位数, 有效读数, 异常读数)。
    """
    group_median = median(readings)
    inliers = [r for r in readings if abs(r - group_median) <= tolerance]
    outliers = [r for r in readings if abs(r - group_median) > tolerance]
    return group_median, inliers, outliers


def compute_bias(reference: float, tolerance: float, readings: list[float]) -> tuple[float, float, list[float], list[float]]:
    """校准偏差 = 参考值 - 有效读数（剔除异常后）的中位数。

    返回 (bias, 组中位数, 有效读数, 异常读数)。
    """
    if len(readings) < 3:
        raise CalibrationError(f"need at least 3 readings, got {len(readings)}")
    group_median, inliers, outliers = split_outliers(readings, tolerance)
    if not inliers:
        raise CalibrationError(
            "all readings deviate from the group median by more than the tolerance; "
            "cannot compute a calibration bias"
        )
    bias = reference - median(inliers)
    return bias, group_median, inliers, outliers


def interpolate(t: float, t0: float, b0: float, t1: float, b1: float) -> float:
    """在相邻时点 (t0, b0) 与 (t1, b1) 之间对 t 做线性插值。"""
    if t1 == t0:
        return b0
    return b0 + (b1 - b0) * (t - t0) / (t1 - t0)


def integrate_segment(t0: float, b0: float, t1: float, b1: float) -> tuple[float, float]:
    """对线性段 (t0, b0)-(t1, b1) 下的面积做解析积分，返回 (signed, absolute)。

    signed 为梯形面积 (b0+b1)/2·Δt。absolute 为 |bias| 的面积：端点严格异号时
    段内恰有一个零点 t* = t0 − b0·Δt/(b1−b0)，在 t* 处拆成两个三角形求和
    （面积为 (b0² + b1²)·Δt / (2·|b1−b0|)）；否则 |bias| 不变号，面积即 |signed|。
    全程解析解，不做固定步长采样。
    """
    dt = t1 - t0
    if dt <= 0:
        return 0.0, 0.0
    signed = (b0 + b1) * 0.5 * dt
    if b0 * b1 < 0:  # 严格异号：段内恰有一个零点，按解析交点拆分
        absolute = (b0 * b0 + b1 * b1) * 0.5 * dt / abs(b1 - b0)
    else:  # 同号或端点触零：不变号
        absolute = abs(signed)
    return signed, absolute


def abs_subsegments(t0: float, b0: float, t1: float, b1: float) -> list[tuple[float, float, float, float]]:
    """把线性 bias 段在解析过零点拆分，返回 |bias| 线性的子段 [(s, |b_s|, e, |b_e|)]。

    端点严格异号时段内恰有一个零点 t* = t0 − b0·Δt/(b1−b0)，在 t* 处一分为二；
    否则 |bias| 全程线性，原段直接返回。每个子段上 |bias| 都是线性函数，
    其累积面积是时长的二次函数，可解析反解（见 time_to_accumulate）。
    """
    if b0 * b1 < 0:  # 严格异号：段内恰有一个零点
        t_cross = t0 - b0 * (t1 - t0) / (b1 - b0)
        return [(t0, abs(b0), t_cross, 0.0), (t_cross, 0.0, t1, abs(b1))]
    return [(t0, abs(b0), t1, abs(b1))]


def time_to_accumulate(abs_b0: float, abs_b1: float, duration: float, area: float) -> float:
    """反解 |bias| 线性段的累积函数：从段首累积 area（|bias|·秒）所需的时长。

    |bias| 从 abs_b0 线性变化到 abs_b1（斜率 k = (abs_b1 − abs_b0)/duration），
    累积面积 A(Δt) = abs_b0·Δt + k·Δt²/2 是 Δt 的二次函数，解析求根：
    k ≠ 0 时取数值稳定形式 Δt = 2·area / (abs_b0 + √(abs_b0² + 2k·area))，
    k = 0 时退化为线性 Δt = area / abs_b0。全程解析解，不做固定步长采样。
    调用前须保证 0 < area ≤ 段内总面积（段内 |bias| 不恒为零）；
    判别式与结果做浮点误差钳制，area 恰等于段内总面积时返回 duration。
    """
    k = (abs_b1 - abs_b0) / duration
    if k == 0.0:  # |bias| 恒定：累积随时间线性增长
        return area / abs_b0
    discriminant = max(0.0, abs_b0 * abs_b0 + 2.0 * k * area)
    offset = 2.0 * area / (abs_b0 + math.sqrt(discriminant))
    return min(offset, duration)


def _above_threshold(t0: float, v0: float, t1: float, v1: float, limit: float) -> tuple[float, float] | None:
    """线性函数 v 在 [t0, t1] 上严格大于 limit 的子区间端点 (s, e)；无则 None。

    恰等于 limit 不计入；超限集本身是开/半开区间，这里返回其闭包端点
    （端点瞬时时长为零，不影响持续秒数与合并判定）。
    """
    dt = t1 - t0
    if dt <= 0:
        return None
    above0 = v0 > limit
    above1 = v1 > limit
    if above0 and above1:
        return t0, t1
    if not above0 and not above1:
        return None
    # 恰一端超限：解析交点 t* = t0 + (limit − v0)·Δt/(v1 − v0)
    t_cross = t0 + (limit - v0) * dt / (v1 - v0)
    if above0:  # v0 > limit ≥ v1：段首到交点
        return t0, t_cross
    return t_cross, t1  # v1 > limit ≥ v0：交点到段尾


def exceedance_subranges(t0: float, b0: float, t1: float, b1: float, limit: float) -> list[tuple[float, float, str]]:
    """求线性段 (t0,b0)-(t1,b1) 内 |bias| > limit 的解析子区间。

    返回 [(s, e, direction), ...]，按时间排序，至多两段：高端（bias > limit，
    direction="high"）与低端（bias < −limit，direction="low"，通过对 bias 取负
    复用同一交点求解）。恰等于 limit 不计入。全程解析解，不做固定步长采样。
    """
    out = []
    high = _above_threshold(t0, b0, t1, b1, limit)
    if high is not None:
        out.append((high[0], high[1], "high"))
    low = _above_threshold(t0, -b0, t1, -b1, limit)
    if low is not None:
        out.append((low[0], low[1], "low"))
    out.sort(key=lambda r: r[0])  # 同一段内高低两段不重叠，按起点排即时间序
    return out


# ---------- 观测插入影响预演 ----------

def insertion_impact(
    t_prev: float, t_candidate: float, t_next: float, delta_candidate: float, limit: float
) -> tuple[float, float, list[tuple[float, float]]]:
    """解析比较插入候选观测前后的分段线性偏差曲线。

    插入只会把 (t_prev, t_next) 一段重新分段，曲线变化量 Δ 是折点为
    (t_prev, 0) → (t_candidate, delta_candidate) → (t_next, 0) 的分段线性函数，
    该范围之外 Δ 恒为 0。返回 (max_abs_change, max_change_at, exceedances)：

    - max_abs_change = |delta_candidate|：两段上 |Δ| 分别由 0 单调升至峰值再降回 0，
      最大值必在候选点取得；
    - max_change_at 为该最大值的最早发生时点：delta_candidate ≠ 0 时唯一（候选点
      本身）；delta_candidate = 0 时 Δ 恒为 0，取受影响范围起点 t_prev；
    - exceedances 为 |Δ| > limit 的连续区间 [(s, e)]（至多一段）：在两段上分别复用
      exceedance_subranges 解析求解与 limit 的交点，跨候选点仍超限的片段合并；
      恰等于 limit 的瞬时不计入。全程解析解，不做固定步长采样。
    """
    max_abs_change = abs(delta_candidate)
    max_change_at = t_candidate if delta_candidate != 0.0 else t_prev
    exceedances: list[list[float]] = []
    for s, e, _direction in exceedance_subranges(t_prev, 0.0, t_candidate, delta_candidate, limit):
        exceedances.append([s, e])
    for s, e, _direction in exceedance_subranges(t_candidate, delta_candidate, t_next, 0.0, limit):
        if exceedances and exceedances[-1][1] == s and max_abs_change > limit:
            exceedances[-1][1] = e  # 候选点两侧同属一段超限：跨候选点合并
        else:
            exceedances.append([s, e])
    return max_abs_change, max_change_at, [(s, e) for s, e in exceedances]


# ---------- 最长稳定窗口（max−min ≤ spread） ----------

def _clip_to_band(t0: float, b0: float, t1: float, b1: float, lo: float, hi: float) -> tuple[float, float] | None:
    """线性段 (t0,b0)-(t1,b1) 上 lo ≤ b ≤ hi 的子区间 (u, v)；完全在带外则 None。

    端点落在带内时 u/v 直接取精确的端点时刻；仅当边界严格穿过带宽时才计算解析
    交点 t* = t0 + (level − b0)·Δt/(b1 − b0)。这样相邻段在带内折点处的拼接是
    精确的（不受除法舍入误差影响），全程解析解，不做固定步长采样。
    """
    in0 = lo <= b0 <= hi
    in1 = lo <= b1 <= hi
    if in0 and in1:
        return t0, t1
    if b0 == b1:  # 恒定且在带外
        return None
    k = (b1 - b0) / (t1 - t0)
    t_lo = t0 + (lo - b0) / k   # b = lo 的解析交点
    t_hi = t0 + (hi - b0) / k   # b = hi 的解析交点
    enter, leave = (t_lo, t_hi) if k > 0 else (t_hi, t_lo)
    u = t0 if in0 else max(t0, enter)
    v = t1 if in1 else min(t1, leave)
    if u > v:
        return None
    return u, v


def _band_components(points: list[tuple[float, float]], lo: float, hi: float):
    """产出 {t : lo ≤ b(t) ≤ hi} 的各连通分量 (s, e)，按时间排序。

    相邻线性段在共享折点处同属带宽时合并为同一分量；仅在折点处相触也算相连
    （该瞬时时长为零，窗口经过该点不影响 max−min ≤ spread 的成立）。
    """
    cur_s = cur_e = None
    for (t0, b0), (t1, b1) in zip(points, points[1:]):
        clip = _clip_to_band(t0, b0, t1, b1, lo, hi)
        if clip is None:
            if cur_s is not None:
                yield cur_s, cur_e
                cur_s = cur_e = None
            continue
        u, v = clip
        if cur_e is not None and u == cur_e:
            cur_e = v                      # 与上一段在折点处相接：延伸当前分量
        else:
            if cur_s is not None:
                yield cur_s, cur_e
            cur_s, cur_e = u, v
    if cur_s is not None:
        yield cur_s, cur_e


def _value_at(points: list[tuple[float, float]], t: float) -> float:
    """分段线性函数在 t 处的取值（t 须在 points 覆盖范围内）。"""
    for (t0, b0), (t1, b1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            return interpolate(t, t0, b0, t1, b1)
    return points[-1][1]


def _is_better(duration: float, s: float, best_duration: float, best_s: float) -> bool:
    """候选窗口是否优于当前最优：时长更长；时长并列（含浮点噪声）时取起点更早者。"""
    tol = 1e-9 * max(1.0, abs(duration), abs(best_duration))
    if duration > best_duration + tol:
        return True
    return abs(duration - best_duration) <= tol and s < best_s


def stability_window(points: list[tuple[float, float]], spread: float) -> tuple[float, float, float, float]:
    """求分段线性 bias 上 max(bias) − min(bias) ≤ spread 的最长连续子区间。

    points 为按时间升序的折点 [(t, b)]（含裁剪后的查询窗口端点）。最优窗口必为
    某临界带宽 [m, m+spread] 的连通分量：带宽边缘未触及任何折点取值时，微调 m
    可让分量至少一端继续外伸、时长严格增大（或平移到更早的起点），故只需枚举
    m 取遍折点取值 b_i 与 b_i − spread。分量边界由线性段与 m / m+spread 的解析
    交点定位，不做固定步长采样。返回 (s, e, min_bias, max_bias)，其中
    min_bias/max_bias 为窗口内实际达到的极值；时长并列时取起点最早者。
    """
    values = [b for _, b in points]
    candidates = sorted({*values, *(v - spread for v in values)})
    best_duration = best_s = best_e = None
    for m in candidates:
        for s, e in _band_components(points, m, m + spread):
            duration = e - s
            if best_duration is None or _is_better(duration, s, best_duration, best_s):
                best_duration, best_s, best_e = duration, s, e
    s, e = best_s, best_e
    extrema = [_value_at(points, s), _value_at(points, e)]
    extrema += [b for t, b in points if s < t < e]
    return s, e, min(extrema), max(extrema)
