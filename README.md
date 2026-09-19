# 骑手碎片时间诊疗协同

面向新就业群体（骑手）的诊疗协同后端。社区卫生服务中心、社区服务站与工会驿站三处服务点，把十五分钟接诊、三十分钟中医干预与每周外展义诊嵌进骑手两单之间的空档，并由签约家庭医生团队接续随访。

## 核心规则

**时间窗匹配**
- 骑手只提交可用时间窗（开始、结束）与所处服务片区；系统不读取配送订单详情，携带订单字段的请求会被拒绝。
- 槽位以 15 分钟为粒度：十五分钟接诊占 1 格，三十分钟中医干预占连续 2 格。
- 槽位选择顺序：签约家庭医生团队优先，再最早时间，再服务点剩余容量。

**医疗优先级不能被普通排队覆盖**
- 优先级分普通（0）、较高（1）、紧急（2），大于 0 须注明医学理由。
- 容量不足时，较高医疗优先级可置换严格更低优先级且未签到的预约；被置换者自动改期或进入候补。
- 候补与补位都按（医疗优先级，候补顺序）排列。

**明确改期规则**
- 临时接单（`temp_order`）/骑手主动调整（`rider_request`）：释放原槽位并按新时间窗重排，释放的容量立即回流候补。
- 迟到：10 分钟宽限内保留槽位；超过宽限期重排到当天同服务点下一个可用槽位。
- 医生出诊变化：支持取消、缩短、移动、换人；受影响预约按医疗优先级依次重排，先本服务点再本片区。

**数据隔离**
- 身份证明核验结果与健康档案分开保存（两个独立存储）。
- 工会只能访问去标识化覆盖统计（仅计数），看不到诊断与骑手标识；角色越界访问返回 403。
- 工作角色经 `X-Actor-Role` 请求头声明：`clinician`（医护）、`rider`（骑手）、`verifier`（身份核验员）、`union`（工会）、`admin`（管理）。

**费用估算**
- 估算顺序：基准价 → 家庭医生服务包减免 → 医保统筹报销（对减免后余额）→ 个人自付。
- 每个数字都在 `sources` 中注明来源：价格表版本、职工医保/居民医保统筹比例、服务包版本（`none`/`basic-v1`/`plus-v2）。

**到诊转化与随访**
- 一次到诊可转为院内治疗（`in_hospital`）、驿站干预（`station_intervention`）或正式转诊（`formal_referral`）。
- 正式转诊生成 3 日内随访、中医干预生成 7 日复评；随访任务归属骑手签约的家庭医生团队，跨站点、跨日接续，不随服务点变化。

**并发与重放**
- 所有写操作在存储锁内串行化；预约携带版本号，改期可带 `expected_version`，冲突返回 409。
- 写请求可携带 `request_id` 实现幂等，重放同一请求返回首次结果。

## 运行

```bash
python3 service.py --check          # 基础检查
python3 service.py --port 8000      # 启动服务，/health 健康检查
python3 service.py --replay scenarios/temp_orders.json   # 重放场景并输出核对摘要
npm test                            # 全部测试（契约 + 领域 + 接口 + 场景重放）
```

## 主要接口

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `GET /health` | 公开 | 健康检查 |
| `POST /api/riders` | staff | 登记骑手（医保身份、服务包版本、签约团队） |
| `POST /api/riders/{id}/windows` | staff | 提交可用时间窗并匹配（日期/起止/片区/服务类型） |
| `POST /api/appointments/{id}/reschedule` | staff | 临时接单/骑手改期（`reason`+新时间窗，可带 `expected_version`） |
| `POST /api/appointments/{id}/checkin` | staff | 签到（迟到按明确规则重排） |
| `POST /api/appointments/{id}/outcome` | staff | 到诊转化（院内治疗/驿站干预/正式转诊） |
| `POST /api/appointments/{id}/priority` | staff | 设定医疗优先级（须注明医学理由） |
| `POST /api/shifts` | staff | 发布医生排班 |
| `POST /api/shifts/{id}/change` | staff | 医生出诊变化（cancel/shorten/move/reassign） |
| `GET /api/queue?date=&point_id=` | staff | 开诊前的可执行队列（含容量与候补） |
| `GET /api/riders/{id}/cost-estimate?service=` | staff | 费用估算（注明来源） |
| `GET /api/teams/{id}/followups` | staff | 家庭医生团队的随访任务（跨站点跨日） |
| `POST /api/followups/{id}/complete` | staff | 完成随访 |
| `POST /api/identity/verifications` | verifier | 记录身份证明核验（独立存储） |
| `GET /api/health-records/{rid}` | clinician | 健康档案（独立存储） |
| `GET /api/union/coverage?date=` | union | 去标识化覆盖统计（仅计数） |
| `POST /api/reset` | admin | 清空业务状态（重放前复位） |

## 场景重放

`scenarios/` 中的资料会被重放，用于核对容量、费用依据、医疗优先级与跨日随访：

- `temp_orders.json`：临时派单改期，释放槽位回流，费用来源核对。
- `doctor_change.json`：医生停诊，紧急/较高优先级保住容量，普通预约进入候补。
- `cross_day_followup.json`：9 月 30 日转诊 → 10 月 3 日随访，次日跨站点到诊，团队接续不变。
- `concurrent_reschedule.json`：四个并发改期重放，恰好一个成功，其余 409。

场景格式：`riders`/`shifts` 为种子，`ops` 为操作序列（`as` 命名、`@引用`），`{"parallel": [...]}` 内的操作由多线程同时发起。

## 代码结构

```
riderclinic/
  models.py      实体、常量、时间工具、业务错误
  store.py       线程安全内存存储（身份核验与健康档案分开保存）
  scheduling.py  容量、匹配、优先级置换、改期规则、可执行队列
  billing.py     费用估算与来源说明
  followup.py    到诊转化生成随访任务
  privacy.py     去标识化覆盖统计
  app.py         用例编排：校验、幂等、并发控制
  http_api.py    HTTP 路由与工作角色校验
  replay.py      场景重放器
service.py       运行入口（/health、--check、--replay）
```
