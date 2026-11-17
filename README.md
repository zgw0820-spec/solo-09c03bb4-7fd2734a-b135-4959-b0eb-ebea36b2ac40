# 机房告警升级 API

供电柜过热告警的升级通知服务。两条入口：

1. **柜温采样自动判定**：采集端按柜号上传采样时间与摄氏温度，服务端识别持续过热，
   确认的过热事件自动创建一条告警。
2. **手工提交告警**：保留原有的 `POST /alarms` 人工提交通道。

告警通知语义：**先通知主值班员 → 主通知确已受理才开始计时 →
逾期未确认仅投递一次备班通知**。附带一个可设置慢响应、可丢回执的本地通知网关，
用于演示和验证各种异常路径。

## 快速开始

```bash
docker compose up --build -d
curl -s localhost:8000/health
open http://localhost:8000/docs      # Swagger UI
```

两个服务：

| 服务 | 地址 | 说明 |
|---|---|---|
| `api` | http://localhost:8000 | 告警 API，Swagger 在 `/docs` |
| `gateway` | http://localhost:9000 | 本地通知网关，Swagger 在 `/docs` |

数据分别持久化在命名卷 `api-data`、`gateway-data` 中，`docker compose restart`
或重建容器后仍能接续未完成的投递与确认状态。

不使用 Docker 时：

```bash
pip install -r requirements.txt
python -m uvicorn gateway.main:app --port 9000 &
DATABASE_PATH=./data/alarms.db GATEWAY_URL=http://127.0.0.1:9000 \
  python -m uvicorn app.main:app --port 8000
```

## 核心语义

### 0. 柜温采样与过热事件判定

`POST /cabinets/{cabinet_id}/samples`：采集端按柜号上传 `sampled_at`（带时区的
ISO-8601 UTC 时间）与 `temperature_c`（摄氏温度）。

**接收窗口与乱序**

- 只接收接收时刻之前 120 秒内的采样（`THERMAL_SAMPLE_WINDOW_SECONDS`）；未来时间、
  老化达到 120 秒的采样返回 422。乱序上传允许，判定一律按采样时间排序后计算。
- 同柜同采样时刻 + 同温度重送：返回 200 且 `deduplicated=true`，不重复计数。
- 同柜同采样时刻 + 不同温度：返回 409，拒绝写入。

**触发条件（按采样时间顺序的确定性归并）**

- 温度 **≥ 70°C**（`THERMAL_HIGH_THRESHOLD_C`）记为高温读数；连续三笔高温、且每对
  相邻读数间隔 **≤ 60 秒**（`THERMAL_MAX_GAP_SECONDS`）才构成候选连续高温。
- 温度 **< 65°C**（`THERMAL_RESET_THRESHOLD_C`）的读数打断连续高温并重新布防；
  65°C ≤ t < 70°C 的读数只打断连续高温、**不**重新布防。已确认一次事件后，必须等
  到一笔真正低于 65°C 的读数，才允许再次触发。

**判定窗口（待定 → 结论不可改写）**

- 接收窗口 120 秒也是判定的不可变边界：水位线 `now - 120s` 之前不可能再有新采样。
  候选连续高温的第一笔读数越过水位线时窗口关闭，事件由 `pending` 变为
  `confirmed`（成立）或 `interrupted`（迟到的低温等在关窗前打断了连续高温）。
- 窗口未关时，`GET /cabinets/{id}` 与 `GET /thermal-events` 只显示“待定”。
- 关窗后旧样本无法再进入系统（接收时即 422），已落定的结论不可被改写。

**事件与告警的关系**

- 每个确认事件只创建**一条**告警：告警号由“柜号 + 连续高温首笔采样时间”确定性生成，
  主备联系人与确认期限取自 Compose 环境配置（见下），通知、确认、逾期升级完全沿用
  原有 `POST /alarms` 的状态机。
- 关窗落库与建告警分开提交：判定先持久化，建告警失败或进程中断后，后台 tick 会按
  确定性告警号幂等补建，不漏报、不重复通知。
- `GET /thermal-events`（可按柜号、状态过滤）与 `GET /thermal-events/{event_no}`
  返回判定所用的三笔读数 `trigger_readings` 以及关联告警快照 `alarm`。

