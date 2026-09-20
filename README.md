# 仪器校准漂移补偿 API

基于 **FastAPI + SQLite** 的仪器校准服务：客户端在各时点提交参考值与同点多次读数，服务标记异常读数、计算校准偏差，并在相邻时点间线性插值偏差，对待校正读数施加漂移补偿。

## 快速开始（单条命令）

```bash
docker compose up --build
```

- **API 地址：http://localhost:8000**
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

SQLite 数据落在命名卷 `calibration-data`（容器内 `/data/calibration.db`），容器重启数据不丢。

## 校准规则

1. **观测登记**：每次提交参考值 `reference`、容差 `tolerance`、同一时点 `timestamp` 的至少 3 次读数。
2. **异常判定**：读数偏离该组**中位数**超过 `tolerance` 即标记为异常（原始读数仍完整保留）。
3. **校准偏差**：`bias = reference − 有效读数（剔除异常后）的中位数`。
4. **漂移补偿**：在相邻观测时点间对 `bias` 做**线性插值**，`校正读数 = 原始读数 + 插值偏差`。
5. **区间重算**：新增/修订观测只重算受影响的相邻区间（原位置缺口重新接通、新位置区间被时点拆分），其余历史区间与原始数据不变。
6. **覆盖范围**：查询时点超出 `[最早观测, 最晚观测]` 时拒绝（HTTP 400）；恰落在观测点上时直接使用该点偏差。
7. **修订与版本**：观测带单调递增的 `revision`（新建为 1）。`PUT` 修订须携带当前 `revision`，版本过期或目标时点已被占用均返回 409；成功后观测 ID 不变、版本 +1，并保存一份修订前快照（旧输入与旧偏差），可通过修订历史接口按版本倒序查询。
8. **累计漂移量**：`GET /drift-integral?from=&to=` 在覆盖范围内对当前分段线性 `bias` 做**解析积分**（精确梯形面积；段内 `bias` 过零时按解析交点拆分绝对值面积，不做固定步长采样），返回 `signed_bias_seconds`、`absolute_bias_seconds` 及按插值区间裁剪后的分段 `contributions`。窗口边界可落在区间内部；`from` 必须小于 `to`，任一边界越界返回 400。
9. **漂移超限窗口分析**：`GET /drift-exceedances?from=&to=&limit=` 在覆盖范围内**解析求解**分段线性 `bias` 与 `±limit` 的交点（不做固定步长采样），返回 `|bias| > limit` 的连续区间，每段含方向（`high`/`low`）、持续秒数与区间内峰值绝对偏差；跨观测点仍连续且同向的片段合并为一段，恰等于阈值的瞬时不计入超限（观测点恰好触及阈值时两侧不合并）。`from` 必须小于 `to`、`limit` 必须为正数，任一边界越界返回 400。
10. **漂移预算耗尽分析**：`GET /drift-budget-depletion?from=&budget=` 从 `from` 起沿当前分段线性 `bias` 累积 `|bias|·秒`，**解析求出**预算首次耗尽的精确时点：段内 `bias` 过零时先在解析零点拆分（`|bias|` 仅在拆分后的子段上才是线性函数），目标面积落入某子段时反解其累积二次式，不做固定步长采样。返回 `depleted_at`（覆盖范围内未耗尽为 `null`）、截至耗尽点（未耗尽时为覆盖终点）的 `consumed`/`remaining`，以及按插值区间裁剪的分段 `contributions`。`budget` 必须为正数，`from` 越出覆盖范围返回 400。
11. **漂移稳定窗口分析**：`GET /drift-stability-window?from=&to=&max_spread=` 在覆盖范围内**解析求出** `max(bias) − min(bias) ≤ max_spread` 的持续时间最长的连续子区间：最优窗口必是某临界带宽 `[m, m+max_spread]` 的连通分量（`m` 取遍折点取值及其下移 `max_spread` 的值），分量边界由线性段与带宽边缘的解析交点精确定位，可落在插值段内部，不做固定步长采样；时长并列时返回起点最早者。响应含起止时刻、`duration_seconds` 与窗口内实际达到的 `min_bias`/`max_bias`。`from` 必须小于 `to`、`max_spread` 必须为正数，任一边界越界返回 400。
12. **历史时点漂移补偿**：`GET /historical-correction?effective_at=&as_of=&reading=` 按 `as_of` 系统时点重建校准状态后，对目标观测时点 `effective_at` 的读数做漂移补偿。重建依据 `observations.created_at`、当前版本与 `revisions` 快照：`created_at > as_of` 的观测尚未创建、不参与；修订自 `changed_at` 起生效（恰等于 `changed_at` 时使用新版本），`changed_at > as_of` 的修订一律回滚到快照旧值。随后按重建出的观测时点排序，沿用与 `/corrections` 相同的覆盖范围与线性插值规则。查询**只读**，不改写当前观测、修订历史或区间表；响应附带 `as_of` 及所用端点的观测 ID 与版本。
13. **观测影响预演**：`POST /observation-impact-preview` 接收一条**不落库**的候选观测与正数 `impact_limit`，沿用现有异常判定算出候选偏差，预演插入对当前分段线性偏差曲线的影响。仅在候选时点两侧都已有观测时预演（候选时点已占用或无法形成双侧邻点均返回 409）：插入只会把左右最近观测之间的区间重新分段，受影响范围即 `[prev, next]`；曲线变化量 Δ 是折点 `(prev, 0) → (候选, Δc) → (next, 0)` 的分段线性函数，返回受影响时间范围、最大绝对偏差变化及其最早发生时点，以及 `|Δ| > impact_limit` 的连续区间（阈值交点解析求解，恰等于阈值的瞬时不计入，不做固定步长采样）。请求**只读**，不改写观测、修订历史或区间表。
14. **校正回执**：`POST /correction-receipts` 复用 `/corrections` 的漂移补偿，并对结果签发版本化 HMAC 回执：当前新签发为 **v2**，携带 `key_id`（`CORRECTION_RECEIPT_ACTIVE_KEY` 指定的当前签发键）、归一化请求（`timestamp` 统一为 Unix 秒）、`bias`、`corrected`、`issued_at`（UTC Unix 毫秒整数）与 `receipt`。v2 签名载荷按 `version`、`key_id`、`timestamp`、`reading`、`bias`、`corrected`、`issued_at` 顺序组成 JSON 数组，数值取 JSON 最短十进制、UTF-8 编码且无任何空白，再以 `CORRECTION_RECEIPT_KEYS` 中该 `key_id` 对应的密钥计算 HMAC-SHA256，输出小写十六进制。`POST /correction-receipts/verify` 接收完整回执，重算签名并以**常量时间**比较：v2 **严格按回执中的 `key_id` 精确选键**，`key_id` 未知或密钥已从键环移除（含缺失）均返回 `{"valid": false}`，因此轮换时保留旧键即可让轮换前回执继续验证，退役旧键后其回执立即失效；服务**继续接受 v1 回执**（载荷为 `[version,timestamp,reading,bias,corrected,issued_at]`），验签沿用旧的 `CORRECTION_RECEIPT_SECRET` 单密钥规则，与现行键环无关。任一签名字段被改动返回 `{"valid": false}`；结构缺失或类型错误（如字符串冒充数值、`issued_at` 为小数）返回 422。密钥环在**启动时**严格校验：空键环、`CORRECTION_RECEIPT_ACTIVE_KEY` 不在键环中、非法 `key_id`（非空、1–64 字符、仅 `[A-Za-z0-9_-]`）均拒绝启动。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/observations` | 登记校准观测，返回偏差与异常明细（201） |
| GET | `/observations` | 列出全部观测（含原始读数与异常标记） |
| PUT | `/observations/{id}` | 修订观测（乐观锁版本校验，409 冲突；ID 不变，版本 +1） |
| GET | `/observations/{id}/revisions` | 按版本倒序列出修订前快照（旧输入与旧偏差） |
| POST | `/observation-impact-preview` | 预演插入一条观测（不落库）对偏差曲线的影响：受影响范围、最大绝对变化与超阈值区间 |
| GET | `/intervals` | 列出当前插值区间 |
| POST | `/corrections` | 对指定时点的读数做漂移补偿 |
| POST | `/correction-receipts` | 漂移补偿 + 签发版本化 HMAC-SHA256 校正回执 |
| POST | `/correction-receipts/verify` | 常量时间校验完整回执：字段被改动 valid=false，结构/类型错误 422 |
| GET | `/historical-correction` | 按 `as_of` 系统时点重建校准状态，对目标时点读数做漂移补偿（只读） |
| GET | `/drift-integral` | 对覆盖范围内分段线性偏差做精确积分（累计漂移量） |
| GET | `/drift-exceedances` | 解析求解偏差越出 ±limit 的连续区间（方向/持续秒数/峰值） |
| GET | `/drift-budget-depletion` | 解析求解漂移预算（\|bias\|·秒）首次耗尽的精确时点 |
| GET | `/drift-stability-window` | 解析求解 max(bias) − min(bias) ≤ max_spread 的最长连续稳定子区间 |
| GET | `/health` | 健康检查 |

时间戳支持 Unix 秒（数字）或 ISO 8601 字符串（如 `2026-09-18T08:00:00Z`），缺省为服务器当前时间。

### 登记观测

```bash
curl -X POST http://localhost:8000/observations \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T08:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [99.0, 99.2, 98.8, 150.0]}'
```

响应（`150.0` 偏离组中位数 `99.1` 超差，判为异常；`bias = 100.0 − 99.0 = 1.0`）：

```json
{
  "id": 1, "revision": 1, "timestamp": 1789718400.0, "timestamp_iso": "2026-09-18T08:00:00+00:00",
  "reference": 100.0, "tolerance": 0.5, "group_median": 99.1, "bias": 1.0,
  "readings": [
    {"value": 99.0, "is_outlier": false}, {"value": 99.2, "is_outlier": false},
    {"value": 98.8, "is_outlier": false}, {"value": 150.0, "is_outlier": true}
  ],
  "outliers": [150.0]
}
```

### 修订观测

修订请求体与登记一致（`reference`、`tolerance`、`readings`、`timestamp`），额外携带客户端持有的当前 `revision`。服务在单个事务内重新判定异常、重算偏差，并重建**原位置与新位置**受影响的相邻插值区间；观测 ID 保持不变。版本不匹配（已被他人修订）或目标时间戳已被其它观测占用时返回 409，数据不变。

```bash
curl -X PUT http://localhost:8000/observations/1 \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T08:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [99.1, 99.0, 99.2], "revision": 1}'
```

响应与登记观测相同，`revision` 推进为 `2`：

```json
{
  "id": 1, "revision": 2, "timestamp": 1789718400.0,
  "timestamp_iso": "2026-09-18T08:00:00+00:00",
  "reference": 100.0, "tolerance": 0.5, "group_median": 99.1, "bias": 0.9,
  "readings": [
    {"value": 99.1, "is_outlier": false}, {"value": 99.0, "is_outlier": false},
    {"value": 99.2, "is_outlier": false}
  ],
  "outliers": []
}
```

版本冲突（或时间戳冲突）响应：

```json
{"detail": "observation 1 is at revision 2, not 1; fetch the latest observation and retry"}
```

### 查询修订历史

每次成功修订都会先保存一份修订前快照。按版本倒序返回变更时间、旧输入（旧时点、旧参考值、旧容差、旧读数）与旧偏差：

```bash
curl http://localhost:8000/observations/1/revisions
```

```json
[
  {
    "revision": 1,
    "changed_at": 1789720200.0, "changed_at_iso": "2026-09-18T08:30:00+00:00",
    "old_timestamp": 1789718400.0, "old_timestamp_iso": "2026-09-18T08:00:00+00:00",
    "old_reference": 100.0, "old_tolerance": 0.5, "old_bias": 1.0,
    "old_readings": [99.0, 99.2, 98.8, 150.0]
  }
]
```

从未修订过的观测返回空数组 `[]`；观测 ID 不存在时两个接口均返回 404。

### 观测影响预演

`POST /observation-impact-preview` 回答“如果现在插入这条观测，偏差曲线会发生什么变化”，但**不落库**：候选观测沿用与登记观测相同的异常判定算出偏差，仅在候选时点两侧都已有观测时预演。插入只会把左右最近观测之间的区间重新分段，受影响范围即 `[prev, next]`；曲线变化量 Δ 是折点 `(prev, 0) → (候选, Δc) → (next, 0)` 的分段线性函数。响应给出受影响时间范围、最大绝对偏差变化 `max_abs_change` 及其最早发生时点 `max_change_at`，并按时间列出 `|Δ| > impact_limit` 的连续区间——阈值交点由线性段解析求解，不做固定步长采样，恰等于阈值的瞬时不计入。请求只读，不改写观测、修订历史或区间表。

沿用漂移补偿一节的场景（08:00 `bias 1.0`、10:00 `bias 3.0`），预演在 09:00 插入一条 `bias 4.5` 的观测（当前曲线在 09:00 的插值为 `2.0`，故 Δc = 2.5）：

```bash
curl -X POST http://localhost:8000/observation-impact-preview \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T09:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [95.4, 95.5, 95.6], "impact_limit": 1.0}'
```

```json
{
  "timestamp": 1789722000.0, "timestamp_iso": "2026-09-18T09:00:00+00:00",
  "impact_limit": 1.0,
  "candidate_bias": 4.5, "current_bias": 2.0,
  "affected_from": 1789718400.0, "affected_from_iso": "2026-09-18T08:00:00+00:00",
  "affected_to": 1789725600.0, "affected_to_iso": "2026-09-18T10:00:00+00:00",
  "max_abs_change": 2.5,
  "max_change_at": 1789722000.0, "max_change_at_iso": "2026-09-18T09:00:00+00:00",
  "impact_intervals": [
    {"t_start": 1789719840.0, "t_end": 1789724160.0,
     "start_iso": "2026-09-18T08:24:00+00:00", "end_iso": "2026-09-18T09:36:00+00:00",
     "duration_seconds": 4320.0}
  ]
}
```

`|Δ| > 1.0` 的区间两端（08:24、09:36）即变化量曲线与阈值 `1.0` 的解析交点；候选偏差恰等于当前插值时 `max_abs_change` 为 `0`、`impact_intervals` 为空。候选时点已被占用、或候选时点落在最早/最晚观测之外（无法形成双侧邻点）时返回 409；`impact_limit ≤ 0` 返回 400。

### 漂移补偿

```bash
curl -X POST http://localhost:8000/corrections \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T09:00:00Z", "reading": 98.0}'
```

响应包含校正值、所用区间及两端点观测的异常明细：

```json
{
  "timestamp": 1789722000.0, "timestamp_iso": "2026-09-18T09:00:00+00:00",
  "reading": 98.0, "bias": 2.0, "corrected": 100.0,
  "interval": {"t_start": 1789718400.0, "t_end": 1789725600.0,
               "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
               "bias_start": 1.0, "bias_end": 3.0},
  "endpoints": [
    {"observation_id": 1, "timestamp": 1789718400.0, "timestamp_iso": "2026-09-18T08:00:00+00:00",
     "bias": 1.0, "outliers": [150.0]},
    {"observation_id": 2, "timestamp": 1789725600.0, "timestamp_iso": "2026-09-18T10:00:00+00:00",
     "bias": 3.0, "outliers": []}
  ]
}
```

### 校正回执（HMAC 签名）

`POST /correction-receipts` 的请求体与 `/corrections` 相同，内部**完全复用**漂移补偿（同样的覆盖范围判定与线性插值，越界返回 400），但只回传校正结果与一份可离线校验的签名回执，不携带区间/端点明细：

```bash
curl -X POST http://localhost:8000/correction-receipts \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T09:00:00Z", "reading": 98.0}'
```

```json
{
  "version": 2,
  "key_id": "2026-09",
  "request": {"timestamp": 1789722000.0, "reading": 98.0},
  "bias": 2.0,
  "corrected": 100.0,
  "issued_at": 1789844210712,
  "receipt": "b15e3aec42107a3959ccd65012fa6124e1f82c3742be43ae7d3a897299aad313"
}
```

- `version`：回执格式版本，当前新签发固定为 `2`；`version=1` 的历史回执仍可被校验（见下文）。
- `key_id`：签发所用密钥在密钥环 `CORRECTION_RECEIPT_KEYS` 中的键名，即签发时的 `CORRECTION_RECEIPT_ACTIVE_KEY`。验签严格按它**精确选键**：键环中找不到该 `key_id`（未知或轮换后已移除）一律 `valid=false`。
- `request`：**归一化请求**——ISO 8601/数字字符串等时间格式统一为 Unix 秒数值，`reading` 为数值。
- `issued_at`：签发时刻，UTC Unix **毫秒整数**。
- `receipt`：HMAC-SHA256 小写十六进制摘要（64 个字符）。v2 签名载荷是把 `version`、`key_id`、`timestamp`、`reading`、`bias`、`corrected`、`issued_at` 按此顺序组成的 **JSON 数组**，数值取 JSON 最短十进制（如上例即 `[2,"2026-09",1789722000.0,98.0,2.0,100.0,1789844210712]`），以 UTF-8 编码、不含任何空白后参与 HMAC。

校验时把**完整回执原样**回传：

```bash
curl -X POST http://localhost:8000/correction-receipts/verify \
  -H 'Content-Type: application/json' \
  -d '{"version":2,"key_id":"2026-09","request":{"timestamp":1789722000.0,"reading":98.0},
       "bias":2.0,"corrected":100.0,"issued_at":1789844210712,
       "receipt":"b15e3aec42107a3959ccd65012fa6124e1f82c3742be43ae7d3a897299aad313"}'
