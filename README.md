# restart-permit-api

存储集群值班员用的**分批重启许可 API**。FastAPI + SQLite，单容器即可运行。

核心规则：

- 服务启动时读取**固定的分片投票副本清单**（`manifest.json`）。
- 节点探活报告携带**采样时间、健康状态、启动标识**；每个节点以**采样时间最新**的一份为准。
- 值班员提交**申请号 + 拟重启的一批节点**，满足以下条件才批准整批：
  1. 本批每个节点最新探活均健康；
  2. 本批节点未被任何**未结束许可**占用；
  3. 扣除 *本批节点 + 未结束许可占用节点 + 健康未知/异常节点* 后，本批涉及的**每个分片**剩余可用投票副本均**过半**；否则拒绝并列出受影响分片。
- **并发申请**在写事务（`BEGIN IMMEDIATE`）中串行判定，守住多数票。
- **同号同批重发**返回原许可（HTTP 200）；同号不同批返回 409。
- 许可与探活记录落盘 SQLite，**服务重启后保留**。
- **结束许可**要求本批每个节点的最新探活：在获批后采集、健康、启动标识与获批时不同。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互文档 http://localhost:8000/docs
```

- SQLite 数据在命名卷 `permit-data`（容器内 `/data/permits.db`），重启不丢。
- 分片清单 `./manifest.json` 只读挂载进容器；修改后需 `docker compose restart` 生效。

本地开发（不用 Docker）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
DB_PATH=./data/permits.db MANIFEST_PATH=./manifest.json \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pytest tests/ -q
```

## 分片清单格式

```json
{
  "shards": {
    "shard-a": ["n1", "n2", "n3"],
    "shard-b": ["n2", "n3", "n4"],
    "shard-c": ["n3", "n4", "n5"]
  }
}
```

每个分片列出全部投票副本节点；过半线 = `floor(票数/2)+1`（3 票需 2 可用）。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 服务健康检查 |
| GET | `/manifest` | 查看分片清单与各分片过半线 |
| POST | `/liveness` | 批量上报探活（201） |
| GET | `/liveness` | 全部节点最新探活 |
| GET | `/liveness/{node}` | 单节点最新探活 |
| POST | `/permits` | 申请许可（201 新批 / 200 幂等重发 / 409 拒绝） |
| GET | `/permits?status=open\|finished` | 许可列表 |
| GET | `/permits/{request_id}` | 单个许可 |
| POST | `/permits/{request_id}/finish` | 结束许可（200 / 409 条件不满足） |
| GET | `/nodes/{node}/restart-audit` | **只读**：核查节点在 UTC 时间窗内的重启嫌疑变化与许可覆盖 |

## 调用示例

### 1. 上报探活

```bash
curl -X POST http://localhost:8000/liveness \
  -H 'Content-Type: application/json' -d '{
    "reports": [
      {"node_id":"n1","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"},
      {"node_id":"n2","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"},
      {"node_id":"n3","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"},
      {"node_id":"n4","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"},
      {"node_id":"n5","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"}
    ]}'
# => {"accepted":5}
```

### 2. 申请许可

```bash
curl -X POST http://localhost:8000/permits \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-001","nodes":["n1"]}'
# 201 =>
# {"request_id":"req-001","nodes":["n1"],"status":"open",
#  "approved_at":"...","finished_at":null,
#  "boot_ids_at_approval":{"n1":"boot-1"}}
```

同号同批重发，返回原许可（200，`approved_at` 不变）；同号不同批返回 409。

### 3. 多数票不足被拒（列出受影响分片）

`req-001` 已占用 n1 时再申请 n2（同属 shard-a，3 票最多扣 1 票）：

```bash
curl -X POST http://localhost:8000/permits \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-002","nodes":["n2"]}'
# 409 =>
# {"detail":{"ok":false,"error":"permit_rejected",
#   "affected_shards":[{"shard":"shard-a","voters":3,"required_available":2,
#     "available":1,"available_nodes":["n3"],
#     "excluded":{"n1":"occupied_by:req-001","n2":"in_batch"}}]}}
```

其它拒绝类别：`health_unknown_nodes`（健康未知）、`unhealthy_nodes`（异常）、
`occupied_nodes`（被未结束许可占用）。清单外节点一律 422。

### 4. 节点重启后上报新探活，结束许可

```bash
# 节点重启完成：采样时间晚于获批时间、健康、启动标识已变
curl -X POST http://localhost:8000/liveness \
  -H 'Content-Type: application/json' -d '{
    "reports":[{"node_id":"n1","sampled_at":"2026-11-19T10:20:00Z",
                "healthy":true,"boot_id":"boot-2"}]}'

curl -X POST http://localhost:8000/permits/req-001/finish
# 200 => {"request_id":"req-001",...,"status":"finished","finished_at":"..."}
```

结束条件不满足时 409 并给出逐节点原因，例如：

```json
{"detail":{"error":"finish_conditions_not_met",
           "nodes":{"n1":"boot_id_unchanged"}}}
```

可能原因：`no_liveness_report`、`not_sampled_after_approval`、`unhealthy`、`boot_id_unchanged`。
许可结束后节点被释放，可被后续申请占用。

### 5. 重启核查（只读）

分片异常后，值班员核查某节点是否在**许可之外重启**。输入节点与 UTC 起止时间
（窗口为 `[start, end)`：含起点、不含终点），服务按采样时间回看启动标识的变化。

判定规则：