### 1. 提交告警

`POST /alarms`：告警号、告警内容、主/备联系人、确认期限（**2～30 秒**）。
该手工接口完整保留，过热事件自动建告警走的也是同一条幂等提交逻辑。

- 同一告警号 + **完全相同内容**的重试：返回 `200`，复用原告警，不重复通知。
- 同一告警号但内容不同：返回 `409`。
- 首次提交返回 `201`。内容指纹覆盖告警内容、metadata、主备联系人、期限。

### 2. 计时只在“网关确已受理”后开始

每条通知在发起 HTTP 之前先落库（含完整请求体），通知号由数据库生成并复用。
worker 的状态机：

```
pending ──POST──> in_flight ──网关 202 accepted──> accepted   （主通知：此刻开始计时）
                       │
                       ├──网关明确拒收──> failed                （不盲目重试，展示原因）
                       └──超时/断连/回执丢失──> unknown        （不当作已送达，也不计时）
                                                   │
                                            GET /notifications/{no} 查询受理结果
                                                   ├── accepted → accepted（用网关受理时刻计时）
                                                   ├── rejected → failed
                                                   └── 无记录 ≥3 次 → 重投（通知号幂等）
```

**回执不明永远不会触发升级**：`unknown` 状态下 `primary_accepted_at`、
`confirmation_due_at` 均为 `null`，接口状态为 `primary_unconfirmed`，并通过
`warnings` 明确提示。查询告警时会先向网关核对受理结果（只查不发）。

计时起点使用**网关记录的受理时间**，因此回执丢失或 API 重启后补查到的受理，
其期限仍从真实受理时刻起算，不会凭空延长主值班员的处理窗口。

### 3. 逾期升级：仅一次

后台 deadline 循环只做数据库操作、不发网络请求，因此网关再慢也不会推迟到期判断。
到期且未确认时，在单个事务内用条件 UPDATE 抢占并插入**唯一一条**备班通知
（`UNIQUE(alarm_no, role)` 兜底）。重复 tick、重启、并发都不会产生第二条。

### 4. 确认与到期的交错

`POST /alarms/{alarm_no}/confirm` 是一个短事务，不与网关通信，因此确认不会被慢投递阻塞。
竞态由条件 UPDATE 仲裁：

| 时序 | 结果 |
|---|---|
| 确认早于到期 | 直接 `confirmed`，不会创建备班通知 |
| 到期已生成备班行、但尚未发送 | 确认与“取消备班行”在同一事务完成，`status=confirmed`，备班显示“未投递即取消” |
| 备班已在途 | 确认成功，但**不撤回、也不再新建**；最终通过网关查询得到真实受理结果，显示 `confirmed_after_escalation`，并标记 `delivered_after_confirmation=true` 与“在途无法撤回”告警 |

### 5. 重启接续

- 重启时 `in_flight` 行（上一进程死在 POST 中）转为 `unknown`，先查网关再决定是否重投。
- 若网关已受理，直接采纳受理结果，**不会重发**；若网关无记录，重投时通知号相同，
  网关去重保证最多一次投递。
- pending/未知回执、未确认告警的到期扫描都从数据库重建，无内存队列可丢。

### 6. 慢投递不影响其他告警

- 投递是并行任务（`DELIVERY_CONCURRENCY`，默认 8），HTTP 调用不持有数据库锁。
- 确认、查询、到期扫描都只做短事务，不等待网络。
- 状态查询中的网关核对并发执行，最坏耗时约一次查询超时，而非逐条累加。

## 接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/cabinets/{cabinet_id}/samples` | 上传柜温采样（202；同温重送 200；异温 409；超窗 422） |
| GET | `/cabinets` | 列出有采样的柜号及判定状态 |
| GET | `/cabinets/{cabinet_id}` | 单柜状态：连续高温长度、待定事件、布防/冷却状态 |
| GET | `/thermal-events` | 过热事件列表（`?cabinet_id=&status=` 过滤） |
| GET | `/thermal-events/{event_no}` | 单事件：判定读数 + 关联告警快照 |
| POST | `/alarms` | 手工提交告警（201；幂等重试 200；内容冲突 409） |
| GET | `/alarms/{alarm_no}` | 告警、通知、确认、计时状态 |
| POST | `/alarms/{alarm_no}/confirm` | 确认（幂等） |
| GET | `/alarms/{alarm_no}/notifications` | 该告警的通知明细 |
| GET | `/notifications/{notification_no}` | 按通知号查询网关受理结果 |
| GET | `/alarms` | 列出告警，可按 `status` 过滤 |
| GET | `/health` | 健康检查 |

