# Arc 回测批次 B1: FIFO 撮合器模块移植与适配哈希集成验收报告

- **工单编号**: `TASK-B1-FIFO-INTEGRATION`
- **集成主脑**: M1 总控与集成 (Master Control & Integration) / B1 合流执行器
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **精确候选源**: `/tmp/arc-b1-fifo-impl/src` (四文件纯只读提取)
- **独立审查对照源**: `/tmp/arc-b1-fifo-review/integrated` (受审哈希比对核一致)
- **独立验收评定**: `/tmp/arc-b1-fifo-review/REVIEW_REPORT.md` (准入通过 / 证据完备)
- **交付凭据产物**:
  - `docs/acceptance/FIFO_REPAIR.md` (本集成验收报告)
  - `/tmp/arc-b1-fifo-integration/RESULT.md` (集成执行与状态报告)

---

## 一、 精确装配与哈希核对一致表

本集成操作严格提取已通过审查的四文件，并对比 `/tmp/arc-b1-fifo-impl/src` 与 `/tmp/arc-b1-fifo-review/integrated`，确认 SHA-256 哈希 100% 一致后装配入工作区：

| 文件角色 | 目标工作区路径 | 大小 (Bytes) | SHA-256 哈希 | 校验结果 |
|---|---|---|---|---|
| **FIFO 导出入口** | `research/fifo/__init__.py` | 276 | `a55ac2ddd401169e208c33563c384aa131f9f4b46298a0220b9c095d9337771f` | ✅ 严格一致 |
| **FIFO 数据模型** | `research/fifo/models.py` | 2,893 | `5b767e97ed1518d41a2f0a05dd95c962d7ba5eb10e0eb3f6a20e059ff5cdaf84` | ✅ 严格一致 |
| **FIFO 撮合核心** | `research/fifo/fifo.py` | 7,025 | `0ed730827bc1bf028432d0174ed613db89755873925bc9df2ad5af73264ad89c` | ✅ 严格一致 |
| **适配测试套件** | `tests/test_backtest_matcher.py` | 7,138 | `c280cb8016ea0b0bd9118d37ea9f6e4b575d8f8575fe05613a97b60ded97b3eb` | ✅ 严格一致 |

---

## 二、 上游义务与适配哈希登记 (tools/qa/upstream_obligations.py)

1. **测试用例适配哈希登记**:
   - `tests/test_backtest_matcher.py` 在 `docs/reuse/IMPORT_MANIFEST.json` 中存在冻结记录（上游原始 SHA-256 为 `80b68999f5eeccd8cb5dcc68d96fd8c2367940f82ab38357e3db3dd970b26311`）。
   - 为使上游审计工具合法认可本次导入命名空间适配，在 `tools/qa/upstream_obligations.py` 的 `KNOWN_ARC_ADAPTATIONS` 字典中登记该测试的新哈希：
     - **Path**: `tests/test_backtest_matcher.py`
     - **expected_sha256**: `c280cb8016ea0b0bd9118d37ea9f6e4b575d8f8575fe05613a97b60ded97b3eb`
     - **reason**: `TASK-B1-FIFO-ISOLATION-IMPL adaptation to research.fifo, original 5 tests 39 assertions preserved, appended 2 isolation and negative control tests (23 assertions)`
2. **Q2 已退出，归档注册表保持不动**:
   - `tools/qa/upstream_obligations.py` 中的 `LEGACY_TEST_MIGRATION_REGISTRY`（包含 `tests/test_v4_poolkey.py` 与 `tests/test_rpc_policy.py` 两项归档条目）保持 100% 不动。
3. **冻结清单不可篡改**:
   - `docs/reuse/CODE_MANIFEST.json` 与 `docs/reuse/IMPORT_MANIFEST.json` 保持严格 Freeze 状态，零修改。

---

## 三、 避开并发写清单：Source Manifest 待主脑集中登记

1. **当前状态与并发避让**:
   - 当前主脑/其他工单（如 G3）可能正在对 `scripts/test_safety_source_manifest.json` 进行并发写入。
   - 遵循集成红线，本工单**严禁修改** `scripts/test_safety_source_manifest.json`，工作区既有 dirty 变动保持原状。