# {"valid":true}
```

任一签名字段（含 `version`、`key_id`、`request.timestamp`、`request.reading`、`bias`、`corrected`、`issued_at`、`receipt`）被改动，均返回 `{"valid": false}`；回执声明的 `key_id` 在当前键环中不存在（未知或已移除）同样是 `{"valid": false}`。校验只重算签名做常量时间比较，不会因数据库中的校准状态变化而失效。字段缺失或类型错误（例如时间戳传字符串、`issued_at` 传小数、`key_id` 传数字、`receipt` 传数字、整体不是 JSON 对象）一律返回 **422**；v2 回执缺少 `key_id` 字段属签名失效，返回 `{"valid": false}`。未签名的额外字段不影响校验结果。

**v1 历史回执兼容**：升级前签发的 `version=1` 回执（无 `key_id` 字段，签名载荷为 `[1,timestamp,reading,bias,corrected,issued_at]`）继续被接受，密钥一律取自旧配置 `CORRECTION_RECEIPT_SECRET`（未设置时为开发默认值），与现行密钥环互不影响；即使键环中的密钥已轮换或移除，只要旧密钥未改，v1 回执仍可验证。反之，伪造或篡改的 v1 回执（如把 `version` 改成 2 却不提供匹配的 v2 签名）一律 `valid=false`。

### 回执密钥轮换

密钥环通过两个环境变量配置，启动时严格校验，配置非法则**拒绝启动**（不使用带病配置运行）：

- `CORRECTION_RECEIPT_KEYS`：JSON 对象，`key_id` → 密钥字符串的映射。`key_id` 必须非空、1–64 字符且仅含 `[A-Za-z0-9_-]`（如日期段 `2026-09`）；密钥必须是非空字符串，映射不允许为空对象。
- `CORRECTION_RECEIPT_ACTIVE_KEY`：当前**签发**键的 `key_id`，必须是密钥环中的成员，否则启动失败。
- 两个变量都未设置时回退旧版单密钥模式（见下文），`docker compose up` 无需任何额外配置。

典型轮换流程——**先并存、再切换、后退役**：

```bash
# 1) 生成新密钥，把新旧密钥同时放入键环（此步不切换签发键，重启后行为不变）
CORRECTION_RECEIPT_KEYS='{"2026-03":"<old-secret>","2026-09":"<new-secret>"}' \
CORRECTION_RECEIPT_ACTIVE_KEY=2026-03 \
docker compose up -d

