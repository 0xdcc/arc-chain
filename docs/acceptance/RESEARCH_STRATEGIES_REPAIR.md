# Arc-Chain 策略引擎与不可变市场快照合流集成验收报告 (RESEARCH_STRATEGIES_REPAIR.md)

- **工单编号**: `TASK-S1-INTEGRATION`
- **集成角色**: M1 总控与集成 (Master Control & Integration)
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **受审来源**: `/tmp/arc-s1-admission-review/staging`
- **前置凭证**: `/tmp/arc-s1-admission-review/RESULT.md` (已独立准入 77 测试全通、AST 纯度负控 requests 突变反证转红)
- **交付结论**: **🟢 S1_INTEGRATION_COMPLETED — 精确合流 7 项受试源码/测试、清单新增 4 项路径至 515、义务表测试哈希精准适配，36 项收集错误精准消除 1 项至 35 项，不主张全仓绿**

---

## 一、 精确装配文件与 SHA-256 现场哈希比对表

严格遵循“不父 init、保 S2 五文件 dirty、不触碰 core/types、仅合流 7 正式受试源码/测试”原则：

| 模块角色 | 目标文件路径 | 集成后 SHA-256 | 大小 (Bytes) | 来源与对账校验 |
| :--- | :--- | :--- | :---: | :---: |
| **不可变市场快照类型** | `research/market_data/types.py` | `b0f8212d3631c33c3afa0302e6cdaf1ee9a4d82e5d4889853bfcb74dcba628ed` | 27,255 | ✅ 100% MATCH |
| **策略包导出** | `research/strategies/__init__.py` | `f6c83682a0febf6335635ec71f77159852d2fc1500a9783b0df71be3e2388e3c` | 292 | ✅ 100% MATCH |
| **价差策略实现** | `research/strategies/spread.py` | `6782a997fdde441c33bc674b783584c198c30c4c6c0c5d7f06e875f754cd50c4` | 8,862 | ✅ 100% MATCH |
| **三角套利策略实现** | `research/strategies/triangular.py` | `1274ab3a2225dd4c58dcc0e706379d3024235c53454746a89443ef420bbe51ff` | 10,489 | ✅ 100% MATCH |
| **策略单测** | `tests/test_strategies.py` | `c6c80f3fed1db7bca4746e112ead931f34dc12e711e392c744ae3be75e8db94f` | 32,506 | ✅ 100% MATCH |
| **领域契约测试** | `tests/test_domain_contracts.py` | `4339fb9300fbec2620ff551ffe6a7353fc966c7b2cd455dc7158e0d16425621b` | 29,712 | ✅ 100% MATCH |
| **独立不可变性单测** | `tests/arc_v3/independent/test_market_snapshot_immutability.py` | `0882d08b570514670939a107cb28e46f2afaaf49f913cbf266316edf6f6dd7fd` | 15,268 | ✅ 100% MATCH |
| **S2 规划实现 (保Dirty)** | `research/planning.py` | `fa12b11bbfd097ec122c64eab944e5543eb46c13b1c2f59ca90d941279309871` | 8,390 | ✅ 100% PRESERVED |
| **S2 规划测试 (保Dirty)** | `tests/test_planning.py` | `feddeb93859fa5dc6e4c3269464458bf2809c5e3484e1b0e3ce4355b454089d7` | 31,424 | ✅ 100% PRESERVED |
| **测试安全清单** | `scripts/test_safety_source_manifest.json` | `9df474aa9de0ad9a1ad2ef9138b7462263f1b67e4fc26d375c2b61201c117d58` | 21,725 | ✅ 515项 (排序无重) |
| **QA义务映射表** | `tools/qa/upstream_obligations.py` | `674a70a4fb6a1aa74311ddb3e27157d0342e66f6cf664ce3dc06296f3c606999` | 52,408 | ✅ 仅更新 domain/strategies |

---

## 二、 边界保护与零污染核验 (Zero Contamination)

1. **S2 成果保护**: S2 五文件（`research/planning.py`、`tests/test_planning.py`、`scripts/test_safety_source_manifest.json`、`tools/qa/upstream_obligations.py`、`docs/acceptance/RESEARCH_PLANNING_REPAIR.md`）严格保留 dirty，未发生 reset，未覆盖回旧哈希 `2aa0`，S2 适配表哈希保全。
2. **父包入口保护**: `research/__init__.py` 零修改；未创建任何不必要的父 `__init__.py`。
3. **既有清单保护**: `docs/reuse/IMPORT_MANIFEST.json`（哈希冻结 `97832326e49dbb15dc39bfb0db46c24a5c9aaed72ec6ed1dd659414e5fc5e211`）、`docs/reuse/EXCLUSION_MANIFEST.json`、`docs/reuse/TEST_OBLIGATION_MAP.json` 保持 100% 冻结，零修改。
4. **Git 状态**: 零 `git commit`、零 `git push`，工作区严格限定于获准修改的文件。
5. **资金与网络红线**: 零 RPC 网络调用、零真实资金/交易操作、默认 `ARMED = False`。