2. **待主脑集中登记之 3 条新增路径**:
   - `research/fifo/__init__.py`
   - `research/fifo/models.py`
   - `research/fifo/fifo.py`
3. **验收状态界定**:
   - 由于上述 3 条源码文件尚未登记入 `scripts/test_safety_source_manifest.json`，故**本单不能报全验收完 (NOT FULL ACCEPTANCE COMPLETE)**，留待主脑 M1 集中登记 source manifest 并在全量门禁中统一闭环。

---

## 四、 测试用例机械保留与隔离反证有效性

1. **原 5 函数 39 断言 100% 机械保留**:
   - `test_fifo_simple_one_to_one`: 9 asserts
   - `test_fifo_partial_sell_with_open_position`: 9 asserts
   - `test_fifo_merge_multiple_partial_sells`: 10 asserts
   - `test_fifo_one_sell_matches_multiple_buys`: 6 asserts
   - `test_fifo_trader_and_token_isolation`: 5 asserts
   - AST 结构完全一致，无阈值放宽，无 skip，仅修改导入路径为 `from research.fifo import FIFOMatcher, SwapRecord`。
2. **追加 2 个新测试函数 (23 断言)**:
   - `test_fifo_same_trader_multi_token_isolation`: 16 asserts，验证同一交易员跨代币物理隔离。
   - `test_fifo_negative_control_orphan_sell_and_exceeding_sell`: 7 asserts，验证孤立卖单与超额卖出容错边界。
3. **可复用复验凭据 (`/tmp/arc-b1-fifo-review/REVIEW_REPORT.md`)**:
   - **7 Pass 凭据**: 沙箱隔离测试 7 项测试全部通过 (`7 passed, 1 warning in 0.06s`)。
   - **破坏隔离反证凭据**: 在控制副本破坏 `(trader, token)` 隔离机制后，原测试 `test_fifo_trader_and_token_isolation` 的原生断言精确转红失败（`AssertionError: assert '' == 'alice'`），证实原断言非空转。

---

## 五、 浮点语义与金融安全隔离声明

1. **历史研究语义兼容**:
   - `research/fifo/models.py` 与 `research/fifo/fifo.py` 内部使用原生 `float` 及 `1e-12` 切片容差，保留原上游成熟历史回测与复盘语义。
2. **绝对禁止接入金融执行路径**:
   - 浮点实现专供纯离线研究、持仓切片分析与因果复盘使用。
   - 严禁接入 Arc 链上金融执行（execution）整数路径（uint256/atom）。
   - 模块仅依赖 Python 标准库，零网络、零 RPC、零私钥、零 4663 池目录引用。

---

## 六、 门禁静态验证结果 (仅 AST / Ruff / Mypy)

按照指令红线，严禁在业务层执行 pytest 导入、audit 或 bwrap 动态测试，仅执行静态语法与类型合规自检：

1. **AST 语法解析**:
   - `research/fifo/__init__.py`: PASS
   - `research/fifo/models.py`: PASS
   - `research/fifo/fifo.py`: PASS
   - `tests/test_backtest_matcher.py`: PASS
   - `tools/qa/upstream_obligations.py`: PASS
2. **Ruff 代码风格与规范检查 (`python -m ruff check`)**:
   - 5 项涉及文件全部通过，0 errors, 0 warnings。
3. **Mypy 严格类型检查 (`python -m mypy`)**:
   - `research/fifo/`: Success: no issues found in 3 source files
   - `tests/test_backtest_matcher.py`: Success: no issues found in 1 source file
   - `tools/qa/upstream_obligations.py`: Success: no issues found in 1 source file

---

## 七、 变更范围与环境隔离确认

- **工作区既有 Dirty 保全**: 原工作区中的 186 项变更保持原样，无丢弃、无覆盖。
- **CLI 变更隔离**: CLI 入口无任何变更。
- **Git 状态**: 无 `git commit`，无 `git push`。
- **主仓零污染**: 主仓 `/root/projects/crypto/arc-chain` 保持只读未触碰。
