# Arc-Chain 报表事件与格式化模块合流集成验收报告 (RESEARCH_REPORTING_REPAIR.md)

- **工单编号**: `TASK-REPORTING-INTEGRATION`
- **集成角色**: M1 总控与集成 (Master Control & Integration)
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **受审来源**: `/tmp/arc-reporting-impl/candidate`
- **前置凭证**: `/tmp/arc-reporting-review/RESULT.md` (独立沙箱审查建议准入；27 单测全通、54 联合全通、资金权威破坏反证转红、回放水印篡改反证转红)
- **交付结论**: **🟢 REPORTING_INTEGRATION_COMPLETED — 精确合流 4 项受试源码/测试、清单新增 3 项生产源码路径至 518、义务表测试哈希精准适配，35 项收集错误精准消除 1 项至 34 项，不主张全仓绿**

---

## 一、 精确装配文件与 SHA-256 现场哈希比对表

严格遵循“不修改 research 父 init、保 S1/S2 全部 dirty 成果、不触碰 core/types、仅合流 4 受审源码与对应测试”原则：

| 模块角色 | 目标文件路径 | 前态 SHA-256 / 状态 | 集成后 SHA-256 | 大小 (Bytes) | 行数 | 来源与对账校验 |
| :--- | :--- | :--- | :--- | :---: | :---: | :---: |
| **报表包导出** | `research/reporting/__init__.py` | *(不存在)* | `9f571f559f8e96dc422dea11b675db7c173892a00fa434fd3881be0e2037e841` | 2,906 | 103 | ✅ 100% MATCH |
| **审计事件模型** | `research/reporting/events.py` | *(不存在)* | `3426a409128f144bd32d39b6be9b4120ebef9c57b5554d10dba92c350c00d16c` | 17,204 | 497 | ✅ 100% MATCH |
| **格式化器实现** | `research/reporting/formatters.py` | *(不存在)* | `a01b196cbc2651c033f63b6e4bf4799f622f644c95dc6c5e579bc68053929c34` | 26,688 | 683 | ✅ 100% MATCH |
| **报表解阻单测** | `tests/test_reporting.py` | `c50c9d221e03e5b7b62a8dca25f14b2e2c7082a41fefba4417f9155850a927e9` | `e442dd7939703b8d1d6b70a96dfbf3704723c979b0519af8999cd249dc96d28f` | 26,110 | 703 | ✅ 100% MATCH (146 断言/拦截对齐) |
| **测试安全清单** | `scripts/test_safety_source_manifest.json` | `653db10b2f2feba3e217b93f42f5670efe0308b45eba62979a587275e32c61f7` (515 项) | `09444936d3c9f9c12070a727b4112079202df63576f7086134f5c3e1e205cd13` | 21,789 | 524 | ✅ 518 项 (严格排序无重) |
| **QA义务映射表** | `tools/qa/upstream_obligations.py` | `d3497f2495bc3b928f75d078f8634a43a5897b8bd13cd2fcba03dc6ffada5cdd` | `32e254a374a3b1f23c8ba1adb4dff18c3d6cb247dbec1bb9298cb32f1a9bd12c` | 52,295 | 1,440 | ✅ 仅更新 test_reporting 哈希 |

---

## 二、 清单与义务映射演进

### 1. 测试安全源文件清单 (`scripts/test_safety_source_manifest.json`)
- **基线项数**: 515 项 (含 S2 `research/planning.py` 1 项、S1 策略/快照 4 项)
- **新增登记**: 仅登记 3 个生产源码文件，单测文件不入生产源清单：
  - `+ "research/reporting/__init__.py"`
  - `+ "research/reporting/events.py"`
  - `+ "research/reporting/formatters.py"`
- **演进结果**: 515 $\to$ **518 项**，经 `set` 去重并字典序升序排列，保持原 `public_fixtures` 不变。

