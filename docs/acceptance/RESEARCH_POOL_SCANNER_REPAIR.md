# Arc-Chain 池扫描模块合流集成验收报告 (RESEARCH_POOL_SCANNER_REPAIR.md)

- **工单编号**: `TASK-SCANNER-INTEGRATION`
- **集成角色**: M1 总控与集成 (Master Control & Integration)
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **受审来源**: `/tmp/arc-scanner-r2/`
- **前置凭证**: `/tmp/arc-scanner-r2/RESULT.md` (已闭环修复并经沙箱 65 pass、变异反证 missinghooks 1 fail 24 pass)
- **交付结论**: **🟢 SCANNER_INTEGRATION_COMPLETED — 精确合流 2 项受试源码/测试、源清单维持 518 条目无需新增、义务表测试哈希精准适配，34 项收集错误精准消除 1 项至 33 项，真实差集闭环，不主张全仓绿**

---

## 一、 精确装配文件与 SHA-256 现场哈希比对表

严格遵循“不修改 research 父 init、保 S1/S2/reporting 全部 dirty 成果、不新增 source 文件维持 518 规则、仅合流 2 受审源码与对应测试、仅更新 obligations 对应哈希与理由”原则：

| 模块角色 | 目标文件路径 | 前态 SHA-256 / 状态 | 集成后 SHA-256 | 大小 (Bytes) | 行数 | 来源与对账校验 |
| :--- | :--- | :--- | :--- | :---: | :---: | :---: |
| **费率扫描生产实现** | `research/market_data/fee_scan.py` | `0507204910cf9dbb8eb91c7ae9bfd0a0d9b4bebc9f0616b328a6fcf767eeea4c` | `14ed3a6d2d0cfe48d71f4bc3501318088fa27900e9b927f1616dc47b9edd8751` | 35,336 | 873 | ✅ 100% MATCH |
| **池扫描回归单测** | `tests/test_pool_scanner.py` | `d924fd2db5dac7b2b700ed43320904bd246d392bc9d29b74ddec0d94f1e1028d` | `b6eda3c3e7cbabd8fa0decc27eaec84ad20c873435ab4bfff386011e776ebd74` | 21,767 | 629 | ✅ 100% MATCH (25用例/62asserts/7raises) |
| **QA义务映射表** | `tools/qa/upstream_obligations.py` | `32e254a374a3b1f23c8ba1adb4dff18c3d6cb247dbec1bb9298cb32f1a9bd12c` | `eacb9408040b5fe94d8028c72755c44c99a3ee041a7b124cefe172e2b57c2c81` | 52,346 | 1,098 | ✅ 仅更新 test_pool_scanner 哈希与理由 |
| **测试安全源清单** | `scripts/test_safety_source_manifest.json` | `09444936d3c9f9c12070a727b4112079202df63576f7086134f5c3e1e205cd13` | `09444936d3c9f9c12070a727b4112079202df63576f7086134f5c3e1e205cd13` | 21,789 | 525 | ✅ 518 项严格保持未变 (无需修改) |
| **导入基线清单** | `docs/reuse/IMPORT_MANIFEST.json` | `97832326e49dbb15dc39bfb0db46c24a5c9aaed72ec6ed1dd659414e5fc5e211` | `97832326e49dbb15dc39bfb0db46c24a5c9aaed72ec6ed1dd659414e5fc5e211` | 65,301 | 1,135 | ✅ 冻结哈希零变动 |
| **根层包初始化** | `research/__init__.py` | `0d9b4276824f4b26fd76a6b28fbdae40010a45e9fb56edfa1fca471aad023ece` | `0d9b4276824f4b26fd76a6b28fbdae40010a45e9fb56edfa1fca471aad023ece` | 134 | 7 | ✅ 父 init 零触碰 |

---

## 二、 清单与义务映射演进

### 1. 测试安全源文件清单 (`scripts/test_safety_source_manifest.json`)
- **基线项数**: 518 项。
- **演进分析**:
  - `research/market_data/fee_scan.py` 属既有存量生产源码，已在 518 条目中登记。
  - `tests/test_pool_scanner.py` 属测试文件，按架构规约不纳入生产源清单。
  - 本轮无新增源码文件，故现场清单 518 条目保持不变，文件 SHA-256 为 `09444936d3c9f9c12070a727b4112079202df63576f7086134f5c3e1e205cd13`。

