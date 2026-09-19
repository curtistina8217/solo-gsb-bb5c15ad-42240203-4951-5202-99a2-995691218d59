"""端到端 API 测试：每个用例使用独立临时 SQLite 库。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    from app.main import app  # 环境变量设置后再导入

    with TestClient(app) as c:
        yield c


def make_observation(client, ts, reference, tolerance, readings):
    resp = client.post(
        "/observations",
        json={"timestamp": ts, "reference": reference, "tolerance": tolerance, "readings": readings},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def revise_observation(client, obs_id, ts, reference, tolerance, readings, revision):
    resp = client.put(
        f"/observations/{obs_id}",
        json={"timestamp": ts, "reference": reference, "tolerance": tolerance,
              "readings": readings, "revision": revision},
    )
    return resp


# ---------- 观测登记 ----------

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_observation_marks_outliers_and_computes_bias(client):
    body = make_observation(client, 1000.0, reference=10.0, tolerance=0.5,
                            readings=[9.0, 9.1, 8.9, 20.0])
    # 组中位数 9.05，20.0 偏离超差判为异常；有效读数中位数 9.0 → 偏差 10.0 - 9.0 = 1.0
    assert body["group_median"] == pytest.approx(9.05)
    assert body["bias"] == pytest.approx(1.0)
    assert body["outliers"] == [20.0]
    flags = {r["value"]: r["is_outlier"] for r in body["readings"]}
    assert flags == {9.0: False, 9.1: False, 8.9: False, 20.0: True}


def test_raw_readings_are_preserved(client):
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9, 20.0])
    obs = client.get("/observations").json()
    assert len(obs) == 1
    assert sorted(r["value"] for r in obs[0]["readings"]) == [8.9, 9.0, 9.1, 20.0]


def test_fewer_than_three_readings_rejected(client):
    resp = client.post("/observations",
                       json={"timestamp": 1000.0, "reference": 1.0, "tolerance": 0.1,
                             "readings": [1.0, 1.1]})
    assert resp.status_code == 422


def test_all_outliers_rejected(client):
    # 容差为 0 时四个读数两两不同，全部偏离中位数 → 无法计算偏差
    resp = client.post("/observations",
                       json={"timestamp": 1000.0, "reference": 1.0, "tolerance": 0.0,
                             "readings": [1.0, 2.0, 3.0, 4.0]})
    assert resp.status_code == 422
    assert "cannot compute a calibration bias" in resp.json()["detail"]


def test_duplicate_timestamp_rejected(client):
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])
    resp = client.post("/observations",
                       json={"timestamp": 1000.0, "reference": 10.0, "tolerance": 0.5,
                             "readings": [9.0, 9.1, 8.9]})
    assert resp.status_code == 409


def test_iso8601_timestamp_accepted(client):
    body = make_observation(client, "1970-01-01T00:16:40Z", 10.0, 0.5, [9.0, 9.1, 8.9])
    assert body["timestamp"] == pytest.approx(1000.0)


# ---------- 漂移补偿 ----------

@pytest.fixture()
def two_points(client):
    make_observation(client, 1000.0, reference=10.0, tolerance=0.5, readings=[9.0, 9.1, 8.9, 20.0])  # bias 1.0
    make_observation(client, 2000.0, reference=10.0, tolerance=0.5, readings=[7.0, 7.1, 6.9])        # bias 3.0
    return client


def test_correction_interpolates_linearly(two_points):
    resp = two_points.post("/corrections", json={"timestamp": 1500.0, "reading": 5.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bias"] == pytest.approx(2.0)        # 1.0 与 3.0 的中点
    assert body["corrected"] == pytest.approx(7.0)   # 读数 + 偏差
    assert body["interval"]["t_start"] == 1000.0
    assert body["interval"]["t_end"] == 2000.0
    assert body["interval"]["bias_start"] == pytest.approx(1.0)
    assert body["interval"]["bias_end"] == pytest.approx(3.0)


def test_correction_reports_endpoint_anomalies(two_points):
    body = two_points.post("/corrections", json={"timestamp": 1500.0, "reading": 5.0}).json()
    assert len(body["endpoints"]) == 2
    assert body["endpoints"][0]["outliers"] == [20.0]
    assert body["endpoints"][1]["outliers"] == []


def test_correction_at_exact_observation_point(two_points):
    body = two_points.post("/corrections", json={"timestamp": 2000.0, "reading": 5.0}).json()
    assert body["bias"] == pytest.approx(3.0)
    assert body["corrected"] == pytest.approx(8.0)
    assert body["interval"] is None
    assert len(body["endpoints"]) == 1


def test_correction_outside_coverage_rejected(two_points):
    for ts in (999.0, 2001.0):
        resp = two_points.post("/corrections", json={"timestamp": ts, "reading": 5.0})
        assert resp.status_code == 400
        assert "outside calibration coverage" in resp.json()["detail"]


def test_correction_without_any_observation_rejected(client):
    resp = client.post("/corrections", json={"timestamp": 1.0, "reading": 5.0})
    assert resp.status_code == 400


# ---------- 区间重算 ----------

def test_adjacent_intervals_recomputed_after_insert(two_points):
    # 在 1000 与 2000 之间插入新观测（bias 2.0）
    make_observation(two_points, 1500.0, reference=10.0, tolerance=0.5, readings=[8.0, 8.1, 7.9])
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(1000.0, 1500.0), (1500.0, 2000.0)]

    # 旧区间中点 1250 现在应基于新区间 (1000,1500) 插值：bias = 1.5
    body = two_points.post("/corrections", json={"timestamp": 1250.0, "reading": 10.0}).json()
    assert body["bias"] == pytest.approx(1.5)
    assert body["corrected"] == pytest.approx(11.5)


def test_intervals_extend_at_edges(two_points):
    make_observation(two_points, 500.0, 10.0, 0.5, [9.5, 9.6, 9.4])   # bias 0.5
    make_observation(two_points, 3000.0, 10.0, 0.5, [6.0, 6.1, 5.9])  # bias 4.0
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(500.0, 1000.0), (1000.0, 2000.0), (2000.0, 3000.0)]
    # 覆盖范围随之扩展
    assert two_points.post("/corrections", json={"timestamp": 750.0, "reading": 0.0}).status_code == 200
    assert two_points.post("/corrections", json={"timestamp": 2500.0, "reading": 0.0}).status_code == 200


# ---------- 观测修订 ----------

def test_new_observation_starts_at_revision_1(client):
    body = make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])
    assert body["revision"] == 1


def test_revise_same_position_recomputes_bias_and_intervals(two_points):
    # 将 1000 处观测的偏差从 1.0 修订为 0.0（读数全部贴近参考值）
    resp = revise_observation(two_points, 1, 1000.0, 10.0, 0.5, [10.0, 10.1, 9.9], revision=1)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == 1                 # ID 保持不变
    assert body["revision"] == 2           # 版本推进
    assert body["bias"] == pytest.approx(0.0)
    assert body["outliers"] == []

    # 区间端点偏差随之更新
    interval = two_points.get("/intervals").json()
    assert [(iv["t_start"], iv["t_end"]) for iv in interval] == [(1000.0, 2000.0)]
    assert interval[0]["bias_start"] == pytest.approx(0.0)
    assert interval[0]["bias_end"] == pytest.approx(3.0)
    # 中点插值偏差由 2.0 变为 1.5
    body = two_points.post("/corrections", json={"timestamp": 1500.0, "reading": 0.0}).json()
    assert body["bias"] == pytest.approx(1.5)


def test_revise_moves_observation_and_rebuilds_both_locations(two_points):
    # 把 1000 处观测（id=1）前移到 500：原位置缺口接通，新位置一分为二
    resp = revise_observation(two_points, 1, 500.0, 10.0, 0.5, [9.5, 9.6, 9.4], revision=1)
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(500.0, 2000.0)]      # 1000 不再是观测点，区间不被拆分


def test_revise_into_gap_splits_new_interval(two_points):
    # 把 1000 处观测移动到 1000 与 2000 之间的 1500
    resp = revise_observation(two_points, 1, 1500.0, 10.0, 0.5, [8.0, 8.1, 7.9], revision=1)
    assert resp.status_code == 200, resp.text
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(1500.0, 2000.0)]     # 原位置 1000 左侧已无观测，缺口无需接通


def test_revise_middle_point_in_three_point_chain(client):
    # 三点链 1000(bias1) / 2000(bias3) / 3000(bias4)，把中间点移到 1500(bias2)
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9, 20.0])
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])
    make_observation(client, 3000.0, 10.0, 0.5, [6.0, 6.1, 5.9])
    resp = revise_observation(client, 2, 1500.0, 10.0, 0.5, [8.0, 8.1, 7.9], revision=1)
    assert resp.status_code == 200, resp.text
    intervals = client.get("/intervals").json()
    # 原位置缺口 (1000,3000) 不得被接通——观测自身已落在新位置 1500
    assert [(iv["t_start"], iv["t_end"]) for iv in intervals] == [
        (1000.0, 1500.0), (1500.0, 3000.0),
    ]
    assert client.post("/corrections", json={"timestamp": 1250.0, "reading": 0.0}).json()["bias"] \
        == pytest.approx(1.5)
    assert client.post("/corrections", json={"timestamp": 2500.0, "reading": 0.0}).json()["bias"] \
        == pytest.approx(10 / 3)  # 新区间 (1500 bias2, 3000 bias4) 的 2/3 处
    # 再移回 2000（小幅右移越过旧缺口左侧），区间仍应恰好两段
    resp = revise_observation(client, 2, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9], revision=2)
    assert resp.status_code == 200, resp.text
    assert [(iv["t_start"], iv["t_end"]) for iv in client.get("/intervals").json()] == [
        (1000.0, 2000.0), (2000.0, 3000.0),
    ]


def test_revise_beyond_edge_extends_coverage(two_points):
    # 把 1000 处观测移动到 3000，覆盖范围变为 [2000, 3000]
    resp = revise_observation(two_points, 1, 3000.0, 10.0, 0.5, [6.0, 6.1, 5.9], revision=1)
    assert resp.status_code == 200, resp.text
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(2000.0, 3000.0)]
    assert two_points.post("/corrections", json={"timestamp": 1500.0, "reading": 0.0}).status_code == 400
    assert two_points.post("/corrections", json={"timestamp": 2500.0, "reading": 0.0}).status_code == 200


def test_revise_stale_revision_conflicts(two_points):
    ok = revise_observation(two_points, 1, 1000.0, 10.0, 0.5, [10.0, 10.1, 9.9], revision=1)
    assert ok.status_code == 200
    stale = revise_observation(two_points, 1, 1000.0, 10.0, 0.5, [10.0, 10.1, 9.9], revision=1)
    assert stale.status_code == 409
    # 冲突时数据未被二次修订
    obs = next(o for o in two_points.get("/observations").json() if o["id"] == 1)
    assert obs["revision"] == 2


def test_revise_timestamp_clash_conflicts(two_points):
    # 把 1000 处观测改到已被 2000 处观测占用的时点 → 409，区间维持原样
    resp = revise_observation(two_points, 1, 2000.0, 10.0, 0.5, [9.0, 9.1, 8.9], revision=1)
    assert resp.status_code == 409
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(1000.0, 2000.0)]


def test_revise_unknown_id_returns_404(two_points):
    resp = revise_observation(two_points, 999, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9], revision=1)
    assert resp.status_code == 404


def test_revise_invalid_readings_returns_422(two_points):
    resp = revise_observation(two_points, 1, 1000.0, 10.0, 0.0, [1.0, 2.0, 3.0, 4.0], revision=1)
    assert resp.status_code == 422


def test_revisions_history_returns_snapshots_newest_first(two_points):
    # 第一次修订（revision 1 -> 2）：1000 → 900，偏差 1.0 → 约 0.5
    r1 = revise_observation(two_points, 1, 900.0, 10.0, 0.5, [9.5, 9.6, 9.4], revision=1)
    assert r1.status_code == 200
    # 第二次修订（revision 2 -> 3）：900 → 800
    r2 = revise_observation(two_points, 1, 800.0, 10.0, 0.5, [9.0, 9.1, 8.9], revision=2)
    assert r2.status_code == 200

    resp = two_points.get("/observations/1/revisions")
    assert resp.status_code == 200
    history = resp.json()
    assert [h["revision"] for h in history] == [2, 1]   # 按版本倒序

    snap_v1 = history[1]
    assert snap_v1["old_timestamp"] == 1000.0
    assert snap_v1["old_timestamp_iso"].startswith("1970-01-01T00:16:40")
    assert snap_v1["old_reference"] == 10.0
    assert snap_v1["old_tolerance"] == 0.5
    assert snap_v1["old_bias"] == pytest.approx(1.0)
    assert snap_v1["old_readings"] == [9.0, 9.1, 8.9, 20.0]
    assert "changed_at" in snap_v1 and "changed_at_iso" in snap_v1

    snap_v2 = history[0]
    assert snap_v2["old_timestamp"] == 900.0
    assert snap_v2["old_readings"] == [9.5, 9.6, 9.4]


def test_revisions_history_empty_before_any_revision(two_points):
    assert two_points.get("/observations/1/revisions").json() == []


def test_revisions_history_unknown_id_returns_404(two_points):
    assert two_points.get("/observations/999/revisions").status_code == 404


# ---------- 累计漂移量积分 ----------

@pytest.fixture()
def three_points(client):
    # bias：1000 → 1.0，2000 → 3.0，3000 → -1.0（第二段在 t=2750 处过零）
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9, 20.0])
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])
    make_observation(client, 3000.0, 10.0, 0.5, [11.0, 11.1, 10.9])
    return client


def test_drift_integral_full_coverage(three_points):
    resp = three_points.get("/drift-integral", params={"from": 1000.0, "to": 3000.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from"] == pytest.approx(1000.0)
    assert body["to"] == pytest.approx(3000.0)
    # 段1 (1→3)：梯形 2000；段2 (3→-1)：梯形 1000
    assert body["signed_bias_seconds"] == pytest.approx(3000.0)
    # 段2 过零（t=2750）：两三角形 1125 + 125 = 1250；段1 不变号 2000
    assert body["absolute_bias_seconds"] == pytest.approx(3250.0)

    contribs = body["contributions"]
    assert [(c["t_start"], c["t_end"]) for c in contribs] == [(1000.0, 2000.0), (2000.0, 3000.0)]
    assert contribs[0]["bias_start"] == pytest.approx(1.0)
    assert contribs[0]["bias_end"] == pytest.approx(3.0)
    assert contribs[0]["signed_bias_seconds"] == pytest.approx(2000.0)
    assert contribs[0]["absolute_bias_seconds"] == pytest.approx(2000.0)
    assert contribs[1]["signed_bias_seconds"] == pytest.approx(1000.0)
    assert contribs[1]["absolute_bias_seconds"] == pytest.approx(1250.0)
    # 分段贡献之和分别等于汇总值
    assert sum(c["signed_bias_seconds"] for c in contribs) == body["signed_bias_seconds"]
    assert sum(c["absolute_bias_seconds"] for c in contribs) == body["absolute_bias_seconds"]


def test_drift_integral_clips_window_inside_intervals(three_points):
    # 窗口两端都落在区间内部：[1500, 2500]
    body = three_points.get("/drift-integral", params={"from": 1500.0, "to": 2500.0}).json()
    contribs = body["contributions"]
    assert [(c["t_start"], c["t_end"]) for c in contribs] == [(1500.0, 2000.0), (2000.0, 2500.0)]
    # 裁剪点偏差按线性插值：bias(1500)=2.0，bias(2500)=1.0
    assert contribs[0]["bias_start"] == pytest.approx(2.0)
    assert contribs[1]["bias_end"] == pytest.approx(1.0)
    assert body["signed_bias_seconds"] == pytest.approx(1250.0 + 1000.0)
    assert body["absolute_bias_seconds"] == pytest.approx(2250.0)  # 窗口未触及零点


def test_drift_integral_splits_absolute_area_at_zero_crossing(three_points):
    # 窗口 [2500, 3000]：bias 1.0 → -1.0，t=2750 处过零
    body = three_points.get("/drift-integral", params={"from": 2500.0, "to": 3000.0}).json()
    assert body["signed_bias_seconds"] == pytest.approx(0.0)          # 两三角形相互抵消
    assert body["absolute_bias_seconds"] == pytest.approx(125.0 + 125.0)
    assert len(body["contributions"]) == 1
    contrib = body["contributions"][0]
    assert contrib["signed_bias_seconds"] == pytest.approx(0.0)
    assert contrib["absolute_bias_seconds"] == pytest.approx(250.0)


def test_drift_integral_window_at_observation_points(three_points):
    # 边界恰落在观测点上（含端点）
    body = three_points.get("/drift-integral", params={"from": 1000.0, "to": 2000.0}).json()
    assert body["signed_bias_seconds"] == pytest.approx(2000.0)
    assert [(c["t_start"], c["t_end"]) for c in body["contributions"]] == [(1000.0, 2000.0)]


def test_drift_integral_accepts_iso8601_bounds(two_points):
    resp = two_points.get(
        "/drift-integral",
        params={"from": "1970-01-01T00:16:40Z", "to": "1970-01-01T00:33:20Z"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["signed_bias_seconds"] == pytest.approx(2000.0)  # (1+3)/2 × 1000s
    assert body["absolute_bias_seconds"] == pytest.approx(2000.0)


def test_drift_integral_rejects_inverted_or_empty_window(three_points):
    for params in ({"from": 2000.0, "to": 2000.0}, {"from": 3000.0, "to": 1000.0}):
        resp = three_points.get("/drift-integral", params=params)
        assert resp.status_code == 400
        assert "must be less than" in resp.json()["detail"]


def test_drift_integral_out_of_coverage_rejected(three_points):
    for params in ({"from": 999.0, "to": 2000.0}, {"from": 2000.0, "to": 3001.0}):
        resp = three_points.get("/drift-integral", params=params)
        assert resp.status_code == 400
        assert "outside calibration coverage" in resp.json()["detail"]


def test_drift_integral_without_any_observation_rejected(client):
    resp = client.get("/drift-integral", params={"from": 1.0, "to": 2.0})
    assert resp.status_code == 400


# ---------- 漂移超限窗口分析 ----------

@pytest.fixture()
def exceedance_points(client):
    # bias：1000 → 1.0，2000 → 3.0，3000 → -1.0，4000 → -3.0
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9, 20.0])
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])
    make_observation(client, 3000.0, 10.0, 0.5, [11.0, 11.1, 10.9])
    make_observation(client, 4000.0, 10.0, 0.5, [13.0, 13.1, 12.9])
    return client


def test_drift_exceedances_full_window(exceedance_points):
    resp = exceedance_points.get("/drift-exceedances",
                                 params={"from": 1000.0, "to": 4000.0, "limit": 2.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from"] == pytest.approx(1000.0)
    assert body["to"] == pytest.approx(4000.0)
    assert body["limit"] == pytest.approx(2.0)

    exc = body["exceedances"]
    assert [(e["t_start"], e["t_end"], e["direction"]) for e in exc] == [
        (1500.0, 2250.0, "high"),   # 段1 交点 t=1500 与段2 交点 t=2250 跨观测点 2000 合并
        (3500.0, 4000.0, "low"),    # 段3 在 t=3500 处跌破 -2
    ]
    assert exc[0]["duration_seconds"] == pytest.approx(750.0)
    assert exc[0]["peak_abs_deviation"] == pytest.approx(3.0)
    assert exc[0]["start_iso"].startswith("1970-01-01T00:25:00")
    assert exc[1]["duration_seconds"] == pytest.approx(500.0)
    assert exc[1]["peak_abs_deviation"] == pytest.approx(3.0)


def test_drift_exceedances_merges_across_multiple_observation_points(client):
    # bias 3 → 5 → 4：三点全程超限，峰值落在中间观测点上
    make_observation(client, 1000.0, 10.0, 0.5, [7.0, 7.1, 6.9])
    make_observation(client, 2000.0, 10.0, 0.5, [5.0, 5.1, 4.9])
    make_observation(client, 3000.0, 10.0, 0.5, [6.0, 6.1, 5.9])
    body = client.get("/drift-exceedances",
                      params={"from": 1000.0, "to": 3000.0, "limit": 2.0}).json()
    assert len(body["exceedances"]) == 1
    exc = body["exceedances"][0]
    assert (exc["t_start"], exc["t_end"], exc["direction"]) == (1000.0, 3000.0, "high")
    assert exc["duration_seconds"] == pytest.approx(2000.0)
    assert exc["peak_abs_deviation"] == pytest.approx(5.0)


def test_drift_exceedances_clips_window_inside_intervals(exceedance_points):
    # 窗口两端落在区间内部：[1750, 2500]，bias(1750)=2.5，bias(2500)=1.0
    body = exceedance_points.get("/drift-exceedances",
                                 params={"from": 1750.0, "to": 2500.0, "limit": 2.0}).json()
    assert [(e["t_start"], e["t_end"], e["direction"]) for e in body["exceedances"]] == [
        (1750.0, 2250.0, "high"),
    ]
    assert body["exceedances"][0]["duration_seconds"] == pytest.approx(500.0)
    assert body["exceedances"][0]["peak_abs_deviation"] == pytest.approx(3.0)


def test_drift_exceedances_exact_threshold_not_counted(client):
    # 偏差恒等于阈值：不算超限
    make_observation(client, 1000.0, 10.0, 0.5, [8.0, 8.1, 7.9])   # bias 2.0
    make_observation(client, 2000.0, 10.0, 0.5, [8.0, 8.1, 7.9])   # bias 2.0
    body = client.get("/drift-exceedances",
                      params={"from": 1000.0, "to": 2000.0, "limit": 2.0}).json()
    assert body["exceedances"] == []


def test_drift_exceedances_touch_at_observation_point_not_merged(client):
    # bias 3 → 2 → 3：观测点恰好触及阈值，该瞬时不算超限，两侧不合并
    make_observation(client, 1000.0, 10.0, 0.5, [7.0, 7.1, 6.9])   # bias 3.0
    make_observation(client, 2000.0, 10.0, 0.5, [8.0, 8.1, 7.9])   # bias 2.0
    make_observation(client, 3000.0, 10.0, 0.5, [7.0, 7.1, 6.9])   # bias 3.0
    body = client.get("/drift-exceedances",
                      params={"from": 1000.0, "to": 3000.0, "limit": 2.0}).json()
    assert [(e["t_start"], e["t_end"], e["direction"]) for e in body["exceedances"]] == [
        (1000.0, 2000.0, "high"),
        (2000.0, 3000.0, "high"),
    ]
    for e in body["exceedances"]:
        assert e["duration_seconds"] == pytest.approx(1000.0)
        assert e["peak_abs_deviation"] == pytest.approx(3.0)


def test_drift_exceedances_empty_when_never_exceeded(exceedance_points):
    body = exceedance_points.get("/drift-exceedances",
                                 params={"from": 1000.0, "to": 4000.0, "limit": 10.0}).json()
    assert body["exceedances"] == []


def test_drift_exceedances_high_and_low_in_same_interval(client):
    # 单区间内 bias 5 → -5：先高端超限后低端超限，按时间排序
    make_observation(client, 1000.0, 10.0, 0.5, [5.0, 5.1, 4.9])    # bias 5.0
    make_observation(client, 2000.0, 10.0, 0.5, [15.0, 15.1, 14.9])  # bias -5.0
    body = client.get("/drift-exceedances",
                      params={"from": 1000.0, "to": 2000.0, "limit": 1.0}).json()
    assert [(e["t_start"], e["t_end"], e["direction"]) for e in body["exceedances"]] == [
        (1000.0, 1400.0, "high"),
        (1600.0, 2000.0, "low"),
    ]
    for e in body["exceedances"]:
        assert e["duration_seconds"] == pytest.approx(400.0)
        assert e["peak_abs_deviation"] == pytest.approx(5.0)


def test_drift_exceedances_accepts_iso8601_bounds(exceedance_points):
    resp = exceedance_points.get(
        "/drift-exceedances",
        params={"from": "1970-01-01T00:16:40Z", "to": "1970-01-01T01:06:40Z", "limit": 2.0},
    )
    assert resp.status_code == 200, resp.text
    exc = resp.json()["exceedances"]
    assert [(e["t_start"], e["t_end"], e["direction"]) for e in exc] == [
        (1500.0, 2250.0, "high"),
        (3500.0, 4000.0, "low"),
    ]


def test_drift_exceedances_rejects_inverted_or_empty_window(exceedance_points):
    for params in ({"from": 2000.0, "to": 2000.0, "limit": 2.0},
                   {"from": 3000.0, "to": 1000.0, "limit": 2.0}):
        resp = exceedance_points.get("/drift-exceedances", params=params)
        assert resp.status_code == 400
        assert "must be less than" in resp.json()["detail"]


def test_drift_exceedances_rejects_non_positive_limit(exceedance_points):
    for limit in (0.0, -0.5):
        resp = exceedance_points.get("/drift-exceedances",
                                     params={"from": 1000.0, "to": 4000.0, "limit": limit})
        assert resp.status_code == 400
        assert "must be positive" in resp.json()["detail"]


def test_drift_exceedances_out_of_coverage_rejected(exceedance_points):
    for params in ({"from": 999.0, "to": 2000.0, "limit": 2.0},
                   {"from": 2000.0, "to": 4001.0, "limit": 2.0}):
        resp = exceedance_points.get("/drift-exceedances", params=params)
        assert resp.status_code == 400
        assert "outside calibration coverage" in resp.json()["detail"]


def test_drift_exceedances_without_any_observation_rejected(client):
    resp = client.get("/drift-exceedances", params={"from": 1.0, "to": 2.0, "limit": 2.0})
    assert resp.status_code == 400


# ---------- 历史时点漂移补偿（as_of 状态重建） ----------

@pytest.fixture()
def clock(monkeypatch):
    """可控时钟：service 里的 time.time() 返回当前值，测试可随时推进。"""
    import app.service as service_module

    now = [1_000_000.0]
    monkeypatch.setattr(service_module.time, "time", lambda: now[0])
    return now


def test_historical_correction_excludes_not_yet_created_observations(client, clock):
    clock[0] = 100.0
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])   # id=1, bias 1.0
    clock[0] = 200.0
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])   # id=2, bias 3.0

    # as_of=150：第二条观测尚未创建，覆盖范围只有 [1000, 1000]
    resp = client.get("/historical-correction",
                      params={"effective_at": 1500.0, "as_of": 150.0, "reading": 5.0})
    assert resp.status_code == 400
    assert "outside calibration coverage" in resp.json()["detail"]

    # 但恰落在已创建观测点上仍可校正
    body = client.get("/historical-correction",
                      params={"effective_at": 1000.0, "as_of": 150.0, "reading": 5.0}).json()
    assert body["bias"] == pytest.approx(1.0)
    assert body["corrected"] == pytest.approx(6.0)
    assert body["interval"] is None
    assert [(e["observation_id"], e["revision"]) for e in body["endpoints"]] == [(1, 1)]

    # as_of 恰等于第二条观测的 created_at：已创建，参与插值
    body = client.get("/historical-correction",
                      params={"effective_at": 1500.0, "as_of": 200.0, "reading": 5.0}).json()
    assert body["bias"] == pytest.approx(2.0)
    assert body["corrected"] == pytest.approx(7.0)
    assert [(e["observation_id"], e["revision"]) for e in body["endpoints"]] == [(1, 1), (2, 1)]


def test_historical_correction_rolls_back_revisions(client, clock):
    clock[0] = 100.0
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])    # id=1, bias 1.0
    clock[0] = 200.0
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])    # id=2, bias 3.0
    clock[0] = 300.0
    resp = revise_observation(client, 1, 1000.0, 10.0, 0.5, [10.0, 10.1, 9.9], revision=1)
    assert resp.status_code == 200                                   # bias 变为 0.0，changed_at=300

    # as_of=250：修订尚未发生，用旧版本（revision 1，bias 1.0）
    body = client.get("/historical-correction",
                      params={"effective_at": 1500.0, "as_of": 250.0, "reading": 0.0}).json()
    assert body["bias"] == pytest.approx(2.0)
    assert body["interval"]["bias_start"] == pytest.approx(1.0)
    assert [(e["observation_id"], e["revision"]) for e in body["endpoints"]] == [(1, 1), (2, 1)]

    # as_of 恰等于 changed_at=300：使用新版本（revision 2，bias 0.0）
    body = client.get("/historical-correction",
                      params={"effective_at": 1500.0, "as_of": 300.0, "reading": 0.0}).json()
    assert body["bias"] == pytest.approx(1.5)
    assert body["interval"]["bias_start"] == pytest.approx(0.0)
    assert [(e["observation_id"], e["revision"]) for e in body["endpoints"]] == [(1, 2), (2, 1)]

    # as_of=400：修订已生效，与当前状态一致
    body = client.get("/historical-correction",
                      params={"effective_at": 1500.0, "as_of": 400.0, "reading": 0.0}).json()
    assert body["bias"] == pytest.approx(1.5)


def test_historical_correction_rolls_back_moved_timestamp(client, clock):
    clock[0] = 100.0
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])    # id=1, bias 1.0
    clock[0] = 200.0
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])    # id=2, bias 3.0
    clock[0] = 300.0
    resp = revise_observation(client, 1, 500.0, 10.0, 0.5, [9.5, 9.6, 9.4], revision=1)
    assert resp.status_code == 200                                   # 移到 500，bias 0.5

    # as_of=250：观测 1 仍在 1000，750 在覆盖范围外
    resp = client.get("/historical-correction",
                      params={"effective_at": 750.0, "as_of": 250.0, "reading": 0.0})
    assert resp.status_code == 400
    body = client.get("/historical-correction",
                      params={"effective_at": 1500.0, "as_of": 250.0, "reading": 0.0}).json()
    assert (body["interval"]["t_start"], body["interval"]["t_end"]) == (1000.0, 2000.0)
    assert body["bias"] == pytest.approx(2.0)

    # as_of=350：观测 1 已移到 500，区间变为 (500, 2000)，覆盖范围随之扩大
    body = client.get("/historical-correction",
                      params={"effective_at": 750.0, "as_of": 350.0, "reading": 0.0}).json()
    assert (body["interval"]["t_start"], body["interval"]["t_end"]) == (500.0, 2000.0)
    assert body["bias"] == pytest.approx(0.5 + 2.5 * 250.0 / 1500.0)
    assert body["endpoints"][0]["timestamp"] == 500.0
    assert body["endpoints"][0]["revision"] == 2


def test_historical_correction_rolls_back_multiple_revisions(client, clock):
    clock[0] = 100.0
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])    # id=1, bias 1.0
    clock[0] = 200.0
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])    # id=2, bias 3.0
    clock[0] = 300.0
    revise_observation(client, 1, 900.0, 10.0, 0.5, [9.5, 9.6, 9.4], revision=1)   # → bias 0.5
    clock[0] = 400.0
    revise_observation(client, 1, 800.0, 10.0, 0.5, [8.0, 8.1, 7.9], revision=2)   # → bias 2.0

    # 三个历史时点分别落在版本 1 / 2 / 3 上
    for as_of, expected_ts, expected_bias, expected_rev in (
        (250.0, 1000.0, 1.0, 1),
        (350.0, 900.0, 0.5, 2),
        (450.0, 800.0, 2.0, 3),
    ):
        body = client.get(
            "/historical-correction",
            params={"effective_at": expected_ts, "as_of": as_of, "reading": 0.0},
        ).json()
        assert body["interval"] is None                              # 恰落在重建后的观测点上
        assert body["bias"] == pytest.approx(expected_bias)
        ep = body["endpoints"][0]
        assert (ep["observation_id"], ep["revision"]) == (1, expected_rev)
        assert ep["timestamp"] == expected_ts
        assert ep["bias"] == pytest.approx(expected_bias)


def test_historical_correction_response_carries_as_of(client, clock):
    clock[0] = 100.0
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])
    clock[0] = 200.0
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])
    body = client.get("/historical-correction",
                      params={"effective_at": 1500.0, "as_of": 250.0, "reading": 5.0}).json()
    assert body["effective_at"] == pytest.approx(1500.0)
    assert body["effective_at_iso"].startswith("1970-01-01T00:25:00")
    assert body["as_of"] == pytest.approx(250.0)
    assert body["as_of_iso"].startswith("1970-01-01T00:04:10")
    assert body["reading"] == pytest.approx(5.0)
    assert body["corrected"] == pytest.approx(7.0)


def test_historical_correction_accepts_iso8601_params(client, clock):
    clock[0] = 100.0
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])
    clock[0] = 200.0
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])
    resp = client.get(
        "/historical-correction",
        params={"effective_at": "1970-01-01T00:25:00Z", "as_of": "1970-01-01T00:04:10Z",
                "reading": 5.0},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["bias"] == pytest.approx(2.0)


def test_historical_correction_is_read_only(client, clock):
    clock[0] = 100.0
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9, 20.0])
    clock[0] = 200.0
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])
    clock[0] = 300.0
    revise_observation(client, 1, 500.0, 10.0, 0.5, [9.5, 9.6, 9.4], revision=1)

    before = (
        client.get("/observations").json(),
        client.get("/intervals").json(),
        client.get("/observations/1/revisions").json(),
    )
    # 成功与失败（越界）的历史查询都不得改动任何现状数据
    client.get("/historical-correction",
               params={"effective_at": 1500.0, "as_of": 250.0, "reading": 5.0})
    client.get("/historical-correction",
               params={"effective_at": 100.0, "as_of": 250.0, "reading": 5.0})
    after = (
        client.get("/observations").json(),
        client.get("/intervals").json(),
        client.get("/observations/1/revisions").json(),
    )
    assert after == before


def test_historical_correction_without_any_observation_rejected(client, clock):
    clock[0] = 100.0
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])
    # as_of 早于一切观测的创建时刻：重建状态为空
    resp = client.get("/historical-correction",
                      params={"effective_at": 1000.0, "as_of": 50.0, "reading": 5.0})
    assert resp.status_code == 400


# ---------- 漂移预算耗尽分析 ----------

def test_drift_budget_depletion_within_single_interval(two_points):
    # bias 1.0 → 3.0（1000s）：A(Δt) = Δt + 0.001·Δt²，budget 750 恰在 t=1500 耗尽
    resp = two_points.get("/drift-budget-depletion", params={"from": 1000.0, "budget": 750.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from"] == pytest.approx(1000.0)
    assert body["budget"] == pytest.approx(750.0)
    assert body["depleted_at"] == pytest.approx(1500.0)
    assert body["depleted_at_iso"].startswith("1970-01-01T00:25:00")
    assert body["consumed"] == pytest.approx(750.0)
    assert body["remaining"] == pytest.approx(0.0)
    assert body["coverage_end"] == pytest.approx(2000.0)
    contribs = body["contributions"]
    assert [(c["t_start"], c["t_end"]) for c in contribs] == [(1000.0, 1500.0)]
    assert contribs[0]["bias_start"] == pytest.approx(1.0)
    assert contribs[0]["bias_end"] == pytest.approx(2.0)
    assert contribs[0]["absolute_bias_seconds"] == pytest.approx(750.0)


def test_drift_budget_depletion_exactly_at_coverage_end(two_points):
    # 全程总面积 (1+3)/2 × 1000 = 2000，预算恰在覆盖终点耗尽
    body = two_points.get("/drift-budget-depletion", params={"from": 1000.0, "budget": 2000.0}).json()
    assert body["depleted_at"] == pytest.approx(2000.0)
    assert body["consumed"] == pytest.approx(2000.0)
    assert body["remaining"] == pytest.approx(0.0)
    assert [(c["t_start"], c["t_end"]) for c in body["contributions"]] == [(1000.0, 2000.0)]


def test_drift_budget_not_depleted_within_coverage(two_points):
    body = two_points.get("/drift-budget-depletion", params={"from": 1000.0, "budget": 5000.0}).json()
    assert body["depleted_at"] is None
    assert body["depleted_at_iso"] is None
    assert body["consumed"] == pytest.approx(2000.0)   # 覆盖终点处的全部累计量
    assert body["remaining"] == pytest.approx(3000.0)
    assert body["coverage_end"] == pytest.approx(2000.0)
    assert body["coverage_end_iso"].startswith("1970-01-01T00:33:20")
    contribs = body["contributions"]
    assert [(c["t_start"], c["t_end"]) for c in contribs] == [(1000.0, 2000.0)]
    assert sum(c["absolute_bias_seconds"] for c in contribs) == body["consumed"]


def test_drift_budget_depletion_splits_at_zero_crossing(three_points):
    # 段2 (3 → -1) 在 t=2750 过零：budget 3170 = 2000 + 1125 + 45，恰在 t=2900 耗尽
    body = three_points.get("/drift-budget-depletion", params={"from": 1000.0, "budget": 3170.0}).json()
    assert body["depleted_at"] == pytest.approx(2900.0)
    assert body["consumed"] == pytest.approx(3170.0)
    assert body["remaining"] == pytest.approx(0.0)
    contribs = body["contributions"]
    assert [(c["t_start"], c["t_end"]) for c in contribs] == [(1000.0, 2000.0), (2000.0, 2900.0)]
    assert contribs[0]["absolute_bias_seconds"] == pytest.approx(2000.0)
    assert contribs[1]["bias_start"] == pytest.approx(3.0)
    assert contribs[1]["bias_end"] == pytest.approx(-0.6)
    assert contribs[1]["absolute_bias_seconds"] == pytest.approx(1170.0)
    assert sum(c["absolute_bias_seconds"] for c in contribs) == body["consumed"]


def test_drift_budget_depletion_exactly_at_zero_crossing(three_points):
    # budget 3125 = 2000 + 1125：恰在过零点 t=2750 耗尽
    body = three_points.get("/drift-budget-depletion", params={"from": 1000.0, "budget": 3125.0}).json()
    assert body["depleted_at"] == pytest.approx(2750.0)
    contribs = body["contributions"]
    assert [(c["t_start"], c["t_end"]) for c in contribs] == [(1000.0, 2000.0), (2000.0, 2750.0)]
    assert contribs[1]["bias_end"] == pytest.approx(0.0)
    assert contribs[1]["absolute_bias_seconds"] == pytest.approx(1125.0)


def test_drift_budget_depletion_clips_from_inside_interval(two_points):
    # 从区间内部 1500（bias 2.0）起累积：budget 562.5 恰在 t=1750 耗尽
    body = two_points.get("/drift-budget-depletion", params={"from": 1500.0, "budget": 562.5}).json()
    assert body["depleted_at"] == pytest.approx(1750.0)
    contribs = body["contributions"]
    assert [(c["t_start"], c["t_end"]) for c in contribs] == [(1500.0, 1750.0)]
    assert contribs[0]["bias_start"] == pytest.approx(2.0)
    assert contribs[0]["bias_end"] == pytest.approx(2.5)
    assert contribs[0]["absolute_bias_seconds"] == pytest.approx(562.5)


def test_drift_budget_depletion_constant_bias(client):
    # |bias| 恒为 2.0：累积退化为线性，budget 1000 在 Δt=500 处耗尽
    make_observation(client, 1000.0, 10.0, 0.5, [8.0, 8.1, 7.9])   # bias 2.0
    make_observation(client, 2000.0, 10.0, 0.5, [8.0, 8.1, 7.9])   # bias 2.0
    body = client.get("/drift-budget-depletion", params={"from": 1000.0, "budget": 1000.0}).json()
    assert body["depleted_at"] == pytest.approx(1500.0)
    assert body["consumed"] == pytest.approx(1000.0)
    assert body["remaining"] == pytest.approx(0.0)


def test_drift_budget_depletion_skips_zero_bias_interval(client):
    # 首段 bias 恒 0 不消耗预算；第二段 |bias| 0 → 2，budget 250 恰在 t=2500 耗尽
    make_observation(client, 1000.0, 10.0, 0.5, [10.0, 10.1, 9.9])  # bias 0.0
    make_observation(client, 2000.0, 10.0, 0.5, [10.0, 10.1, 9.9])  # bias 0.0
    make_observation(client, 3000.0, 10.0, 0.5, [8.0, 8.1, 7.9])    # bias 2.0
    body = client.get("/drift-budget-depletion", params={"from": 1000.0, "budget": 250.0}).json()
    assert body["depleted_at"] == pytest.approx(2500.0)
    contribs = body["contributions"]
    assert [(c["t_start"], c["t_end"]) for c in contribs] == [(1000.0, 2000.0), (2000.0, 2500.0)]
    assert contribs[0]["absolute_bias_seconds"] == pytest.approx(0.0)
    assert contribs[1]["bias_start"] == pytest.approx(0.0)
    assert contribs[1]["bias_end"] == pytest.approx(1.0)
    assert contribs[1]["absolute_bias_seconds"] == pytest.approx(250.0)


def test_drift_budget_depletion_from_at_coverage_end(two_points):
    # from 恰在覆盖终点：不再累积，预算原样剩余
    body = two_points.get("/drift-budget-depletion", params={"from": 2000.0, "budget": 100.0}).json()
    assert body["depleted_at"] is None
    assert body["consumed"] == pytest.approx(0.0)
    assert body["remaining"] == pytest.approx(100.0)
    assert body["contributions"] == []


def test_drift_budget_depletion_accepts_iso8601_from(two_points):
    resp = two_points.get("/drift-budget-depletion",
                          params={"from": "1970-01-01T00:16:40Z", "budget": 750.0})
    assert resp.status_code == 200, resp.text
    assert resp.json()["depleted_at"] == pytest.approx(1500.0)


def test_drift_budget_depletion_out_of_coverage_rejected(two_points):
    for frm in (999.0, 2001.0):
        resp = two_points.get("/drift-budget-depletion", params={"from": frm, "budget": 100.0})
        assert resp.status_code == 400
        assert "outside calibration coverage" in resp.json()["detail"]


def test_drift_budget_depletion_rejects_non_positive_budget(two_points):
    for budget in (0.0, -0.5):
        resp = two_points.get("/drift-budget-depletion", params={"from": 1000.0, "budget": budget})
        assert resp.status_code == 400
        assert "must be positive" in resp.json()["detail"]


def test_drift_budget_depletion_without_any_observation_rejected(client):
    resp = client.get("/drift-budget-depletion", params={"from": 1000.0, "budget": 100.0})
    assert resp.status_code == 400


# ---------- 漂移稳定窗口分析 ----------

def test_drift_stability_window_full_range(exceedance_points):
    # bias：1000 → 1.0，2000 → 3.0，3000 → -1.0，4000 → -3.0；max_spread=2
    # 最优窗口 [1000, 2500]：bias 1.0 → 3.0 → 1.0，波动恰为 2.0
    resp = exceedance_points.get("/drift-stability-window",
                                 params={"from": 1000.0, "to": 4000.0, "max_spread": 2.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from"] == pytest.approx(1000.0)
    assert body["to"] == pytest.approx(4000.0)
    assert body["max_spread"] == pytest.approx(2.0)
    assert body["t_start"] == pytest.approx(1000.0)
    assert body["t_end"] == pytest.approx(2500.0)
    assert body["duration_seconds"] == pytest.approx(1500.0)
    assert body["min_bias"] == pytest.approx(1.0)
    assert body["max_bias"] == pytest.approx(3.0)
    assert body["start_iso"].startswith("1970-01-01T00:16:40")
    assert body["end_iso"].startswith("1970-01-01T00:41:40")


def test_drift_stability_window_boundaries_at_analytic_crossings(client):
    # bias 0 → 10 → 0：最优窗口两端都是带宽边缘与线性段的解析交点（非观测点）
    make_observation(client, 1000.0, 10.0, 0.5, [10.0, 10.1, 9.9])   # bias 0.0
    make_observation(client, 2000.0, 10.0, 0.5, [0.0, 0.1, -0.1])    # bias 10.0
    make_observation(client, 3000.0, 10.0, 0.5, [10.0, 10.1, 9.9])   # bias 0.0
    body = client.get("/drift-stability-window",
                      params={"from": 1000.0, "to": 3000.0, "max_spread": 5.0}).json()
    # 带宽 [5, 10] 的连通分量：t=1500（b=5）到 t=2500（b=5），峰值 10 在 t=2000
    assert body["t_start"] == pytest.approx(1500.0)
    assert body["t_end"] == pytest.approx(2500.0)
    assert body["duration_seconds"] == pytest.approx(1000.0)
    assert body["min_bias"] == pytest.approx(5.0)
    assert body["max_bias"] == pytest.approx(10.0)


def test_drift_stability_window_tie_prefers_earliest_start(client):
    # bias 0 → 10 → 0 → 10 → 0：等长最优窗口 [1500,2500]（5→10→5）、
    # [2500,3500]（5→0→5）、[3500,4500]（5→10→5），时长均 1000，取起点最早者
    for ts, readings in ((1000.0, [10.0, 10.1, 9.9]), (2000.0, [0.0, 0.1, -0.1]),
                         (3000.0, [10.0, 10.1, 9.9]), (4000.0, [0.0, 0.1, -0.1]),
                         (5000.0, [10.0, 10.1, 9.9])):
        make_observation(client, ts, 10.0, 0.5, readings)
    body = client.get("/drift-stability-window",
                      params={"from": 1000.0, "to": 5000.0, "max_spread": 5.0}).json()
    assert body["t_start"] == pytest.approx(1500.0)
    assert body["t_end"] == pytest.approx(2500.0)
    assert body["duration_seconds"] == pytest.approx(1000.0)
    assert body["min_bias"] == pytest.approx(5.0)
    assert body["max_bias"] == pytest.approx(10.0)


def test_drift_stability_window_clips_window_inside_intervals(exceedance_points):
    # 窗口两端落在区间内部：[1500, 2500]，bias(1500)=2.0，峰值 3.0 在 t=2000
    # max_spread=1.5 → min ≥ 1.5，右边界为 b=1.5 的解析交点 t=2375，左边界被窗口裁在 1500
    body = exceedance_points.get("/drift-stability-window",
                                 params={"from": 1500.0, "to": 2500.0, "max_spread": 1.5}).json()
    assert body["t_start"] == pytest.approx(1500.0)
    assert body["t_end"] == pytest.approx(2375.0)
    assert body["duration_seconds"] == pytest.approx(875.0)
    assert body["min_bias"] == pytest.approx(1.5)
    assert body["max_bias"] == pytest.approx(3.0)


def test_drift_stability_window_whole_window_when_spread_covers(two_points):
    # bias 1.0 → 3.0，全程波动 2.0 恰等于 max_spread：整个窗口即最优
    body = two_points.get("/drift-stability-window",
                          params={"from": 1000.0, "to": 2000.0, "max_spread": 2.0}).json()
    assert body["t_start"] == pytest.approx(1000.0)
    assert body["t_end"] == pytest.approx(2000.0)
    assert body["duration_seconds"] == pytest.approx(1000.0)
    assert body["min_bias"] == pytest.approx(1.0)
    assert body["max_bias"] == pytest.approx(3.0)


def test_drift_stability_window_constant_bias(client):
    # bias 恒为 2.0：波动为 0，任意 max_spread 下整个窗口稳定
    make_observation(client, 1000.0, 10.0, 0.5, [8.0, 8.1, 7.9])   # bias 2.0
    make_observation(client, 2000.0, 10.0, 0.5, [8.0, 8.1, 7.9])   # bias 2.0
    body = client.get("/drift-stability-window",
                      params={"from": 1000.0, "to": 2000.0, "max_spread": 0.5}).json()
    assert body["t_start"] == pytest.approx(1000.0)
    assert body["t_end"] == pytest.approx(2000.0)
    assert body["duration_seconds"] == pytest.approx(1000.0)
    assert body["min_bias"] == pytest.approx(2.0)
    assert body["max_bias"] == pytest.approx(2.0)


def test_drift_stability_window_accepts_iso8601_bounds(exceedance_points):
    resp = exceedance_points.get(
        "/drift-stability-window",
        params={"from": "1970-01-01T00:16:40Z", "to": "1970-01-01T01:06:40Z", "max_spread": 2.0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["t_start"] == pytest.approx(1000.0)
    assert body["t_end"] == pytest.approx(2500.0)


def test_drift_stability_window_rejects_inverted_or_empty_window(exceedance_points):
    for params in ({"from": 2000.0, "to": 2000.0, "max_spread": 2.0},
                   {"from": 3000.0, "to": 1000.0, "max_spread": 2.0}):
        resp = exceedance_points.get("/drift-stability-window", params=params)
        assert resp.status_code == 400
        assert "must be less than" in resp.json()["detail"]


def test_drift_stability_window_rejects_non_positive_spread(exceedance_points):
    for spread in (0.0, -0.5):
        resp = exceedance_points.get("/drift-stability-window",
                                     params={"from": 1000.0, "to": 4000.0, "max_spread": spread})
        assert resp.status_code == 400
        assert "must be positive" in resp.json()["detail"]


def test_drift_stability_window_out_of_coverage_rejected(exceedance_points):
    for params in ({"from": 999.0, "to": 2000.0, "max_spread": 2.0},
                   {"from": 2000.0, "to": 4001.0, "max_spread": 2.0}):
        resp = exceedance_points.get("/drift-stability-window", params=params)
        assert resp.status_code == 400
        assert "outside calibration coverage" in resp.json()["detail"]


def test_drift_stability_window_without_any_observation_rejected(client):
    resp = client.get("/drift-stability-window", params={"from": 1.0, "to": 2.0, "max_spread": 2.0})
    assert resp.status_code == 400


# ---------- 观测影响预演 ----------

def preview(client, ts, reference, tolerance, readings, impact_limit):
    return client.post(
        "/observation-impact-preview",
        json={"timestamp": ts, "reference": reference, "tolerance": tolerance,
              "readings": readings, "impact_limit": impact_limit},
    )


def test_impact_preview_basic(two_points):
    # 当前曲线：1000(bias 1.0) → 2000(bias 3.0)；候选 1500 处 bias 4.0，当前插值 2.0
    resp = preview(two_points, 1500.0, 10.0, 0.5, [6.0, 6.1, 5.9], 0.5)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["timestamp"] == pytest.approx(1500.0)
    assert body["timestamp_iso"].startswith("1970-01-01T00:25:00")
    assert body["impact_limit"] == pytest.approx(0.5)
    assert body["candidate_bias"] == pytest.approx(4.0)
    assert body["current_bias"] == pytest.approx(2.0)
    # 受影响范围即左右最近观测之间
    assert body["affected_from"] == pytest.approx(1000.0)
    assert body["affected_to"] == pytest.approx(2000.0)
    assert body["affected_from_iso"].startswith("1970-01-01T00:16:40")
    assert body["affected_to_iso"].startswith("1970-01-01T00:33:20")
    # Δ：0 → 2.0 → 0，最大绝对变化 2.0，最早（唯一）发生于候选点
    assert body["max_abs_change"] == pytest.approx(2.0)
    assert body["max_change_at"] == pytest.approx(1500.0)
    assert body["max_change_at_iso"].startswith("1970-01-01T00:25:00")
    # |Δ| > 0.5：左交点 1000 + 0.5/2×500 = 1125，右交点 2000 − 125 = 1875，跨候选点合并为一段
    intervals = body["impact_intervals"]
    assert len(intervals) == 1
    assert intervals[0]["t_start"] == pytest.approx(1125.0)
    assert intervals[0]["t_end"] == pytest.approx(1875.0)
    assert intervals[0]["duration_seconds"] == pytest.approx(750.0)
    assert intervals[0]["start_iso"].startswith("1970-01-01T00:18:45")
    assert intervals[0]["end_iso"].startswith("1970-01-01T00:31:15")


def test_impact_preview_candidate_bias_uses_outlier_marking(two_points):
    # 20.0 偏离组中位数 6.05 超差判为异常；候选偏差按有效读数中位数 6.0 计算
    body = preview(two_points, 1500.0, 10.0, 0.5, [6.0, 6.1, 5.9, 20.0], 0.5).json()
    assert body["candidate_bias"] == pytest.approx(4.0)


def test_impact_preview_negative_change(two_points):
    # 候选 bias 0.5 低于当前插值 2.0：Δc = −1.5，最大绝对变化 1.5
    body = preview(two_points, 1500.0, 10.0, 0.5, [9.5, 9.6, 9.4], 0.5).json()
    assert body["candidate_bias"] == pytest.approx(0.5)
    assert body["current_bias"] == pytest.approx(2.0)
    assert body["max_abs_change"] == pytest.approx(1.5)
    assert body["max_change_at"] == pytest.approx(1500.0)
    # 交点 1000 + 0.5/1.5×500 与 2000 − 0.5/1.5×500
    (interval,) = body["impact_intervals"]
    assert interval["t_start"] == pytest.approx(1000.0 + 500.0 / 3.0)
    assert interval["t_end"] == pytest.approx(2000.0 - 500.0 / 3.0)
    assert interval["duration_seconds"] == pytest.approx(1000.0 - 1000.0 / 3.0)


def test_impact_preview_no_change_when_candidate_matches_curve(two_points):
    # 候选 bias 恰等于当前插值 2.0：曲线不变，最大变化为 0，
    # 全程并列最大，最早发生时点为受影响范围起点
    body = preview(two_points, 1500.0, 10.0, 0.5, [8.0, 8.1, 7.9], 0.1).json()
    assert body["candidate_bias"] == pytest.approx(2.0)
    assert body["max_abs_change"] == pytest.approx(0.0)
    assert body["max_change_at"] == pytest.approx(1000.0)
    assert body["impact_intervals"] == []


def test_impact_preview_threshold_exactly_at_peak_not_counted(two_points):
    # impact_limit 恰等于最大变化（严格大于才计入）：无影响区间
    body = preview(two_points, 1500.0, 10.0, 0.5, [6.0, 6.1, 5.9], 2.0).json()
    assert body["max_abs_change"] == pytest.approx(2.0)
    assert body["impact_intervals"] == []


def test_impact_preview_uses_immediate_neighbors_only(client):
    # 三点链：候选落在 (2000, 3000) 内，受影响范围不含更远的观测
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])    # bias 1.0
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])    # bias 3.0
    make_observation(client, 3000.0, 10.0, 0.5, [6.0, 6.1, 5.9])    # bias 4.0
    body = preview(client, 2500.0, 10.0, 0.5, [4.0, 4.1, 3.9], 0.5).json()
    assert body["affected_from"] == pytest.approx(2000.0)
    assert body["affected_to"] == pytest.approx(3000.0)
    assert body["current_bias"] == pytest.approx(3.5)
    assert body["candidate_bias"] == pytest.approx(6.0)
    assert body["max_abs_change"] == pytest.approx(2.5)
    assert body["max_change_at"] == pytest.approx(2500.0)
    # 左交点 2000 + 0.5/2.5×500 = 2100，右交点 3000 − 100 = 2900
    (interval,) = body["impact_intervals"]
    assert interval["t_start"] == pytest.approx(2100.0)
    assert interval["t_end"] == pytest.approx(2900.0)


def test_impact_preview_occupied_timestamp_conflicts(two_points):
    resp = preview(two_points, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9], 0.5)
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


def test_impact_preview_outside_coverage_conflicts(two_points):
    # 候选时点只有单侧邻点（覆盖范围之外）
    for ts in (999.9, 2000.1):
        resp = preview(two_points, ts, 10.0, 0.5, [9.0, 9.1, 8.9], 0.5)
        assert resp.status_code == 409
        assert "both sides" in resp.json()["detail"]


def test_impact_preview_with_single_observation_conflicts(client):
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])
    for ts in (500.0, 1500.0):
        assert preview(client, ts, 10.0, 0.5, [9.0, 9.1, 8.9], 0.5).status_code == 409


def test_impact_preview_without_any_observation_conflicts(client):
    assert preview(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9], 0.5).status_code == 409


def test_impact_preview_rejects_non_positive_limit(two_points):
    for limit in (0.0, -0.5):
        resp = preview(two_points, 1500.0, 10.0, 0.5, [6.0, 6.1, 5.9], limit)
        assert resp.status_code == 400
        assert "must be positive" in resp.json()["detail"]


def test_impact_preview_invalid_readings_returns_422(two_points):
    resp = preview(two_points, 1500.0, 10.0, 0.0, [1.0, 2.0, 3.0, 4.0], 0.5)
    assert resp.status_code == 422


def test_impact_preview_accepts_iso8601_timestamp(two_points):
    resp = preview(two_points, "1970-01-01T00:25:00Z", 10.0, 0.5, [6.0, 6.1, 5.9], 0.5)
    assert resp.status_code == 200, resp.text
    assert resp.json()["timestamp"] == pytest.approx(1500.0)


def test_impact_preview_is_read_only(client, clock):
    clock[0] = 100.0
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9, 20.0])
    clock[0] = 200.0
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])
    clock[0] = 300.0
    revise_observation(client, 1, 1000.0, 10.0, 0.5, [10.0, 10.1, 9.9], revision=1)

    before = (
        client.get("/observations").json(),
        client.get("/intervals").json(),
        client.get("/observations/1/revisions").json(),
    )
    # 成功与失败（409/400）的预演都不得改动任何现状数据
    assert preview(client, 1500.0, 10.0, 0.5, [6.0, 6.1, 5.9], 0.5).status_code == 200
    assert preview(client, 500.0, 10.0, 0.5, [6.0, 6.1, 5.9], 0.5).status_code == 409
    assert preview(client, 1500.0, 10.0, 0.5, [6.0, 6.1, 5.9], 0.0).status_code == 400
    after = (
        client.get("/observations").json(),
        client.get("/intervals").json(),
        client.get("/observations/1/revisions").json(),
    )
    assert after == before