# 2) 把签发键切到新 key_id；旧键保留在键环中，轮换前的回执照常验证通过
CORRECTION_RECEIPT_KEYS='{"2026-03":"<old-secret>","2026-09":"<new-secret>"}' \
CORRECTION_RECEIPT_ACTIVE_KEY=2026-09 \
docker compose up -d

# 3) 待旧回执全部到期后，从键环移除旧键；此后用 2026-03 签名的回执立即 valid=false
CORRECTION_RECEIPT_KEYS='{"2026-09":"<new-secret>"}' \
CORRECTION_RECEIPT_ACTIVE_KEY=2026-09 \
docker compose up -d
```

第 2 步之后：

```bash
# 新回执以 2026-09 签发；旧回执（key_id=2026-03）回传验签仍为 true
curl -X POST http://localhost:8000/correction-receipts \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": 1789722000.0, "reading": 98.0}'
# {"version":2,"key_id":"2026-09",...}
```

兼容旧版单密钥部署（或只做快速本地验证）也可以不配置密钥环：仅设置 `CORRECTION_RECEIPT_SECRET` 时，服务合成一个只含 `legacy` 键的键环，新回执为 `version=2、key_id="legacy"`，签名密钥即该 secret；升级前签发的 `version=1` 回执同样继续可验。

### 历史时点漂移补偿（as_of 状态重建）

`GET /historical-correction?effective_at=&as_of=&reading=` 回答“以 `as_of` 系统时点所能看到的校准状态，如何校正 `effective_at` 时点的读数”。服务用 `observations.created_at`、当前版本与 `revisions` 快照重建 `as_of` 状态：尚未创建的观测不参与；修订自 `changed_at` 起生效（恰等于变更时刻用新版本），晚于 `as_of` 的修订回滚到快照旧值。随后按重建出的观测时点排序，套用与 `/corrections` 完全相同的覆盖范围与线性插值规则。查询只读，不改写当前观测、修订历史或区间表。

沿用上文场景：两条观测（08:00 `bias 1.0`、10:00 `bias 3.0`）均已在 08:15 前登记，观测 1 的修订（`bias 1.0 → 0.9`，版本 1 → 2）于 08:30 生效。查询 09:00 的读数 `98.0`：

```bash
# as_of 在修订之前：看到旧版本（revision 1，bias 1.0）
curl "http://localhost:8000/historical-correction?effective_at=2026-09-18T09:00:00Z&as_of=2026-09-18T08:15:00Z&reading=98.0"
```

```json
{
  "effective_at": 1789722000.0, "effective_at_iso": "2026-09-18T09:00:00+00:00",
  "as_of": 1789719300.0, "as_of_iso": "2026-09-18T08:15:00+00:00",
  "reading": 98.0, "bias": 2.0, "corrected": 100.0,
  "interval": {"t_start": 1789718400.0, "t_end": 1789725600.0,
               "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
               "bias_start": 1.0, "bias_end": 3.0},
  "endpoints": [
    {"observation_id": 1, "revision": 1, "timestamp": 1789718400.0,
     "timestamp_iso": "2026-09-18T08:00:00+00:00", "bias": 1.0},
    {"observation_id": 2, "revision": 1, "timestamp": 1789725600.0,
     "timestamp_iso": "2026-09-18T10:00:00+00:00", "bias": 3.0}
  ]
}
```

```bash
# as_of 在修订之后：看到新版本（revision 2，bias 0.9），中点插值变为 1.95
curl "http://localhost:8000/historical-correction?effective_at=2026-09-18T09:00:00Z&as_of=2026-09-18T08:45:00Z&reading=98.0"
```

```json
{
  "effective_at": 1789722000.0, "effective_at_iso": "2026-09-18T09:00:00+00:00",
  "as_of": 1789721100.0, "as_of_iso": "2026-09-18T08:45:00+00:00",
  "reading": 98.0, "bias": 1.95, "corrected": 99.95,
  "interval": {"t_start": 1789718400.0, "t_end": 1789725600.0,
               "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
               "bias_start": 0.9, "bias_end": 3.0},
  "endpoints": [
    {"observation_id": 1, "revision": 2, "timestamp": 1789718400.0,
     "timestamp_iso": "2026-09-18T08:00:00+00:00", "bias": 0.9},
    {"observation_id": 2, "revision": 1, "timestamp": 1789725600.0,
     "timestamp_iso": "2026-09-18T10:00:00+00:00", "bias": 3.0}
  ]
}
```

`effective_at` 越出重建后的覆盖范围（含 `as_of` 时无任何观测）时返回 400；恰落在重建后的观测点上时直接使用该点偏差（`interval` 为 `null`）。

### 累计漂移量积分

对 `[from, to]` 窗口内的分段线性 `bias` 做解析积分。窗口边界可落在区间内部（按插值裁剪）；段内 `bias` 过零时，绝对值面积在解析零点处拆成两个三角形求和，全程无固定步长采样。各分段贡献按时间排序，其 `signed`/`absolute` 面积之和分别等于汇总值。

```bash
curl "http://localhost:8000/drift-integral?from=2026-09-18T08:00:00Z&to=2026-09-18T10:00:00Z"
```

响应（沿用上文 08:00 `bias 1.0` → 10:00 `bias 3.0` 的区间，梯形面积 `(1+3)/2 × 7200s`）：

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "to": 1789725600.0, "to_iso": "2026-09-18T10:00:00+00:00",
  "signed_bias_seconds": 14400.0,
  "absolute_bias_seconds": 14400.0,
  "contributions": [
    {"t_start": 1789718400.0, "t_end": 1789725600.0,
     "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
     "bias_start": 1.0, "bias_end": 3.0,
     "signed_bias_seconds": 14400.0, "absolute_bias_seconds": 14400.0}
  ]
}
```

