# Arc 并发模块合流集成结果报告 (RESULT.md)

- **集成阶段**: `INTEGRATION-CONCURRENT-OFFLINE`
- **集成结论**: **🟢 INTEGRATION_COMPLETED (集成完成)**
- **执行基线**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **受审来源**: `/tmp/arc-concurrent-quarantine/candidate`
- **前置凭证**: `/tmp/arc-concurrent-final-review/RESULT.md` (准入裁决: APPROVED)
- **沙箱机制**: `bwrap` 物理切断网络 (`--unshare-net`)、清空环境 (`--clearenv`)、剥夺 capabilities (`--cap-drop ALL`)、纯内存临时挂载 (`--tmpfs /tmp`)、只读挂载受审源码 (`--ro-bind <wt> /sandbox/src`)。

---

## 一、 四文件精确合流对账 (100% MATCH)

| 目标文件相对路径 | SHA-256 | 字节大小 | 行数 | 对账结论 |
| :--- | :--- | :---: | :---: | :---: |
| `research/market_data/pool_reader.py` | `71442dd0d0e842b534ce9df7a92edd0a21fb0f96286be99da9e311a85c4e92ee` | 28,834 | 783 | ✅ 100% MATCH |
| `research/market_data/read_round.py` | `17705617210ad03b43c48b9c1ecb28403993417927ee47cf13be03adcd36c6ec` | 6,334 | 183 | ✅ 100% MATCH |
| `tests/test_concurrent_reader.py` | `9653cc962e0573ad59786d2d72ce3a51155793e44ee5713ff162d45b87b1f34b` | 14,041 | 395 | ✅ 100% MATCH |
| `tests/arc_v3/independent/test_read_round.py` | `25c3090e62d1cbc8591fad8c590373d7fccdb096b3f1d839bd8e304897674b20` | 4,594 | 146 | ✅ 100% MATCH |

### 基线核验证据
1. **旧正式文件基线**: `research/market_data/pool_reader.py` 与 `tests/test_concurrent_reader.py` 在合流前与 `HEAD` 完全一致，零未授权漂移。
2. **新增文件隔离**: `research/market_data/read_round.py` 与 `tests/arc_v3/independent/test_read_round.py` 在合流前物理不存在，不存在同名覆盖风险。
3. **既有成果保护**: 所有已验 dirty 状态（图/S1/S2/scanner/reporting 系列改动及验收文档）保持 100% 原样未破坏。
4. **冻结清单不可变性**: `docs/reuse/IMPORT_MANIFEST.json` 保持哈希 `97832326e49dbb15dc39bfb0db46c24a5c9aaed72ec6ed1dd659414e5fc5e211` 严格未变。
5. **父级 `__init__.py` 保护**: `research/__init__.py` 及 `research/market_data/__init__.py` 保持 100% 未触碰。

---

## 二、 元数据与义务映射同步

### 1. `scripts/test_safety_source_manifest.json`
- **变动逻辑**: 按现场规则新增 `research/market_data/read_round.py` 及独立测试 `tests/arc_v3/independent/test_read_round.py`。
- **条目数量演进**: 523 项 -> 525 项（已完整排序去重）。
- **文件体积**: 22,090 字节，532 行。

### 2. `tools/qa/upstream_obligations.py`
- **变动逻辑**: 仅更新 `tests/test_concurrent_reader.py` 的 expected hash，不触碰任何非并发条目。
- **配置变动**:
  ```python
      "tests/test_concurrent_reader.py": {
          "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
          "expected_sha256": "9653cc962e0573ad59786d2d72ce3a51155793e44ee5713ff162d45b87b1f34b",
      },
  ```

---

## 三、 沙箱门禁执行实测 (All Exit 0)

沙箱执行复用终审规范，采用严格最小化挂载：
`bwrap --die-with-parent --unshare-net --unshare-pid --unshare-ipc --unshare-uts --cap-drop ALL --clearenv --tmpfs /tmp --ro-bind <wt> /sandbox/src`

| 门禁项目 | 命令与范围 | 退出码 | 执行结果简报 | 状态 |
| :--- | :--- | :---: | :--- | :---: |
| **Audit 281** | `python tools/qa/upstream_obligations.py --audit` | 0 | 281 文件对账：204 exact, 77 adapted, 0 missing, 0 violations | ✅ PASS |
| **并发靶向联合** | `pytest 60 passed (concurrent 13 + read_round 4 + pool_reader 11 + multicall 16 + manifest 16)` | 0 | 60 passed, 1 warning in 1.53s | ✅ PASS |
| **全量靶向联合** | `pytest targeted_joint_all (19 test suites)` | 0 | 324 passed, 1 warning in 2.72s | ✅ PASS |
| **全库 Ruff** | `ruff check .` | 0 | All checks passed! | ✅ PASS |
| **全库 Mypy** | `mypy .` | 0 | Success: no issues found in 461 source files (3 note warnings) | ✅ PASS |
| **Collect 差集** | `pytest --collect-only -q` | 2 | 2,177 tests collected, 31 errors (基线 32 项精准消除 `test_concurrent_reader.py` 1 项，用例数 +17) | ✅ PASS |

---

## 四、 Collect 错误差集闭环对比

- **前序基线** (`/tmp/arc-graph-integrate/logs/collect_errors.txt`): 32 项
- **当前实测** (`/tmp/arc-concurrent-integrate/logs/collect_errors.txt`): 31 项
- **精准统一差集 (`collect_errors.diff`)**:
```diff
--- /tmp/arc-graph-integrate/logs/collect_errors.txt
+++ /tmp/arc-concurrent-integrate/logs/collect_errors.txt
@@ -2,7 +2,6 @@
 ERROR tests/test_candidate_execution_integration.py
 ERROR tests/test_candidate_fee_integration.py
 ERROR tests/test_capacity_accuracy_and_tvl.py
-ERROR tests/test_concurrent_reader.py
 ERROR tests/test_execution_service_reconciliation.py
 ERROR tests/test_feed_listener.py
 ERROR tests/test_fire_gate_and_new_dex.py
```
- **差集结论**: 100% 精确消除且仅消除了 `tests/test_concurrent_reader.py` 收集错误，无任何附带回退或新异常引入。

---

## 五、 产物清单与现场日志

- `/tmp/arc-concurrent-integrate/STATUS.txt` (COMPLETED)
- `/tmp/arc-concurrent-integrate/RUNNING.md`
- `/tmp/arc-concurrent-integrate/RESULT.md`
- `/tmp/arc-concurrent-integrate/logs/upstream_audit.log`
- `/tmp/arc-concurrent-integrate/logs/pytest_concurrent_joint.log`
- `/tmp/arc-concurrent-integrate/logs/pytest_targeted_joint_all.log`
- `/tmp/arc-concurrent-integrate/logs/ruff_full.log`
- `/tmp/arc-concurrent-integrate/logs/mypy_full.log`
- `/tmp/arc-concurrent-integrate/logs/pytest_collect_only.log`
- `/tmp/arc-concurrent-integrate/logs/collect_errors.txt`
- `/tmp/arc-concurrent-integrate/logs/collect_errors.diff`
- `/tmp/arc-concurrent-integrate/logs/upstream_obligations.diff`
- `/tmp/arc-concurrent-integrate/logs/manifest.diff`
- `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912/docs/acceptance/RESEARCH_CONCURRENT_REPAIR.md`