- 同一采样时刻有多份探活时，**以后入库的一份为准**（SQL 按 `id` 去重）。
- 仅列出**后一份探活采样时刻 ≥ start 且 < end** 的启动标识变化；
  起点之前的最后一份探活作为对照基线（`baseline`，可能为空）。
- 每次变化对应一个**可能重启时段** `(前一次采样, 后一次采样]`：
  重启可能发生在前一次采样之后、后一次采样当时或之前。
- 许可**获批时生效、结束时失效**（未结束许可视为仍有效）；
  只要许可有效期与可能重启时段**相交**，且许可覆盖该节点，即为候选。
- **每份许可在本次核查中最多覆盖一次变化**。服务对"变化 ↔ 许可"做二分图
  最大匹配，`summary.min_uncovered_changes` = 总变化数 − 可覆盖的最大数，
  即这段时间**至少有几次变化无法由许可覆盖**。
- 探活与许可在**同一读事务快照**中读取；核查期间上报与许可操作照常受理，
  不会阻塞，也不会读到新旧混合的状态。

```bash
curl -G 'http://localhost:8000/nodes/n1/restart-audit' \
  --data-urlencode 'start=2026-11-19T10:00:00Z' \
  --data-urlencode 'end=2026-11-19T11:00:00Z'
```

```json
{
  "node_id": "n1",
  "window": {"start": "2026-11-19T10:00:00+00:00", "end": "2026-11-19T11:00:00+00:00"},
  "baseline": {"sampled_at": "2026-11-19T09:55:00+00:00", "healthy": true, "boot_id": "boot-1"},
  "changes": [
    {
      "previous": {"sampled_at": "2026-11-19T10:10:00+00:00", "healthy": true, "boot_id": "boot-1"},
      "current":  {"sampled_at": "2026-11-19T10:20:00+00:00", "healthy": true, "boot_id": "boot-2"},
      "possible_restart_window": {
        "start": "2026-11-19T10:10:00+00:00",
        "end": "2026-11-19T10:20:00+00:00",
        "boundary": "(prev_sample, current_sample]"
      },
      "candidate_permits": ["req-001"],
      "covered_by": "req-001"
    },
    {
      "previous": {"sampled_at": "2026-11-19T10:40:00+00:00", "healthy": true, "boot_id": "boot-2"},
      "current":  {"sampled_at": "2026-11-19T10:50:00+00:00", "healthy": true, "boot_id": "boot-3"},
      "possible_restart_window": {
        "start": "2026-11-19T10:40:00+00:00",
        "end": "2026-11-19T10:50:00+00:00",
        "boundary": "(prev_sample, current_sample]"
      },
      "candidate_permits": ["req-001"],
      "covered_by": null
    }
  ],
  "summary": {
    "total_changes": 2,
    "max_coverable_by_permits": 1,
    "min_uncovered_changes": 1
  }
}
```

字段说明：

- `baseline`：起点前最后一份探活（窗口内第一份探活之前没有数据时为 `null`）。
- `changes[].candidate_permits`：与该次变化可能重启时段相交的全部候选许可号。
- `changes[].covered_by`：最大匹配中实际覆盖该变化的许可号；`null` 表示无法覆盖。
- `summary`：变化总数、许可最多可覆盖数、**至少无法覆盖的变化次数**。
- 清单外节点返回 422（`unknown_nodes`）；`start >= end` 返回 422
  （`invalid_time_window`）；不带时区的时间按 UTC 处理。

## 设计说明

- **幂等**：`request_id` 是许可表主键。节点集合规范化（去重排序）后比较，
  同号同批重放直接返回原许可；同号不同批 409。被拒绝的申请不落库，整改后可用同号重试。
- **并发**：所有写路径走单连接 + 可重入锁 + `BEGIN IMMEDIATE` 事务，
  "检查多数票 → 写入许可"是原子的；并发申请串行判定，不会超批。
- **持久化**：许可与全部探活报告存 SQLite（WAL 模式），最新探活按
  `max(sampled_at)` 实时计算，服务重启后状态完整恢复。
- **结束判定**：批准时快照各节点 `boot_id`；结束时要求最新报告
  `sampled_at > approved_at` 且健康且 `boot_id` 不同。
- **重启核查**：在纯函数 `analyze_restarts` 中完成，输入为同一读事务快照
  （去重探活序列 + 全部许可），无副作用、可独立单测。同采样时刻按入库
  `id` 最大者胜出；变化窗口为左开右闭 `(prev, curr]`，许可有效期为
  闭开 `[approved_at, finished_at)`，相交判定按各自边界语义处理。
  "每许可最多覆盖一次"建模为二分图匹配，用 Kuhn 增广路求最大匹配，
  未被匹配的变化数即"至少无法由许可覆盖"的下界，候选有序、结果可复现。

## 已知限制 / 未完成项

- 单 worker 运行（SQLite 单写者）；如需多副本需换外部数据库。
- 未做认证鉴权，假定部署在受信内网；如需可前置网关或加 token。
- 探活无"过期"概念：只要最新一份健康即视为健康，不判断采样时间新旧。
- 结束许可比较的是节点上报的采样时钟与服务端获批时钟，依赖各节点时钟基本同步（NTP）。
- 许可无过期/自动回收机制；值班员需显式 finish。
- 重启核查只能发现被探活采样捕获的 `boot_id` 变化：两次采样间发生多次重启
  但标识恰好变回旧值时无法识别；无任何探活的时段无法判定重启。
- 核查返回的是无法覆盖次数的**下界**（采样粒度限制 + 许可只能按"相交"
  做候选，不能断言某次重启确由某许可授权）。