### 2. QA 义务映射表 (`tools/qa/upstream_obligations.py`)
- 维持 `KNOWN_ARC_ADAPTATIONS` 结构，精确更新 `tests/test_reporting.py` 适配预期值：
  ```python
  "tests/test_reporting.py": {
      "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
      "expected_sha256": "e442dd7939703b8d1d6b70a96dfbf3704723c979b0519af8999cd249dc96d28f",
  },
  ```
- **冻结守卫**: `docs/reuse/IMPORT_MANIFEST.json` 物理哈希严格保持 `97832326e49dbb15dc39bfb0db46c24a5c9aaed72ec6ed1dd659414e5fc5e211` 零触碰、零修改。

---

## 三、 强隔离操作系统级沙箱与复验执行证据

所有动态检验严格在 `bwrap` 物理隔离沙箱内运行，断开网络 (`--unshare-net`)、剥夺 capabilities (`--cap-drop ALL`)、清空宿主环境变量 (`--clearenv`)、私有临时挂载 (`--tmpfs /tmp`)，并以只读形式绑定基准工作树 (`--ro-bind <wt> /sandbox/src`)，杜绝任何中间状态回写污染。

### 1. 上游义务全面审计 (`audit281`)
- **命令**: `/sandbox/venv/bin/python tools/qa/upstream_obligations.py --audit`
- **结果**: **EXIT 0 (PASSED [OK])**
  - Total Checked: 281 项（Exact Matches: 204 项，Adapted Files: 77 项，Missing Files: 0 项）
  - Exclusion Rules Checked: 7 项（Violations: 0）
  - Test Obligation Parity: PASS
  - Sys.path Isolation: PASS
- **日志产物**: `/tmp/arc-reporting-integrate/logs/upstream_audit.log`

### 2. 核心联合测试套件 (Reporting + Planning + Domain + Manifest)
- **命令**: `/sandbox/venv/bin/python -m pytest tests/test_reporting.py tests/test_planning.py tests/test_domain_contracts.py tests/contracts/test_manifest_coverage.py tests/arc_v3/independent/test_import_manifest.py -v`
- **结果**: **EXIT 0 (113 passed in 0.88s)**
  - `tests/test_reporting.py`: 27 passed
  - `tests/test_planning.py`: 27 passed
  - `tests/test_domain_contracts.py`: 40 passed
  - `tests/contracts/test_manifest_coverage.py`: 17 passed
  - `tests/arc_v3/independent/test_import_manifest.py`: 2 passed
- **日志产物**: `/tmp/arc-reporting-integrate/logs/pytest_reporting_joint.log`

### 3. 全量联合靶向测试 (Targeted Joint All)
- **命令**: `/sandbox/venv/bin/python -m pytest tests/arc_v3/independent/test_market_snapshot_immutability.py tests/test_strategies.py tests/test_domain_contracts.py tests/test_market_data_catalog.py tests/test_planning.py tests/test_quoting.py tests/contracts/test_manifest_coverage.py tests/arc_v3/independent/test_import_manifest.py tests/test_market_data_pool_reader.py tests/test_v4_reader.py tests/test_multicall_reader.py tests/test_reporting.py -v`
- **结果**: **EXIT 0 (205 passed, 1 warning in 2.36s)**
  - 既有 178 项靶向测试 + 新增 27 项 reporting 测试 = 205 项全绿。
  - **保留 Warning 说明**: `websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated`（第三方包内部废弃告警，非生产代码异常）。
- **日志产物**: `/tmp/arc-reporting-integrate/logs/pytest_targeted_joint_all.log`

### 4. 全库静态检查 (Ruff & Mypy)
- **Ruff Check**:
  - **命令**: `/sandbox/venv/bin/python -m ruff check .`
  - **结果**: **EXIT 0 (All checks passed!)**
  - **日志产物**: `/tmp/arc-reporting-integrate/logs/ruff_full.log`