若段内 `bias` 变号（如 `2.0 → −2.0`），`signed_bias_seconds` 中正负面积相互抵消（此处为 0），`absolute_bias_seconds` 则为零点两侧两个三角形面积之和（此处为 `7200.0`）。

### 漂移超限窗口分析

对 `[from, to]` 窗口内的分段线性 `bias` 解析求解与 `±limit` 的交点，返回 `|bias| > limit` 的连续区间。每段给出方向（`high`：`bias > limit`；`low`：`bias < −limit`）、持续秒数与区间内峰值绝对偏差；跨观测点仍连续且同向的片段合并为一段，恰等于阈值的瞬时不计入超限（观测点恰好触及阈值时，两侧超限不合并）。全程解析解，不做固定步长采样。

沿用上文观测，再补一条 12:00 的观测（`bias = −3.0`）：

```bash
curl -X POST http://localhost:8000/observations \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T12:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [103.0, 103.1, 102.9]}'

curl "http://localhost:8000/drift-exceedances?from=2026-09-18T08:00:00Z&to=2026-09-18T12:00:00Z&limit=2.0"
```

响应（`bias` 1.0 → 3.0 → −3.0：09:00 越上阈、10:20 回落，两段跨 10:00 观测点合并；11:40 跌破下阈）：

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "to": 1789732800.0, "to_iso": "2026-09-18T12:00:00+00:00",
  "limit": 2.0,
  "exceedances": [
    {"t_start": 1789722000.0, "t_end": 1789726800.0,
     "start_iso": "2026-09-18T09:00:00+00:00", "end_iso": "2026-09-18T10:20:00+00:00",
     "direction": "high", "duration_seconds": 4800.0, "peak_abs_deviation": 3.0},
    {"t_start": 1789731600.0, "t_end": 1789732800.0,
     "start_iso": "2026-09-18T11:40:00+00:00", "end_iso": "2026-09-18T12:00:00+00:00",
     "direction": "low", "duration_seconds": 1200.0, "peak_abs_deviation": 3.0}
  ]
}
```

窗口内从未超限时 `exceedances` 为空数组；`from ≥ to`、`limit ≤ 0` 或窗口边界越出覆盖范围均返回 400。

### 漂移预算耗尽分析

`GET /drift-budget-depletion?from=&budget=` 从 `from` 起沿当前分段线性 `bias` 累积 `|bias|·秒`，解析求出预算首次耗尽的精确时点。逐插值区间推进：段内 `bias` 过零时先在解析零点拆分（`|bias|` 仅在拆分后的子段上才是线性函数，其累积面积是时长的二次函数），目标面积落入某子段时反解该二次式得到精确耗尽时点，全程无固定步长采样。

沿用上文 08:00 `bias 1.0` → 10:00 `bias 3.0` 的区间（`|bias|` 从 1.0 线性升到 3.0，预算 5400 恰在 09:00 耗尽：`(1+2)/2 × 3600s`）：

```bash
curl "http://localhost:8000/drift-budget-depletion?from=2026-09-18T08:00:00Z&budget=5400"
```

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "budget": 5400.0,
  "depleted_at": 1789722000.0, "depleted_at_iso": "2026-09-18T09:00:00+00:00",
  "consumed": 5400.0, "remaining": 0.0,
  "coverage_end": 1789725600.0, "coverage_end_iso": "2026-09-18T10:00:00+00:00",
  "contributions": [
    {"t_start": 1789718400.0, "t_end": 1789722000.0,
     "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T09:00:00+00:00",
     "bias_start": 1.0, "bias_end": 2.0, "absolute_bias_seconds": 5400.0}
  ]
}
```

