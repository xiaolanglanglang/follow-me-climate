# 计划：功率动量门控（v0.3.0）

## 目标

为 follow-me-climate 增加可选的功率传感器输入。步进设定值后，若空调功率明显上升（压缩机已在出力），暂缓后续步进，等参考温度追上来，减少纯积分环因热惯性导致的过冲和来回摆动。**功率滞后是设计前提**：变频升频 + 智能插座上报聚合延迟意味着步进后约 3 分钟内功率证据不可信，算法必须先等证据窗口。

不做的事（明确排除）：饱和硬停、室外温度接入、指令丢失重发。

## 核心算法（controller.py，保持纯 Python）

新增可选注入 `PowerReader = Callable[[], tuple[float | None, float]]`（瓦数 + 帧龄秒），None 时功能完全关闭，行为与现状逐字节一致。

**常量**（放 const.py，v1 不暴露为用户选项）：
- `POWER_RESPONSE_LAG = 3 分钟`：下发后此窗口内不看功率、不判定
- 有效最小步进间隔 = `max(调节间隔, RESPONSE_LAG)`：即使 interval 设 1 分钟，功率启用时也不会在证据形成前再步进
- `POWER_RISE_RATIO = 0.30`、`POWER_RISE_MIN_W = 30W`：上升判定 = ΔP ≥ max(30%×基线, 30W)
- 释放滞回 = 上升阈值的一半（防边界抖动）
- `POWER_HOLD_TIMEOUT = 15 分钟`：保持超时安全网
- `POWER_STALE_TIMEOUT = 5 分钟`：功率失联判定

**状态**：`_power_readings`（deque maxlen 5，中位数滤波，复用 `_filtered()` 模式）、`_power_baseline`（上次下发时的滤波功率快照）、`_momentum_since`。

**决策点**（tick 内：速率门通过之后、真正步进之前插入）：
1. 功能关闭（无 reader / 功率不可用或失联 / dry_run）→ 照常步进
2. 距上次下发 < RESPONSE_LAG → hold，detail「等待功率响应」
3. ΔP = 滤波功率 − 基线：
   - ΔP ≥ 阈值 → 动量确认 → hold，detail「功率上升 x%，暂缓步进等温度响应」；释放条件：ΔP 跌破释放阈（滞回）、误差回死区、或 hold 超 15 分钟
   - 否则 → 步进，并把当前滤波功率快照为新基线

**防卡死双保险**：① 15 分钟 hold 超时；② 每次实际步进后基线重置为当前功率——高功率成为新基线，只有功率继续爬升才会再次触发门控，天然杜绝永久封锁。

**边界处理**：前馈下发同样快照基线并启动滞后计时；功率中途失联 → 立即释放 hold、门控失效、回退纯温度环；enable/disable/传感器失联/人工接管采用新基线时清理功率追踪状态；不新增 status 枚举值（动量 hold 表现为 `adjusting` + detail）。

## 改动范围与文件

| 文件 | 改动 |
|---|---|
| `const.py` | `CONF_POWER_ENTITY = "power_sensor"`；加入 `STRUCTURAL_KEYS`（指向变更自动 reload）；上述 POWER_* 常量 |
| `controller.py` | PowerReader 注入、功率滤波与基线状态、决策点逻辑、暴露 `power_w`/`power_baseline_w`/`power_gate` 读取属性 |
| `__init__.py` | `_build_controller` 内仿参考传感器闭包（:145-156 模式）构造功率读取闭包，传入 controller |
| `config_flow.py` | `vol.Optional` + `EntitySelector(device_class="power")` + suggested_value 预填（仿 :81-84） |
| `sensor.py` | status 传感器属性新增 `power_w`、`power_baseline_w`、`power_gate`（off/unavailable/waiting/holding/open） |
| `tests/test_controller.py` | 新增动量门控测试组（见下） |
| `README.md` | 配置表加「功率传感器（可选）」；动量门控小节；安全机制表补三条：滞后窗口、保持超时、功率失效回退 |
| `manifest.json` | version 0.2.3 → 0.3.0 |

switch.py / number.py / hacs.json 不动。

## 测试（纯 controller 单测，假时钟/假 reader 骨架直接复用）

- 回归：无 PowerReader 时全部现有测试原样通过（默认 None 天然保证）
- 滞后窗口：下发后 3 分钟内不因功率变化门控；interval=1 时有效间隔为 3 分钟
- 动量 hold：步进后功率升 40% → 下一决策点不写设定值；功率回落 → 恢复步进
- hold 超时 15 分钟 → 恢复步进且基线重置（紧接的决策不再被门控）
- 功率不可用/失联 → 门控失效、照常步进
- dry_run → 门控不参与
- 前馈下发快照基线；sensor_lost / 手动接管后状态清理

门槛：`python3 -m pytest tests/ -v` 全绿后才合并。

## 风险与取舍

- **阈值是猜测**：0.30/30W 基于智能插座 + 变频空调的典型量级，v1 收在 const 里，等你家 TCL 的真实数据再调参或暴露为选项
- **插座上报周期 > 3 分钟的极端情况**（重度聚合）滞后窗口可能仍偏短，const 可调、README 说明
- **制热化霜/风机换档扰动**：中位数滤波 + 相对阈值 + hold 超时三层缓冲
- **device_class=power 选择器**会过滤不规范的功率实体，先保持严格，遇到再放宽
- 动量 hold 会拉长收敛时间——这是避免过冲的特性，非缺陷，超时兜底

## 流程（按全局 AGENTS.md）

1. `git worktree add .worktrees/power-gate` 隔离开发，分支 `feature/power-gate`
2. 实现 + 单测全绿，`git add -A && git commit`
3. 主仓库 rebase + `merge --ff-only` 线性合入 main，删 worktree 及分支
4. 推 main → 推 tag v0.3.0 → 发布中文正式 Release（`## 新增`，含「升级方式」收尾）
5. 可选：home HA 经 HACS 更新到 v0.3.0 实测动量门控行为