---

## 三、 清单增量与 QA 上游义务表核对

### 1. 源码清单 (`scripts/test_safety_source_manifest.json`)
- **改动方式**: 纳入 4 项适用新路径：
  - `research/strategies/__init__.py`
  - `research/strategies/spread.py`
  - `research/strategies/triangular.py`
  - `tests/arc_v3/independent/test_market_snapshot_immutability.py`
- **全量计数**: 从 511 文件增至 515 文件；
- **排序与查重**: 严格保持 ASCII/Unicode 升序，经 `sorted(list(set(files)))` 归一，重复项为 0。
- **契约测试通过**: `tests/contracts/test_manifest_coverage.py` 5/5 PASSED，1:1 对账完全一致。

### 2. 义务表 (`tools/qa/upstream_obligations.py`)
- **改动方式**:
  - `tests/test_domain_contracts.py`: 更新 `expected_sha256` 为 `4339fb9300fbec2620ff551ffe6a7353fc966c7b2cd455dc7158e0d16425621b`，`reason` 更新为 `"QUALITY-TYPES-R2 ast purity predicate allowing types and collections.abc with negative controls, original domain contract assertions preserved"`；
  - `tests/test_strategies.py`: 更新 `expected_sha256` 为 `c6c80f3fed1db7bca4746e112ead931f34dc12e711e392c744ae3be75e8db94f`，`reason` 更新为 `"LINT-REMAINING-R1 equivalent specification cleanup and I001 import format with original assertions preserved"`；
  - `tests/test_planning.py`: 保持 S2 最新哈希 `feddeb93859fa5dc6e4c3269464458bf2809c5e3484e1b0e3ce4355b454089d7`；
  - `IMPORT_MANIFEST.json` 冻结哈希 `97832326e49dbb15dc39bfb0db46c24a5c9aaed72ec6ed1dd659414e5fc5e211` 冻结不变。
- **上游 281 文件 Audit 运行结果**:
  - 命令: `/sandbox/venv/bin/python tools/qa/upstream_obligations.py --audit`
  - Exit Code: `0`
  - Expected Files: 281 / Found Files: 281 / Exact Matches: 204 / Adapted Files: 77 / Missing: 0
  - Status: `PASSED [OK]`

---

## 四、 窄挂载强隔离沙箱执行凭据 (Bubblewrap Sandbox)

沙箱参数: `--die-with-parent --unshare-net --unshare-pid --unshare-ipc --unshare-uts --cap-drop ALL --clearenv`，挂载 uv cpython-3.12 运行时与私有 `/tmp`。

| 验证阶段 | 测试用例模块 | 命令概要 | 结果 | 耗时 | Exit Code |
| :--- | :--- | :--- | :--- | :---: | :---: |
| **S1 四套件** | `test_market_snapshot_immutability.py`<br>`test_strategies.py`<br>`test_domain_contracts.py`<br>`test_market_data_catalog.py` | `pytest <S1四套件> -v` | **77 passed** | 0.35s | `0` |
| **S2 + Quoting** | `test_planning.py`<br>`test_quoting.py` | `pytest tests/test_planning.py tests/test_quoting.py -v` | **41 passed, 1 warning** | 0.99s | `0` |
| **Manifest 体系** | `test_manifest_coverage.py`<br>`test_import_manifest.py` | `pytest tests/contracts/test_manifest_coverage.py tests/arc_v3/independent/test_import_manifest.py -v` | **16 passed** | 0.45s | `0` |
| **Market Readers** | `test_market_data_pool_reader.py`<br>`test_v4_reader.py`<br>`test_multicall_reader.py` | `pytest <Readers三套件> -v` | **44 passed, 1 warning** | 0.99s | `0` |
| **全套件跨模块联合** | 上述所有 11 个模块联合单次执行 | `pytest <全量11套件> -v` | **178 passed, 1 warning** | 2.03s | `0` |
| **上游 281 审计** | `tools/qa/upstream_obligations.py` | `python tools/qa/upstream_obligations.py --audit` | **281/281 PASSED [OK]** | 0.44s | `0` |

---

## 五、 全库 Collect 收集错误差集分析 (差集审计)