### 2. QA 义务映射表 (`tools/qa/upstream_obligations.py`)
- 维持 `KNOWN_ARC_ADAPTATIONS` 结构，精确更新 `tests/test_pool_scanner.py` 适配预期值：
  ```python
  "tests/test_pool_scanner.py": {
      "reason": "RESEARCH-SCANNER-R2 modular fee scanner adaptation with cross-pool metadata isolation regression suite and original assertions preserved",
      "expected_sha256": "b6eda3c3e7cbabd8fa0decc27eaec84ad20c873435ab4bfff386011e776ebd74",
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
- **日志产物**: `/tmp/arc-scanner-integrate/logs/upstream_audit.log`

### 2. 扫描三套件与清单套件联合验证 (Scanner 3 Suites + Manifest Suites)
- **命令**:
  ```bash
  /sandbox/venv/bin/python -m pytest \
    tests/test_pool_scanner.py \
    tests/arc_v3/independent/test_fee_scan_contract.py \
    tests/test_pool_fee_verification.py \
    tests/contracts/test_manifest_coverage.py \
    tests/arc_v3/independent/test_import_manifest.py -v
  ```
- **结果**: **EXIT 0 (81 passed, 1 warning in 1.54s)**
  - `tests/test_pool_scanner.py`: 25 passed
  - `tests/arc_v3/independent/test_fee_scan_contract.py`: 29 passed
  - `tests/test_pool_fee_verification.py`: 11 passed
  - `tests/contracts/test_manifest_coverage.py`: 5 passed
  - `tests/arc_v3/independent/test_import_manifest.py`: 11 passed
- **日志产物**: `/tmp/arc-scanner-integrate/logs/pytest_scanner_manifest.log`

### 3. 全量联合靶向测试 (Targeted Joint All — 15 Suites)
- **命令**:
  ```bash
  /sandbox/venv/bin/python -m pytest \
    tests/arc_v3/independent/test_market_snapshot_immutability.py \
    tests/test_strategies.py \
    tests/test_domain_contracts.py \
    tests/test_market_data_catalog.py \
    tests/test_planning.py \
    tests/test_quoting.py \
    tests/contracts/test_manifest_coverage.py \
    tests/arc_v3/independent/test_import_manifest.py \
    tests/test_market_data_pool_reader.py \
    tests/test_v4_reader.py \
    tests/test_multicall_reader.py \
    tests/test_reporting.py \
    tests/test_pool_scanner.py \
    tests/arc_v3/independent/test_fee_scan_contract.py \
    tests/test_pool_fee_verification.py -v
  ```
- **结果**: **EXIT 0 (270 passed, 1 warning in 2.43s)**
  - 既有 205 项靶向测试 + 新增 65 项扫描测试 = 270 项全绿。
  - **保留 Warning 说明**: `websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated`（第三方依赖废弃告警）。
- **日志产物**: `/tmp/arc-scanner-integrate/logs/pytest_targeted_joint_all.log`

### 4. 全库静态检查 (Ruff & Mypy)
- **Ruff Check**:
  - **命令**: `/sandbox/venv/bin/python -m ruff check .`
  - **结果**: **EXIT 0 (`All checks passed!`)**
  - **日志产物**: `/tmp/arc-scanner-integrate/logs/ruff_full.log`
- **Mypy Check**:
  - **命令**: `/sandbox/venv/bin/python -m mypy .`
  - **结果**: **EXIT 0 (`Success: no issues found in 454 source files`)**
  - **日志产物**: `/tmp/arc-scanner-integrate/logs/mypy_full.log`

---

## 四、 收集错误差集对账 (`pytest --collect-only`)

- **测试收集命令**: `/sandbox/venv/bin/python -m pytest --collect-only`
- **退出码**: **EXIT 2 (标准含有未修复模块收集错误退出码)**
- **收集用例总数**: **2,123 tests collected** (基线 2,098 增至 2,123，净增 25 项测试)
- **收集错误总数**: **33 errors** (基线 34 项精准消除 1 项)

### 差集对比 (Diff Against Reporting Integration Baseline)

```diff
--- /tmp/arc-reporting-integrate/logs/collect_errors.txt
+++ /tmp/arc-scanner-integrate/logs/collect_errors.txt
@@ -9,7 +9,6 @@
 ERROR tests/test_fix_cycle_and_profit_anomaly.py
 ERROR tests/test_funds_coordinator.py
 ERROR tests/test_guard.py
-ERROR tests/test_pool_scanner.py
 ERROR tests/test_public_runtime_binding.py
 ERROR tests/test_readonly_monitor_app.py
 ERROR tests/test_remaining_funds.py
```

- **消除项 (1 项)**: `ERROR tests/test_pool_scanner.py`
- **新增项 (0 项)**: 无任何偶发引入或回退
- **剩余收集错误 (33 项)**: 均为未派单模块历史遗留（如 `arbitrage_daemon.py`, `funds_coordinator.py`, `tax_guard.py` 等），如实呈现，不虚报全库绿。
- **日志产物**:
  - `/tmp/arc-scanner-integrate/logs/pytest_collect_only.log`
  - `/tmp/arc-scanner-integrate/logs/collect_errors.txt`
  - `/tmp/arc-scanner-integrate/logs/collect_errors.diff`