预算未在覆盖范围内耗尽时 `depleted_at` 为 `null`，`consumed` 为到覆盖终点的全部累计量、`remaining` 为剩余预算；`contributions` 按插值区间裁剪到 `[from, 耗尽点或覆盖终点]`，各段 `absolute_bias_seconds` 之和即 `consumed`。`budget ≤ 0` 或 `from` 越出覆盖范围返回 400。

### 漂移稳定窗口分析

`GET /drift-stability-window?from=&to=&max_spread=` 在 `[from, to]` 内解析求出 `max(bias) − min(bias) ≤ max_spread` 的持续时间最长的连续子区间。最优窗口必是某临界带宽 `[m, m+max_spread]` 的连通分量——`m` 只需取遍各折点取值 `b_i` 与 `b_i − max_spread`（带宽边缘未触及折点取值时，微调 `m` 可让分量至少一端继续外伸、时长严格增大）；分量边界由线性段与带宽边缘的**解析交点**精确定位，可落在插值段内部，全程无固定步长采样。时长并列时返回起点最早者。

沿用上文观测（08:00 `bias 1.0` → 10:00 `bias 3.0` → 12:00 `bias −3.0`），允许波动 `max_spread=2.0` 时，最长稳定窗口为 08:00–10:40（`bias` 1.0 → 3.0 → 1.0，波动恰为 2.0，右端点是 `bias` 回落至 1.0 的解析交点）：

