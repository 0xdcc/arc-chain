# Arc-Chain 研究规划与风险护栏合流集成验收报告 (RESEARCH_PLANNING_REPAIR.md)

- **工单编号**: `TASK-S2-INTEGRATION`
- **集成角色**: M1 总控与集成 (Master Control & Integration)
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **受审来源**: `/tmp/arc-s2-precision-review/staging`
- **前置凭证**: `/tmp/arc-s2-precision-review/RESULT.md` (已独立复现 27 测试全通、浮点回退反证转红、缺值假零反证转红)
- **交付结论**: **🟢 S2_INTEGRATION_COMPLETED — 精确合流 2 项受试源码、清单新增 1 项路径至 511、义务表测试哈希精准适配，37 项收集错误精准消除 1 项至 36 项，不主张全仓绿**

---

## 一、 精确装配文件与 SHA-256 现场哈希比对表

严格遵循“不父 init、不触碰 S1、不修改 core/types、仅合流 2 正式受试源码”原则：

| 模块角色 | 目标文件路径 | 前态 SHA-256 / 状态 | 集成后 SHA-256 | 大小 (Bytes) | 来源与对账校验 |
| :--- | :--- | :--- | :--- | :---: | :---: |
| **S2 规划实现** | `research/planning.py` | *(不存在)* | `fa12b11bbfd097ec122c64eab944e5543eb46c13b1c2f59ca90d941279309871` | 8,390 | ✅ 100% MATCH |
| **S2 规划测试** | `tests/test_planning.py` | `db9a04ecd5d51d308254f45d071a48a1c1c812ca75df34f45714defb629bbff1` | `feddeb93859fa5dc6e4c3269464458bf2809c5e3484e1b0e3ce4355b454089d7` | 31,424 | ✅ 100% AST MATCH (isort 收尾) |
| **测试安全清单** | `scripts/test_safety_source_manifest.json` | `734c51639d67568551da74d39f4d7f7811928097f48a80479b122e205ae92d00` (510项) | `653db10b2f2feba3e217b93f42f5670efe0308b45eba62979a587275e32c61f7` | 21,489 | ✅ 511项 (排序无重) |
| **QA义务映射表** | `tools/qa/upstream_obligations.py` | `4859f5f0fb01f5c3a3bb02e75e95a9b706c9e03d76e73db63ffbf18a09a5ae31` | `d3497f2495bc3b928f75d078f8634a43a5897b8bd13cd2fcba03dc6ffada5cdd` | 52,238 | ✅ 仅更新 expected_sha256 |

---

## 二、 边界保护与零污染核验 (Zero Contamination)

1. **S1 隔离保护**: S1 依旧仅留存 `/tmp`，严禁且未修改 `types` / `core` 等未授权文件。
2. **父包入口保护**: `research/__init__.py` (SHA256: `0d9b4276824f4b26fd76a6b28fbdae40010a45e9fb56edfa1fca471aad023ece`) 零修改、零重新生成。
3. **既有清单保护**: `docs/reuse/IMPORT_MANIFEST.json`、`docs/reuse/EXCLUSION_MANIFEST.json`、`docs/reuse/TEST_OBLIGATION_MAP.json` 保持 100% 冻结，零修改。
4. **Git 状态**: 零 `git commit`、零 `git push`，工作区严格只包含本次获准修改的文件。
5. **资金与网络红线**: 零 RPC 网络调用、零真实资金/交易操作、默认 `ARMED = False`。

---

## 三、 清单增量与 QA 上游义务表核对

### 1. 源码清单 (`scripts/test_safety_source_manifest.json`)
- **改动方式**: 仅新增 `research/planning.py` 单一路径；
- **全量计数**: 从 510 文件增至 511 文件；
- **排序与查重**: 严格保持 ASCII/Unicode 升序，位于 `research/market_data/v4_reader.py` 与 `research/quoting/__init__.py` 之间，重复项为 0。

### 2. 义务表 (`tools/qa/upstream_obligations.py`)
- **改动方式**: 仅更新 `KNOWN_ARC_ADAPTATIONS` 中 `tests/test_planning.py` 的 `expected_sha256`（合流初态 `2aa0bcbce52631af648fce148b4b30b4a8590b3b844a0860c69ad08ce2d052a9`，收尾修复 isort 排序后同步为 `feddeb93859fa5dc6e4c3269464458bf2809c5e3484e1b0e3ce4355b454089d7`）；
- **审核规则与冻结项**: 审核规则、检查逻辑、`ALLOWED_POOL_CATALOG_PATHS`、`LEGACY_TEST_MIGRATION_REGISTRY` 零变更。
- **上游 281 文件 Audit 运行结果**:
  - 命令: `/sandbox/venv/bin/python tools/qa/upstream_obligations.py --audit`
  - Exit Code: `0`
  - Expected Files: 281 / Found Files: 281 / Exact Matches: 204 / Adapted Files: 77 / Missing: 0
  - Status: `PASSED [OK]`

---