热事件状态：`pending`（待定，窗口未关）、`confirmed`（已确认并建告警）、
`interrupted`（关窗前被打断，未成立）。

告警状态值：`primary_queued`、`primary_sending`、`primary_unconfirmed`、
`primary_rejected`、`awaiting_confirmation`、`escalated`、`confirmed`、
`confirmed_after_escalation`。

## 通知网关（测试用）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/notifications` | 受理通知，按 `notification_no` 去重；重复请求返回 `{"status":"duplicate"}` |
| GET | `/notifications/{no}` | 查询受理结果（回执丢失后的权威来源） |
| GET | `/notifications` | 列出所有通知记录（`?role=&alarm_no=`） |
| GET/PUT | `/config` | 运行时调整故障注入 |
| DELETE | `/admin/reset` | 清空记录（测试便利） |

```bash
# 慢响应：受理延时 2.5s，API 的 POST 超时为 2s → API 看到“回执不明”
curl -X PUT localhost:9000/config -H 'content-type: application/json' \
  -d '{"slow_ms":2500,"drop_receipt":false,"reject":false}'

# 丢回执：确已受理，但连接挂起 3s 后断开，客户端拿不到回执
curl -X PUT localhost:9000/config -H 'content-type: application/json' \
  -d '{"slow_ms":0,"drop_receipt":true,"drop_hold_ms":3000}'

# 拒收
curl -X PUT localhost:9000/config -H 'content-type: application/json' \
  -d '{"reject":true}'
```

网关记录也会持久化到卷中，因此 API 与网关都重启后仍可查询历史受理结果。

## 演示