```bash
curl "http://localhost:8000/drift-stability-window?from=2026-09-18T08:00:00Z&to=2026-09-18T12:00:00Z&max_spread=2.0"
```

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "to": 1789732800.0, "to_iso": "2026-09-18T12:00:00+00:00",
  "max_spread": 2.0,
  "t_start": 1789718400.0, "t_end": 1789728000.0,
  "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:40:00+00:00",
  "duration_seconds": 9600.0,
  "min_bias": 1.0, "max_bias": 3.0
}
```

`min_bias`/`max_bias` 为窗口内实际达到的极值（可能小于 `max_spread` 所允许的带宽）；`from ≥ to`、`max_spread ≤ 0` 或窗口边界越出覆盖范围均返回 400。

### 错误码

| 状态码 | 场景 |
| --- | --- |
| 400 | 查询时点超出校准覆盖范围（响应含可用区间；历史补偿按重建后的覆盖范围判定）；积分/超限/稳定窗口 `from ≥ to` 或任一边界越界；超限阈值 `limit ≤ 0`；稳定窗口 `max_spread ≤ 0`；预算耗尽起点 `from` 越界或 `budget ≤ 0`；影响预演 `impact_limit ≤ 0` |
| 404 | 修订/查询修订历史时观测 ID 不存在 |
| 409 | 同一时点重复登记观测；修订时版本过期或目标时点已被占用；影响预演候选时点已占用或两侧无法形成邻点 |
| 422 | 读数不足 3 次、全部读数被判异常、参数非法；回执校验请求结构缺失或字段类型错误 |

## 本地开发

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q          # 运行测试
uvicorn app.main:app --reload       # 本地启动（默认 ./calibration.db）
```

