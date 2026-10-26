# Follow-Me Climate：温度滑块 + 空调关机联动 + 步长跟随空调

## 目标
1. Dashboard 上「目标温度」「调节步长」两个 number 实体从输入框改为**滑块**。
2. 选中的空调从制冷/制热变为**关机**时，随身感开关**自动关闭**——可配置开关，默认开启；空调重新开机时**不**自动恢复（已确认）。
3. 「调节步长」的**默认值读取空调自身的步长**（climate 实体的 `target_temp_step` 属性，如 0.5/1.0）；空调不暴露该属性时维持默认 0.5。

## 改动范围（8 个文件）

### 需求 1 + 3：滑块与步长（`number.py`）
- `_attr_mode = NumberMode.BOX` → `NumberMode.SLIDER`
- 两个 number 实体的滑块步进（`native_step`）对齐空调的 `target_temp_step`（无则 0.5），拖动粒度与空调一致

### 需求 2：空调关机联动

**`const.py`**
- 新增 `CONF_FOLLOW_POWER = "follow_climate_power"`、`DEFAULT_FOLLOW_POWER = True`、`HVAC_OFF = "off"`
- `CONF_FOLLOW_POWER` 加入 `RUNTIME_KEYS`（选项修改后热生效，不重载集成）

**`controller.py`**（纯逻辑层，无 HA 依赖）
- `ControllerConfig` 增加 `follow_power: bool = DEFAULT_FOLLOW_POWER`
- `update_runtime()` 应用该键

**`__init__.py`**（联动核心 + 步长读取）
- `_build_controller`：
  - 读取 `CONF_FOLLOW_POWER` 传入配置
  - 读取空调实体的 `target_temp_step` 属性；**仅当用户从未手动改过步长**（`entry.options` 中没有 `CONF_STEP`；number 实体被用户拖动后会写入 options）时，用它作为控制步长初始值，并 clamp 到 [0.5, 2.0]——用户一旦手动设置过，永远以用户值为准
- `async_setup_entry` 用 `async_track_state_change_event` 监听空调实体：
  - 触发条件：联动开启且 controller 处于 enabled，旧状态 ∈ {cooling, heating}，新状态 == `off`
  - 动作：`hass.async_create_task(controller.disable(restore=True))`，异常仅记日志
  - 恢复语义与手动关开关**完全一致**（写回 enable 时快照的默认设定温度；绝大多数 climate 集成关机态下 `set_temperature` 只记忆不启机；dry_run 不写入）
  - `unavailable`/`unknown` 不触发联动：避免向离线实体写恢复值、避免短暂掉线误关
- `entry.async_on_unload` 注销监听

**`config_flow.py`**
- `_build_schema` 增加 `CONF_FOLLOW_POWER: BooleanSelector()`（放 dry_run 附近），config 与 options 流共用同一 schema 自动生效

**`translations/en.json` + `zh-Hans.json`**
- config 和 options 两处 `data` 各加一条：en "Turn off when the AC turns off" / zh 「空调关闭时自动停用」

**`tests/test_controller.py`**
- 新增用例：`follow_power` 默认 True；`update_runtime({"follow_climate_power": False})` 后变 False

**`manifest.json`**
- `version` 0.1.1 → 0.2.0

## 关键步骤
1. 在 `.worktrees/` 下建 worktree 隔离实现（遵守全局规则）
2. 实现上述改动
3. `pytest` 验证（现有测试 + 新增用例必须全绿）
4. worktree 内提交 → 回主仓库 `merge --ff-only` → 删除 worktree 与分支
5. 打 `v0.2.0` tag 并推送 GitHub Release（如想先在家里的 HA 真机验证再发布，这步可最后砍掉）

## 风险与取舍
- **步长来源语义**：只影响「默认值」——用户在滑块上改过步长后即锁定用户值；重启/重载会重新读取空调步长（用户未改过时空调步长变了也跟着变，属预期）
- **关机后写回设定温度**：与手动关开关同路径；若个别空调集成在 off 态收到 set_temperature 行为异常，联动路径可改 `restore=False`（不建议，会造成两种"关"语义不一致）
- **IR 网关类设备关机变 unavailable 而非 off**：不触发联动（控制器 tick 会显示 inactive、不写入）；确有需要后续可扩展
- 无 HA 测试基建（requirements 仅 pytest），`__init__.py` 中的事件联动与步长读取无法单测，靠 controller 层测试 + 真机验证覆盖