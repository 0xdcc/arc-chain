# Arc-Chain Fork 费率合流集成验收报告 (RESEARCH_FORK_FEE_REPAIR.md)

- **工单编号**: `TASK-FORK-FEE-INTEGRATION` (Fork 费率适配器合流与收尾验收)
- **集成角色**: M1 总控与集成 (Master Control & Integration) / 配合 M4 独立审查
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **候选来源**: `/tmp/arc-fork-fee-r4/snapshot` (经 R4 独立审查准入的 5 项授权文件) 及 `/tmp/arc-fork-fee-r4-review`
- **前置凭证**: `/tmp/arc-fork-fee-r4-review/AUDIT_REPORT.md` (R4 审查报告通过，87 项回归测试与 Ruff 检查全绿)
- **集成结论**: **🟢 FORK_FEE_INTEGRATION_COMPLETED — 5 项受权核心文件 100% 精确装配；安全源清单增至 527 条目且 16 项清单/导入测试全绿；QA 义务表 281 项审计全数 PASSED；全库 463 模块 Mypy 零报错通过；全仓 pytest 收集测试从 2191 增至 2213 项，历史收集错误从 31 精准消除 1 项至 30 项 (0 新增错误，真实差集闭环)；严守工作区合法 dirty，零 commit/push。**

---

## 一、 精确装配文件与 SHA-256 哈希现场核验

严格遵循“仅合流 R4 授权的 2 生产 + 3 测试文件、清单仅增 1 独立测试条目、义务表仅更新 2 对应测试哈希、冻结上游 IMPORT_SHA”原则：

| 模块角色 | 目标文件路径 | 文件大小 (Bytes) | SHA-256 现场校验哈希 | 候选源对照 (`/tmp/arc-fork-fee-r4/snapshot`) | 对账状态 |
| :--- | :--- | :---: | :--- | :--- | :---: |
| **费率扫描生产核心** | `research/market_data/fee_scan.py` | 37,834 | `aaa5a4575f61997ff03db18ac48c69b4ecf397179a73f2d1680f7d915cf0b040` | `aaa5a4575f61997ff03db18ac48c69b4ecf397179a73f2d1680f7d915cf0b040` | ✅ 100% MATCH |
| **费率验证生产核心** | `research/market_data/fee_verification.py` | 15,173 | `9cb0cec60a78c2969455501672f3a21face075d3a298af67c175e4c72d976382` | `9cb0cec60a78c2969455501672f3a21face075d3a298af67c175e4c72d976382` | ✅ 100% MATCH |
| **候选集成测试用例** | `tests/test_candidate_fee_integration.py` | 4,328 | `5dd9a25b07a7a1d332904bf582837fb833e2b78203b2996eebac49d3cef71858` | `5dd9a25b07a7a1d332904bf582837fb833e2b78203b2996eebac49d3cef71858` | ✅ 100% MATCH |
| **独立批量费率契约** | `tests/arc_v3/independent/test_batch_fee_contract.py` | 9,566 | `b855cc807648abcbf29df2ccb210523ff345871c6aa462a4e1e4bcf9ae4ac4b2` | `b855cc807648abcbf29df2ccb210523ff345871c6aa462a4e1e4bcf9ae4ac4b2` | ✅ 100% MATCH |
| **池费率验证回归测试** | `tests/test_pool_fee_verification.py` | 11,885 | `4c4ba4a6417c1a55f770a2646d85884a9fefc4a1a151d2f3b3a21c28d04c7345` | `4c4ba4a6417c1a55f770a2646d85884a9fefc4a1a151d2f3b3a21c28d04c7345` | ✅ 100% MATCH |

---

## 二、 安全源清单与上游义务表审计对账

### 2.1 安全源清单 (`scripts/test_safety_source_manifest.json`)
- **变更详情**: 严格按字典序登记新增独立测试条目 `"tests/arc_v3/independent/test_batch_fee_contract.py"`。
- **清单总数**: 由 526 项合规增至 **527 项**，保持有序且无重复项。
- **覆盖与导入验证**:
  - `tests/contracts/test_manifest_coverage.py`: 5/5 PASSED (exit 0)
  - `tests/arc_v3/independent/test_import_manifest.py`: 11/11 PASSED (exit 0)
  - **总计 16 项清单/导入契约测试全数通过**。

### 2.2 QA 上游义务表 (`tools/qa/upstream_obligations.py`)
- **对账适配条目**:
  1. `tests/test_candidate_fee_integration.py`: expected_sha256 更新为 `5dd9a25b07a7a1d332904bf582837fb833e2b78203b2996eebac49d3cef71858`
  2. `tests/test_pool_fee_verification.py`: expected_sha256 更新为 `4c4ba4a6417c1a55f770a2646d85884a9fefc4a1a151d2f3b3a21c28d04c7345`