## 四、 窄挂载强隔离沙箱执行凭据 (Bubblewrap Sandbox)

执行环境采用严格 bwrap 强隔离沙箱（`--unshare-net --unshare-pid --unshare-ipc --unshare-uts --cap-drop ALL --clearenv`，挂载 uv cpython-3.12 运行时与私有 `/tmp`）。

### 1. 靶向联合测试集验证 (S2 + domain + quoting + manifest)
| 测试用例模块 | 命令 | 结果 | 耗时 | Exit Code |
| :--- | :--- | :--- | :---: | :---: |
| **S2 规划测试** (`tests/test_planning.py`) | `pytest tests/test_planning.py -v` | 27 passed | 0.15s | `0` |
| **领域契约测试** (`tests/test_domain_contracts.py`) | `pytest tests/test_domain_contracts.py -v` | 42 passed | 0.17s | `0` |
| **报价测试** (`tests/test_quoting.py`) | `pytest tests/test_quoting.py -v` | 14 passed | 0.89s | `0` |
| **清单契约测试** (`tests/contracts/test_manifest_coverage.py`) | `pytest tests/contracts/test_manifest_coverage.py -v` | 5 passed | 0.31s | `0` |
| **清单独立测试** (`tests/arc_v3/independent/test_import_manifest.py`) | `pytest tests/arc_v3/independent/test_import_manifest.py -v` | 11 passed | 0.17s | `0` |
| **五模块联合靶向套件** | `pytest <上述5文件> -v` | **99 passed, 1 warning** | 1.58s | `0` |

---

## 五、 全库 Collect 收集错误差集分析 (差集审计)

在沙箱中运行全库 `pytest --collect-only`，比对合流前后的收集错误差集：

- **基线前态 (Baseline)**:
  - 收集成功用例数: `2027 tests collected`
  - 收集错误数: `37 errors during collection`
  - Exit Code: `2`
- **合流后态 (Post-Integration)**:
  - 收集成功用例数: `2054 tests collected` (+27 tests，精准来自 `tests/test_planning.py`)
  - 收集错误数: `36 errors during collection` (-1 error，精准消除 `tests/test_planning.py`)
  - Exit Code: `2`
- **差集解析**:
  - `Resolved Errors`: `['tests/test_planning.py']`
  - `New Errors`: `[]` (零新增错误)
  - 差集精确符合预期：仅 `tests/test_planning.py` 修复消除，无任何副作用新增。

### 剩余 36 项存量收集错误清单 (诚实披露，不主张全仓绿)
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
30. `tests/test_strategies.py`
31. `tests/test_tax_guard.py`
32. `tests/test_tick_cache.py`
33. `tests/test_triangular_arb.py`
34. `tests/test_usdg_arbitrage_executor.py`
35. `tests/test_v4_pipeline_integration.py`
36. `tests/test_weth_arbitrage_executor.py`

---

## 六、 静态代码检查与类型分析 (Ruff & Mypy)

### 1. Mypy 全库静态类型分析
- **全库命令**: `/sandbox/venv/bin/python -m mypy .`
- **全库结果**: `Success: no issues found in 447 source files` (Exit Code: `0`)
- **靶向命令**: `/sandbox/venv/bin/python -m mypy research/planning.py tests/test_planning.py`
- **靶向结果**: `Success: no issues found in 2 source files` (Exit Code: `0`)

### 2. Ruff 静态代码分析
- **实现源码 `research/planning.py`**: `ruff check research/planning.py` -> `All checks passed!` (Exit Code: `0`)
- **测试用例 `tests/test_planning.py`**:
  - 初次合流时在主仓根下由于 `atomic_execution` 本地包排序触发 `I001 [*] Import block is un-sorted`（Exit Code `1`）。
  - 收尾执行根配置定点 isort 修复：将 `from atomic_execution.policy import ExcessiveAmountError` 调整至首方包首位排序。
  - **严格 AST 机械等价性核验**:
    - 非 import 语句 AST 节点 dump 完全等价 (`ast.dump(m_old) == ast.dump(m_new)` 为 `True`，长度 74,583 字符 100% 一致)；
    - 导入符号集合完全一致（20 项模块与名称映射 100% 一致）；
    - 规范化排序后全模块 AST dump 完全等价 (`True`)。
    - 证明格式修正零语义改动、零断言弱化、零逻辑漂移。
- **全库 Ruff 校验结果**:
  - 沙箱全库命令: `/sandbox/venv/bin/python -m ruff check .`
  - 真实 Exit Code: `0` (`All checks passed!`)
  - 全仓 Ruff 违规项从 1 处降为 0 处，全库静态检查完全收口。

---

## 七、 证据日志清单与 SHA-256 归档表

### 1. 合流初态证据目录 (`/tmp/arc-s2-integrate/logs/`)

所有执行日志均已即时持久化至证据目录：

