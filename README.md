# 电厂调度与能源分析与机组分析准入服务

本项目是一套可直接运行的 Python 服务端系统，用于记录电力市场基准电价、电厂与变电站设施、送出线路、燃料批次、发电计划和负荷情景，并保留机组巡检传感器统计分析准入流程。系统面向电价连续波动、关键送电送出线路恢复、电量调度和现场设备验证同时发生的运营环境，让调度、风险和审计人员在同一个 SQLite 数据库中获得可追溯结论。

供应调度子域提供以下能力：

- 电力市场基准电价按结算日和来源修订登记，历史版本不会被覆盖；
- 电厂、储罐、终端与储能站设施建档，送出线路保存日能力、在途时间和损耗规则；
- 送出线路停运或降容事件按 UTC 时间区间生效，日分配会计算实际可用能力；
- 燃料批次保留电源类型、牌号、数量、单位成本和接收时间，可计算加权燃料库存成本；
- 交易方提名支持载荷级幂等、优先级分配、燃料库存扣减和在途交接；
- 负荷情景保存电价变化、送出线路能力变化和需求变化，审批后产生可重放的确定性结果；
- 分时电价合同把签订时适用的峰/平/谷规则版本永久锁定，规则经历草稿、复核、生效、退役流程，发布前可按日期预览应付电价；
- 正式结算保存完整规则快照、输入电量和逐段舍入过程，账单不可覆盖、删除，重复结算返回同一账单；规则追溯更正只生成可追踪的重算建议，不改动历史账单；
- 关键写操作进入哈希串联审计日志，可离线验证事件顺序和内容完整性。

机组分析准入子域位于 `plant_science` 包，负责机组巡检传感器的设备构建登记、不可变校准协议、测点分片导入、异常测点复核、统计任务租约、分析准入决定和审计报告。该子域不连接传感器硬件，只处理已经结构化的校准记录。

## 目录

- `src/power_dispatch/`：电价、设施、送出线路、燃料库存、提名、负荷情景、HTTP API 与离线验收；
- `src/plant_science/`：机组巡检传感器校准与统计分析准入；
- `fixtures/`：机组分析准入演示协议和结构化测点；
- `tests/`：核心规则、错误边界、API 和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 无第三方运行依赖

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用内存数据库和临时目录，不访问公网，也不会启动常驻服务。

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m power_dispatch.acceptance --workspace .
```

该命令会在内存数据库中登记六个结算日的峰谷电价，创建电厂、终端和送出线路，完成燃料库存入账、提名分配、送电及负荷情景分析，最后输出一行 JSON。成功时退出码为 `0` 且 `status` 为 `ok`。

机组分析准入子域也保留独立验收入口：

```bash
PYTHONPATH=src python3 -m plant_science.acceptance --workspace .
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m power_dispatch.api --database power_dispatch.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖电价、设施、送出线路、停运事件、燃料批次、提名、能力分配、送电、负荷情景、分时电价合同/规则/账单和审计链。服务重启后，SQLite 中的业务状态和历史版本会继续保留。

## 分时电价规则与结算

规则按合同独立版本化，生命周期为 `draft → in_review → approved → active → retired`（复核不通过为 `rejected`）。每次修订（包括同一天内的多次修订）都追加新版本行，旧版本永不被覆盖。

规则定义示例：

```json
{
  "timezone": "Asia/Shanghai",
  "version_tag": "peak-valley-2026-10",
  "holidays": ["2026-10-01"],
  "effective_from": "2026-10-01",
  "periods": [
    {"kind": "valley", "day_type": "all",    "start": "22:00", "end": "06:00", "price_cny": "0.30"},
    {"kind": "flat",   "day_type": "all",    "start": "06:00", "end": "08:00", "price_cny": "0.70"},
    {"kind": "peak",   "day_type": "all",    "start": "08:00", "end": "22:00", "price_cny": "1.10", "holiday_add_cny": "0.20"}
  ]
}
```

- 时段按**规则时区的本地墙钟时间**定义，再整体换算为 UTC 半开区间 `[start, end)`；`22:00-06:00` 这类跨午夜时段自动向前翻卷，时区端点各自本地化，因此夏令时切换日也正确。
- 每张时段表（`all`/`workday`/`holiday`）必须用分钟位图恰好覆盖 `00:00–24:00`，重叠或空档在起草阶段即被拒绝。`holiday` 表若存在则在节假日整体替换 `all`；同一时段节假日只加价时使用 `holiday_add_cny`。
- 临界分钟由半开区间决定：例如本地 `08:00` 整分钟计入峰段，`07:59` 仍属平段。输入电量必须对齐整分钟、半开区间互不重叠，且完整覆盖规则日的 UTC 窗口。
- 表计段跨时段时按重叠分钟比例分配电量；每行同时保存 `allocated_kwh_raw → allocated_kwh` 和 `amount_raw → amount_cny`（`ROUND_HALF_UP`，电量 0.001 kWh、金额 0.01 元），合计再做一次分位舍入。

合同与结算语义：

- 合同创建后，需调用 `POST /tariff/contracts/{id}/pin` 把签订时适用的规则版本**锁定**（数据库触发器保证锁定后不可更改）。正式结算永远使用锁定版本，后续新规则不影响旧合同。
- `POST /tariff/rules/{version_id}/preview` 可在发布前对任意版本、任意日期试算，结果只记为 `preview`。
- `POST /tariff/contracts/{id}/settle` 产生 `official` 账单，保存规则快照（SHA-256）、规范化输入电量、逐段舍入过程和输入指纹。正式账单由触发器禁止 UPDATE/DELETE；相同（规则快照, 日期, 输入）或相同幂等键的重复结算返回同一账单（`replayed=true`），幂等键对应不同内容会冲突报错。
- 规则更正追溯到已结算日期时，新版本生效会对受影响的历史账单做影子重算，生成 `tariff_recalc_suggestions`（含每张账单的新金额与差额），只能被采纳或驳回并留痕，**绝不**改写原账单。

主要接口：合同 `POST/GET /tariff/contracts`、`.../pin`、`.../rules`、`.../bills`、`.../settle`；规则 `POST /tariff/rules/{id}/{submit,review,activate,retire,preview}`；重算建议 `GET /tariff/recalc-suggestions`、`POST /tariff/recalc-suggestions/{id}/decide`。