环境变量：

- `CALIB_DB_PATH`：SQLite 文件路径（容器内默认 `/data/calibration.db`，本地开发默认 `./calibration.db`）。
- `CORRECTION_RECEIPT_KEYS`：校正回执密钥环，JSON 对象形式的 `key_id` → 密钥映射，例如 `{"2026-09":"<hex>"}`；`key_id` 须为 1–64 字符的 `[A-Za-z0-9_-]`，密钥须为非空字符串。**生产部署推荐使用**并给每个密钥注入高强度随机值：

  ```bash
  # 单条命令启动：注入双密钥键环与当前签发键
  CORRECTION_RECEIPT_KEYS="{\"2026-09\":\"$(openssl rand -hex 32)\"}" \
  CORRECTION_RECEIPT_ACTIVE_KEY=2026-09 \
  docker compose up --build
  ```

  启动时强制校验：空键环（`{}`）、`CORRECTION_RECEIPT_ACTIVE_KEY` 未设置或不在键环中、非法 `key_id`、密钥为空/非字符串、JSON 格式错误均会让容器拒绝启动。轮换方式见上文「回执密钥轮换」：新旧键并存、只切换 ACTIVE，旧回执即可继续验证；退役旧键（从映射中删除）后其回执立即 `valid=false`。验签严格按回执 `key_id` 精确选键，绝不遍历其它密钥。