- **Mypy Type Check**:
  - **命令**: `/sandbox/venv/bin/python -m mypy .`
  - **结果**: **EXIT 0 (Success: no issues found in 454 source files)**
  - **保留 Note 说明**: 3 条关于无类型注解函数默认不检查的提示 (`tests/test_tick_cache.py:308, 334`, `tests/test_domain_contracts.py:789`)，与历史基线完全吻合。
  - **日志产物**: `/tmp/arc-reporting-integrate/logs/mypy_full.log`

### 5. 收集差集比对 (`pytest --collect-only`)
- **命令**: `/sandbox/venv/bin/python -m pytest --collect-only`
- **对比基线**: `/tmp/arc-s1-integrate/logs/collect_errors.txt` (35 项未解测试收集错误)
- **实测收集结果**:
  - 成功收集用例数: **2,098 项**（基线 2,071 项 + reporting 27 项）
  - 收集阻断错误数: **34 项**（由 35 项下降至 34 项，退出码 `exit 2` 符合预期）
- **差集对账 (`collect_errors.diff`)**:
  ```diff
  --- /tmp/arc-s1-integrate/logs/collect_errors.txt
  +++ /tmp/arc-reporting-integrate/logs/collect_errors.txt
  @@ -16,7 +16,6 @@
   ERROR tests/test_remaining_monitor_auditor.py
   ERROR tests/test_remaining_monitor_rpc.py
   ERROR tests/test_remaining_protocols.py
  -ERROR tests/test_reporting.py
   ERROR tests/test_robinhood.py
   ERROR tests/test_round2_regressions.py
   ERROR tests/test_round3_fixture_probe.py
  ```
  - **精准消除**: 仅消除 `ERROR tests/test_reporting.py` 1 项。
  - **无新错误引入**: 净增错误数为 0。
- **日志产物**: `/tmp/arc-reporting-integrate/logs/pytest_collect_only.log`、`collect_errors.txt`、`collect_errors.diff`

---

## 四、 S1/S2 历史成果与只读守卫保全证明

1. **S1 / S2 成果完整保全**:
   - `research/market_data/types.py`: 保留 S1 修改状态，哈希 `b0f8...`
   - `research/strategies/`: 保留 S1 目录与 3 个源码文件，哈希保持一致
   - `tests/test_strategies.py`: 保留 S1 修改状态，44 单测全通
   - `tests/test_domain_contracts.py`: 保留 S1 修改状态，40 契约测试全通
   - `tests/arc_v3/independent/test_market_snapshot_immutability.py`: 保留 S1 不可变快照测试，6 单测全通
   - `research/planning.py`: 保留 S2 规划实现，哈希 `fa12...`
   - `tests/test_planning.py`: 保留 S2 规划测试，27 单测全通
   - `docs/acceptance/RESEARCH_PLANNING_REPAIR.md` & `RESEARCH_STRATEGIES_REPAIR.md`: 保留完整验收活文档
2. **父级包与只读清册守卫**:
   - `research/__init__.py`: 严格保持只读，哈希 `0d9b4276824f4b26fd76a6b28fbdae40010a45e9fb56edfa1fca471aad023ece` 未覆盖。
   - `docs/reuse/IMPORT_MANIFEST.json`: 严格保持只读，哈希 `97832326e49dbb15dc39bfb0db46c24a5c9aaed72ec6ed1dd659414e5fc5e211` 冻结不变。

---

## 五、 安全红线与交付声明

- [x] **零真实资金与网络操作**：全程离线，零 RPC 节点通信，零私钥使用。
- [x] **物理沙箱隔离**：复用 review 阶段的独立 `bwrap` 强沙箱，未在宿主执行裸 pytest/裸 import。
- [x] **断言保全与零妥协**：原测试中 143 处 `assert` 与 3 处 `pytest.raises` 拦截 100% 结构对齐保全，未放宽容差或跳过用例。
- [x] **只读工作树保护**：动态检验通过 `--ro-bind` 挂载源码，零副作用写回。
- [x] **真实透明汇报**：如实记录全仓仍有 34 项历史 collection 错误与 1 项 deprecation warning，绝不虚报“全仓绿”。
- [x] **禁止越权提交**：本地无 git commit，无 git push。