在沙箱中运行全库 `pytest --collect-only`，比对 S2 合流后态与 S1 合流后态的收集错误差集：

- **S2 合流态基线 (Baseline)**:
  - 收集成功用例数: `2054 tests collected`
  - 收集错误数: `36 errors during collection`
  - Exit Code: `2`
- **S1 合流后态 (Post-Integration)**:
  - 收集成功用例数: `2071 tests collected` (+17 tests，来自 `tests/test_strategies.py` 与 `test_market_snapshot_immutability.py`)
  - 收集错误数: `35 errors during collection` (-1 error，精准消除 `tests/test_strategies.py`)
  - Exit Code: `2`
- **差集解析**:
  - `Resolved Errors`: `['ERROR tests/test_strategies.py']`
  - `New Errors`: `[]` (零新增错误)
  - 差集精准收敛：36 -> 35，仅 `tests/test_strategies.py` 修复消除，无任何副作用新增。

### 剩余 35 项存量收集错误清单 (诚实披露，不主张全仓绿)
1. `tests/test_arbitrage_daemon.py`
2. `tests/test_candidate_execution_integration.py`
3. `tests/test_candidate_fee_integration.py`
4. `tests/test_capacity_accuracy_and_tvl.py`
5. `tests/test_concurrent_reader.py`
6. `tests/test_execution_service_reconciliation.py`
7. `tests/test_feed_listener.py`
8. `tests/test_fire_gate_and_new_dex.py`
9. `tests/test_fix_cycle_and_profit_anomaly.py`
10. `tests/test_funds_coordinator.py`
11. `tests/test_guard.py`
12. `tests/test_pool_scanner.py`
13. `tests/test_public_runtime_binding.py`
14. `tests/test_readonly_monitor_app.py`
15. `tests/test_remaining_funds.py`
16. `tests/test_remaining_monitor_auditor.py`
17. `tests/test_remaining_monitor_rpc.py`
18. `tests/test_remaining_protocols.py`
19. `tests/test_reporting.py`
20. `tests/test_robinhood.py`
21. `tests/test_round2_regressions.py`
22. `tests/test_round3_fixture_probe.py`
23. `tests/test_round4_feedback.py`
24. `tests/test_round5_phase_feedback.py`
25. `tests/test_round6_price_phase.py`
26. `tests/test_round6_upstream_phase.py`
27. `tests/test_safety_bootstrap.py`
28. `tests/test_spread_monitor.py`
29. `tests/test_spread_overflow_and_anomaly.py`
30. `tests/test_tax_guard.py`
31. `tests/test_tick_cache.py`
32. `tests/test_triangular_arb.py`
33. `tests/test_usdg_arbitrage_executor.py`
34. `tests/test_v4_pipeline_integration.py`
35. `tests/test_weth_arbitrage_executor.py`

---

## 六、 全库与靶向静态代码规范检查 (Ruff & Mypy)

- **靶向 Ruff 检查**:
  - 命令: `python -m ruff check research/strategies research/market_data/types.py tests/test_strategies.py tests/test_domain_contracts.py tests/arc_v3/independent/test_market_snapshot_immutability.py`
  - 结果: `All checks passed!` (Exit Code: `0`)
- **全库 Ruff 检查**:
  - 命令: `python -m ruff check .`
  - 结果: `All checks passed!` (Exit Code: `0`)
- **靶向 Mypy 检查**:
  - 命令: `python -m mypy research/strategies research/market_data/types.py tests/test_strategies.py tests/test_domain_contracts.py tests/arc_v3/independent/test_market_snapshot_immutability.py`
  - 结果: `Success: no issues found in 7 source files` (Exit Code: `0`)
- **全库 Mypy 检查**:
  - 命令: `python -m mypy .`
  - 结果: `Success: no issues found in 451 source files` (Exit Code: `0`)

---

## 七、 交付物落盘索引

- 验收报告: `docs/acceptance/RESEARCH_STRATEGIES_REPAIR.md`
- 证据目录: `/tmp/arc-s1-integrate/`
  - `RUNNING.md` (全流程每步即时退出码流水)
  - `RESULT.md` (全量验证报告)
  - `logs/pytest_s1_combined.log`
  - `logs/pytest_s2_planning_quoting.log`
  - `logs/pytest_manifest_contracts_independent.log`
  - `logs/pytest_market_readers.log`
  - `logs/pytest_targeted_joint_all.log`
  - `logs/upstream_audit.log`
  - `logs/pytest_collect_only.log`
  - `logs/collect_errors.txt`
  - `logs/ruff_targeted.log`
  - `logs/ruff_full.log`
  - `logs/mypy_targeted.log`
  - `logs/mypy_full.log`