- `CORRECTION_RECEIPT_ACTIVE_KEY`：当前新签发回执使用的 `key_id`；配置了 `CORRECTION_RECEIPT_KEYS` 时必须同时提供且为键环成员。
- `CORRECTION_RECEIPT_SECRET`：旧版单密钥（**兼容配置**）。仅当 `CORRECTION_RECEIPT_KEYS` 未设置（或为空）时生效：服务合成仅含 `legacy` 一个键的密钥环，新回执为 `version=2、key_id="legacy"`，v1 历史回执的验签也始终使用该密钥。未设置时 Compose 与应用会回退到开发默认值 `dev-only-insecure-correction-receipt-secret`（仅供本地调试，任何持有源码者都能伪造回执，切勿用于生产）：

  ```bash
  # 不配置密钥环的旧式启动
  CORRECTION_RECEIPT_SECRET=$(openssl rand -hex 32) docker compose up --build
  ```

  更换该 secret 后，v1 回执与 `legacy` 键 v2 回执都将校验失败（`valid=false`）；从单密钥模式迁移到密钥环后，v1 回执仍按此 secret 验证、不受键环轮换影响。

## 项目结构

```
app/
  main.py         # FastAPI 路由
  service.py      # 观测入库、相邻区间重算、补偿查询、as_of 历史状态重建、漂移积分、超限窗口分析、预算耗尽分析、稳定窗口分析、观测影响预演
  receipts.py     # 校正回执：复用漂移补偿、v2/v1 HMAC-SHA256 签名与常量时间校验
  keyrotation.py  # 回执密钥环：CORRECTION_RECEIPT_KEYS/ACTIVE_KEY 解析、启动校验与按 key_id 精确选键
  calibration.py  # 纯计算：异常判定、偏差、线性插值、分段积分、超限交点求解、预算累积反解、稳定窗口求解、插入影响解析比较
  models.py       # Pydantic 请求/响应模型
  db.py           # SQLite 连接与建表
tests/test_api.py               # 端到端 API 测试
tests/test_correction_receipts.py # 校正回执签发/校验端到端测试
Dockerfile / docker-compose.yml
```