- **上游导入冻结**: **`IMPORT_SHA` 与全部 281 项上游文件的导入哈希保持完全冻结不变**。
- **Audit 281 运行结果**:
  - 执行命令: `bwrap ... /sandbox/venv/bin/python tools/qa/upstream_obligations.py --audit`
  - 返回状态: `Overall Status: PASSED [OK]` (exit code 0)。

---

## 三、 生产与测试关键语义界定

1. **生产未知 Fork 不推断 (No Inferred/Guessed Fees in Production)**:
   - 在生产模块 `research/market_data/fee_scan.py` 及 `research/market_data/fee_verification.py` 中，严禁对未经确认的未知或第三方 Fork 协议盲目推断或猜测其费率单位。
   - 生产环境严格遵循 `test_unknown_fork_stays_visible_without_a_guessed_fee` 约定，当遇到未显式支持的 Fork 时，保持只读池可见性但不填充虚假费率。
2. **测试显式假设沙箱化 (Explicit Synthetic Adapter in Test Scenarios)**:
   - 在 `tests/test_pool_fee_verification.py` 中的 `test_giga_and_up_v3_real_scenario_verification` 测试用例中，针对 `giga-v3` 的 PPM 费率单位（除以 100.0 转换）以合成适配器函数 `simulated_ppm_reader` 形式显式定义，并通过参数 `scan_pools(..., v3_reader=simulated_ppm_reader)` 注入。
   - 该假设严格内聚于测试用例内部，未外溢至生产环境或生产适配器字典。

---

## 四、 隔离沙箱门禁复核与差集证据

所有测试均在标准 bwrap 沙箱（只读工作树、只读 venv、私有 tmpfs、禁用网络 `--unshare-net`、清空环境变量 `--clearenv`）下执行并即时落地日志于 `/tmp/arc-fork-fee-integrate/logs/`：

### 4.1 提取之前 Trace (`deleg_38fadb3f`) 验证结果
- **87 项回归测试**: 87 passed, 1 warning in 1.21s (exit code 0)
- **Ruff 语法风格检查**: `All checks passed!` (exit code 0)
- *注：前序 trace 日志末段超长字符以 `[TRUNCATED_IN_TRACE_LOG]` 诚实标明，绝不伪造。*

### 4.2 本轮补齐验证证据
1. **全库 Mypy 静态检查**:
   - 命令: `/sandbox/venv/bin/python -m mypy --cache-dir /tmp/mypy .`
   - 结果: `Success: no issues found in 463 source files` (exit code 0)。
2. **Audit 281 独立审计**:
   - 命令: `/sandbox/venv/bin/python tools/qa/upstream_obligations.py --audit`
   - 结果: `Overall Status: PASSED [OK]` (exit code 0)。
3. **安全清单与导入测试**:
   - 命令: `/sandbox/venv/bin/python -m pytest tests/contracts/test_manifest_coverage.py tests/arc_v3/independent/test_import_manifest.py -q`
   - 结果: `16 passed in 0.44s` (exit code 0)。
4. **Pytest Collect-only 真实差集对账**:
   - 命令: `/sandbox/venv/bin/python -m pytest --collect-only -q`
   - 前态 (`collect_before.log`): 2191 tests collected, 31 errors (exit 2)
   - 后态 (`collect_after.log`): 2213 tests collected, 30 errors (exit 2)
   - **差集分析**:
     - 成功收集的测试数增加 **+22** (2191 -> 2213)
     - 消除错误 **-1** (`tests/test_candidate_fee_integration.py` 修复成功，不再报错)
     - 引入新错误 **0**
     - 退出码 2 为含有其余历史收集错误时的标准 pytest 退出码，诚实反映真实差集改善。

---

## 五、 缺项声明与工作区状态

1. **缺项与未修改范围说明**:
   - 剩余 30 项测试收集错误（例如 `test_arbitrage_daemon.py`, `test_spread_monitor.py`, `test_tick_cache.py`, `test_tax_guard.py` 等）为外部模块既有遗留问题，分属其他工单职责范围，本工单严守边界，坚决不作越权扩散修改。
2. **工作区整洁与合法保护**:
   - 工作区 `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912` 中已有兄弟模块合法 dirty（如 `read_round.py`, `research/graph/`, `research/strategies/` 及对应的验收文档）全数完整保留。
   - 严禁且未执行 `git reset`、`git clean`、`git checkout` 等破坏性操作。
   - 严禁且未修改 venv 软链，严禁且未执行 `git commit` 或 `git push`。