| 日志文件名 | 大小 (Bytes) | SHA-256 哈希值 | 说明 |
| :--- | :---: | :--- | :--- |
| `pytest_s2_planning.log` | 4,675 | `65979972528e16beeb311809ebeb1196b49d1a4c1b769b46cd3eb990af2341ce` | S2 27项用例全绿日志 (Exit Code: 0) |
| `pytest_domain.log` | 6,488 | `55dc177947077c9e17d29c480f57dfa2c2084e722ade5f8ed587365f20220ceb` | 领域契约42项全绿日志 (Exit Code: 0) |
| `pytest_quoting.log` | 3,752 | `60b10e21e5d45f3bd66c0491f6260cddaf2f352e0d22a7c58c8a44a6127672e0` | 报价14项全绿日志 (Exit Code: 0) |
| `pytest_manifest_contracts.log` | 2,284 | `e7b8805afa969227baea20e7f34e74944f930ebdd4e597ba6812492d0b364729` | 清单契约5项全绿日志 (Exit Code: 0) |
| `pytest_manifest_independent.log` | 3,153 | `3fa33185bd16ea2c2809961df6f42d894104a505ec1441886ab394d19048216a` | 清单独立11项全绿日志 (Exit Code: 0) |
| `pytest_targeted_joint.log` | 13,685 | `42a8b134d9c30e1e9a0de8dfd6a7bcc461f359918f4fb8155f7437d4ead07499` | 五模块联合99项全绿日志 (Exit Code: 0, 99 passed) |
| `upstream_audit.log` | 13,660 | `defcc0f37d822c631c1a70774453d6f4b24da5dfd7dc07b83cf844e986698fb1` | QA 281文件审计全绿日志 (初态) |
| `pytest_collect_only.log` | 171,510 | `9ba36b68953bb3b6e5b02e8fc2f271c9ca88d8c531012c0a9fe5c4441db6e859` | 全库collect后态日志 (Exit Code: 2, 2054 collected, 36 errors) |
| `collect_errors.txt` | 1,420 | `4a63c16bf01ca8c5b66d664b9505e76f58a25c90cb732870df164f902995ba61` | 剩余36项收集错误清单 (诚实披露未行刷绿) |
| `sandbox_mypy_full.log` | 1,882 | `4035bcd0c3c3a204de84b8f00f83d7529247cb6b16c70f06909a7e27bd837a48` | 全库mypy 447文件全绿日志 (Exit Code: 0) |
| `sandbox_mypy_targeted.log` | 1,286 | `c100f6f5f292142636642918c50eeed04115a637737c26b5f511bc174e9c1d9c` | 靶向mypy 2文件全绿日志 (Exit Code: 0) |
| `sandbox_ruff_targeted.log` | 2,657 | `5d414fdffe75cc0aa85852bbae9b6c45abe1ed6c89ea2ef95a35d2181cd4b7bb` | 靶向ruff日志 (初态含I001, Exit Code: 1) |
| `sandbox_ruff_full.log` | 2,615 | `766d132c32c76388620f495abf4e806fd487fd6c9a46d3e81cc861229bec0b89` | 全库ruff日志 (初态含I001, Exit Code: 1) |

### 2. 收尾格式修复与验证证据目录 (`/tmp/arc-s2-isort-final/`)

| 日志/差集文件名 | 大小 (Bytes) | SHA-256 哈希值 | 说明 |
| :--- | :---: | :--- | :--- |
| `sandbox_ruff_full.log` | 19 | `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18` | 全库 Ruff 检查全通日志 (Exit Code: `0`, All checks passed!) |
| `upstream_audit.log` | 12,510 | `83a4e38d87d36040b46cdfb3d812e7d1371909bf47259ddccf4aaf55e1246b24` | 上游义务 281 文件 Audit 日志 (Exit Code: `0`, PASSED [OK]) |
| `ast_verification.log` | 964 | `89166a162e1c40731dd2a30d1f9552e32deb9704e5ad137a7c4c74ef1276a51f` | AST 严格机械等价性审计日志 (三项对比均为 True) |
| `test_planning_isort_delta.diff` | 650 | `735459f6be7f350fe0da5f20a50138017f355109034d99bd454e75c3c0494897` | `tests/test_planning.py` 格式修复最小 diff |
| `upstream_obligations.diff` | 765 | `f24b6a5464f30c51e5ed3810d643208c21422a8bceee67e352ac85b16d63627a` | `tools/qa/upstream_obligations.py` 测试哈希更新 diff |

---

## 八、 综合验收核准声明

1. **S2 靶向联合套件**: 99 项用例全部通过 (`99 passed, 1 warning`, Exit Code: `0`)；
2. **全库 Collect 收集状态**: 2054 collected, 36 errors（历史存量 36 项错误如实披露，未行删除或刷绿，Exit Code: `2`）；
3. **全库类型分析**: Mypy 447 source files 全部通过 (`no issues found`, Exit Code: `0`)；
4. **全库代码规范**: Ruff 本次格式修复后 Exit Code: `0` (`All checks passed!`)，I001 告警彻底收口；
5. **上游审计与清单**: 281 项导入清单与义务映射表严格符合，Exit Code: `0` (`PASSED [OK]`)。
