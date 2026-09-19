# 骑手碎片时间诊疗协同

面向新就业群体（外卖骑手）的诊疗协同后端。骑手只提交**可用时间窗与所处服务片区**，系统不接入、不存储任何配送订单详情；社区卫生服务中心、社区卫生服务站、工会驿站三处服务点的短时接诊（15 分钟接诊、30 分钟中医干预、驿站外展义诊）在骑手两单之间的空档内完成匹配与重排。

## 领域规则

### 排班匹配
- 请求只含：服务类型、时间窗（起/止）、片区。匹配在同片区、可开展该服务的服务点号源内进行。
- 中医干预只能在中心/站点开展；外展义诊只能在驿站开展且对所有医保身份免费。
- 号源按容量逐分钟占用；匹配取窗口内最早可行位置（5 分钟粒度对齐）。
- 生成约诊前必须有**通过的身份核验**；未核验返回 `identity_unverified`。

### 显式重排规则（全部写入约诊/请求 history，可重放核对）
- **R1 迟到**：≤5 分钟宽限直接接诊；6–15 分钟在本人时间窗内顺延到不早于实际到达时刻的最近容量；>15 分钟按爽约处理并释放号源。
- **R2 临时接单**：骑手取消（原因 `rider_order`）；携带新时间窗时按新窗口立即重排。
- **R3 紧急优先**：紧急约诊无空位时，可驱逐**普通约诊**（取驱逐人数最少、最晚预约者先让位的方案）；被驱逐者立即按原窗口重新匹配，无处可去则进入待处理队列，绝不静默丢失。普通约诊永远不能驱逐紧急约诊。
- **R4 医生出诊变化**：号源改期/取消时受影响约诊逐个自动重排（紧急优先），号源取消且无替代时进入待处理队列，经 `/requests/{id}/window` 与骑手确认新窗口后再匹配。
- 号源带版本号，并发改期提交过期版本返回 **409 `slot_version_conflict`**。

### 隐私隔离
- **身份侧**（核验岗）：证件引用、核验方式、核验结果、脱敏姓名——与健康内容物理分库。
- **档案侧**（医护）：诊断、处置、就诊记录——不含任何证件字段；身份是否已核验只以布尔位出现。
- **工会侧**：`/union/coverage` 仅返回去标识化聚合（人次、去重人数、按片区/服务/处置的计数），无法接触骑手标识、证件与诊断。

### 处置与费用
- 一次到诊可处置为：院内治疗 `inhouse_treatment`、驿站干预 `post_intervention`、正式转诊 `formal_referral`、仅随访 `followup_only`。
- 转诊自动追加转诊费用行。每条费用行携带 `total_fen / covered_fen / self_fen` 及 `basis`（可读依据）与 `source`（依据出处）。
- 费用依据按 **职工医保 / 居民医保 / 自费 × 服务包版本** 解析；签约 `fd-v1` 服务包将接诊自付封顶为 1 元，依据与出处中均标注服务包来源。金额为示例费率。

### 随访接续
- 随访任务绑定骑手签约的**家庭医生团队**。骑手跨站点（片区）后，未完成随访自动带至新片区标签，责任团队不变，转移轨迹写入随访 history；跨日到期仍在该团队任务列表中。

## 接口（均为 JSON；角色由请求头 `X-Role` 指定：staff / verifier / union / rider）

| 方法 路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /admin/locations` · `/admin/practitioners` · `/admin/slots` · `/admin/riders` | staff | 资源开诊准备 |
| `POST/GET /riders/{id}/identity` | verifier, staff | 写入/查看核验结果（无健康内容） |
| `GET /riders/{id}/health` | staff | 健康档案（无证件字段） |
| `POST /riders/{id}/transfer-site` | staff, rider | 跨站点，随访同团队接续 |
| `POST /requests` | staff, rider | 提交时间窗（无订单信息） |
| `POST /requests/{id}/triage` | staff | 设置医疗优先级 normal/urgent |
| `POST /requests/{id}/window` | staff, rider | 待处理请求确认新时间窗 |
| `POST /requests/{id}/match` | staff | 匹配（必要时按 R3 重排） |
| `GET /queue?zone=&day=YYYY-MM-DD` | staff | **开诊前可执行队列**：号源、医护、按时间排序（同刻紧急在前）、待处理清单 |
| `POST /slots/{id}/adjust` | staff | R4 改期/取消（可带 `expected_version` 做并发控制） |
| `POST /appointments/{id}/arrival` | staff | R1 到诊 |
| `POST /appointments/{id}/grab-order` | staff, rider | R2 临时接单（可带新窗） |
| `POST /appointments/{id}/complete` | staff | 记录处置/诊断，生成费用行 |
| `POST /appointments/{id}/followups` | staff | 安排随访（绑团队，可跨日） |
| `POST /followups/{id}/complete` | staff | 完成随访 |
| `GET /teams/{teamId}/followups` | staff | 团队随访任务（含跨站点接续） |
| `GET /union/coverage?start=&end=` | union, staff | 去标识化覆盖统计 |
| `GET /events` | staff | 追加事件日志 |
| `POST /replay` | staff | 重放事件序列并输出核对报告 |

时间字段对外统一为 `YYYY-MM-DDTHH:MM`，内部为分钟制整数。

## 事件重放与核对

所有状态变更都是追加事件。`POST /replay`（或 Python 侧 `clinic.replay.audit(events)`）把临时派单与并发改期事件重放进全新存储，输出四项核对 + 重放确定性：

- `capacity`：逐分钟占用不超号源容量、约诊不越出号源时间范围；
- `fee_basis`：每个费用快照/费用行与「医保身份 × 服务包版本」依据目录一致，统筹+自付=总额，依据出处齐全；
- `priority`：被驱逐者均为普通约诊、无紧急请求滞留队列；
- `followup_continuity`：随访团队恒定、跨站点后片区标签同步且留有 `team_unchanged` 轨迹、到期不早于就诊日；
- `replay_determinism`：同一事件序列两次重建状态完全一致。

## 运行与测试

```bash
python3 service.py --check          # 基础自检
python3 service.py --port 8000      # 启动后 GET /health
python3 demo_audit.py               # 临时派单+并发改期+跨日随访的重放核对演示
npm test                            # 全部 37 项契约/领域/接口测试
```

## 代码结构

```
service.py              入口（保留基线 health 契约）
clinic/clock.py         分钟制时钟
clinic/catalog.py       服务点/服务/医保/服务包/费用依据目录
clinic/errors.py        机器可读错误码（409/422/403/404）
clinic/store.py         追加事件存储 + 重放 + 逐分钟容量
clinic/engine.py        匹配、R1–R4 重排、优先级、处置、费用快照、随访、视图
clinic/replay.py        事件重放与四项核对
clinic/server.py        HTTP 接口与角色控制
scenarios.py            测试场景夹具
test_domain.py / test_api.py / service_contract.py
```