```bash
# 0) 柜温：连续三笔 ≥70°C（相邻间隔 ≤60s），窗口未关前为待定，关窗后自动建一条告警
for t in 71 74 78; do
  TS=$(python3 -c "from datetime import UTC,datetime;print(datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00','Z'))")
  curl -s -X POST localhost:8000/cabinets/A07/samples -H 'content-type: application/json' \
    -d "{\"sampled_at\":\"$TS\",\"temperature_c\":$t}"
  sleep 1   # 真实采集端各自带采样时间戳，这里仅保证三笔采样时刻不同
done
curl -s localhost:8000/cabinets/A07 | python3 -m json.tool        # pending_verdict
curl -s localhost:8000/thermal-events?cabinet_id=A07 | python3 -m json.tool
# 约 120 秒后事件 confirmed，且 /thermal-events/{event_no} 内含关联告警与三笔读数

# 1) 正常：3 秒内确认，不升级
curl -X POST localhost:8000/alarms -H 'content-type: application/json' -d '{
  "alarm_no":"DEMO-1","content":"供电柜 A07 温度 78°C",
  "primary_contact":{"name":"主值班员 张工","channel":"app"},
  "backup_contact":{"name":"备班 李工","channel":"sms","address":"+8613800000000"},
  "ack_deadline_seconds":5}'
curl -X POST localhost:8000/alarms/DEMO-1/confirm -H 'content-type: application/json' \
  -d '{"confirmed_by":"张工"}'
curl -s localhost:8000/alarms/DEMO-1 | python3 -m json.tool

# 2) 逾期：2 秒不确认，只通知一次备班
curl -X POST localhost:8000/alarms -H 'content-type: application/json' -d '{
  "alarm_no":"DEMO-2","content":"供电柜 B02 温度 82°C",
  "primary_contact":{"name":"主值班员","channel":"app"},
  "backup_contact":{"name":"备班","channel":"sms"},
  "ack_deadline_seconds":2}'
sleep 5
curl -s localhost:8000/alarms/DEMO-2 | python3 -m json.tool
```

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_PATH` | `./data/alarms.db` | SQLite 路径 |
| `GATEWAY_URL` | `http://127.0.0.1:9000` | 通知网关地址 |
| `GATEWAY_TIMEOUT_SECONDS` | `2.0` | 投递 POST 超时 |
| `GATEWAY_LOOKUP_TIMEOUT_SECONDS` | `1.0` | 受理结果查询超时 |
| `WORKER_INTERVAL_SECONDS` | `0.1` | worker tick 间隔 |
| `DELIVERY_LEASE_SECONDS` | `10` | 在途租约；进程死亡后由他人接管 |
| `DELIVERY_MAX_BACKOFF_SECONDS` | `1.0` | 未知回执的重查退避 |
| `DELIVERY_CONCURRENCY` | `8` | 并发投递上限 |
| `NOT_FOUND_MISSES_BEFORE_REPOST` | `3` | 网关连续“无记录”多少次后才重投 |
| `RECONCILE_ON_READ` | `true` | 查询时是否先向网关核对未知回执（只查不发） |
| `RECOVER_INFLIGHT_ON_STARTUP` | `true` | 启动时把中断的在途通知转为待查 |
| `THERMAL_SAMPLE_WINDOW_SECONDS` | `120` | 采样接收窗口，也是判定窗口；只收过去这么多秒内的采样 |
| `THERMAL_HIGH_THRESHOLD_C` | `70` | 高温阈值（达到即计入连续高温） |
| `THERMAL_RESET_THRESHOLD_C` | `65` | 重新布防阈值（严格低于该温度才算冷却） |
| `THERMAL_CONSECUTIVE_REQUIRED` | `3` | 触发所需连续高温笔数 |
| `THERMAL_MAX_GAP_SECONDS` | `60` | 连续高温相邻读数最大间隔 |
| `THERMAL_TICK_INTERVAL_SECONDS` | `0.2` | 后台关窗/建告警 tick 间隔 |
| `THERMAL_RETENTION_SECONDS` | 约 10 年 | 原始采样保留时长（事件读数随事件永久保留） |
| `THERMAL_PRIMARY_CONTACT_NAME` / `_CHANNEL` / `_ADDRESS` | 主值班员 / app / 空 | 热事件告警的主联系人 |
| `THERMAL_BACKUP_CONTACT_NAME` / `_CHANNEL` / `_ADDRESS` | 备班 / sms / 空 | 热事件告警的备联系人 |
| `THERMAL_ACK_DEADLINE_SECONDS` | `10` | 热事件告警的确认期限（2～30） |
| `ALLOW_TEST_CLOCK` | `false` | 测试专用：允许 `X-Test-Now-Epoch` 头覆盖接收时刻，生产保持关闭 |

## 测试

```bash
python -m pytest -q                     # 46 个测试，进程内假网关，真实 SQLite 文件
API_URL=http://localhost:8000 GATEWAY_URL=http://localhost:9000 \
  python -I scripts/smoke_test.py       # 对真实运行的服务做端到端冒烟
```

覆盖：幂等提交与 409、计时起点、确认阻断升级、逾期仅一次、回执不明不升级、
回执补查不重复投递、重启接续（三种中断点）、确认 vs 到期竞态、慢投递不阻塞、
拒收处理、在途备班如实披露、Swagger/OpenAPI；以及柜温采样的窗口/乱序/幂等/异温
拒绝、连续高温三笔与 60 秒间隔、65°C 重新布防、迟到低温打断、关窗后结论不可改写、
关窗建告警的崩溃接续与重启幂等、后台自动关窗并恰好一次通知。

## 已知限制

- SQLite 单写者：适合单实例部署。多副本共享一个数据库需要换成 PostgreSQL，
  并重新审视事务隔离（`RECOVER_INFLIGHT_ON_STARTUP` 也需关闭）。
- 网关拒收主通知后不自动重发，只标记并告警，等待人工核实联系人——这是有意为之，
  避免对错误号码反复外呼。
- 备班通知不设二次升级（需求只要求一次）。
- 没有鉴权/限流；生产部署前应补上。
