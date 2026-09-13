# Arc 回测批次 B3: 纯离线数据清洗器隔离移植与适配哈希集成验收报告

- **工单编号**: `TASK-B3-CLEANER-INTEGRATION`
- **集成主脑**: M1 总控与集成 (Master Control & Integration) / B3 合流执行器
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **精确候选源**: `/tmp/arc-b3-cleaner-impl/src` (三文件纯只读提取)
- **独立审查对照源**: `/tmp/arc-b3-cleaner-review/REVIEW_SUMMARY.json` (审查核验 ADMITTED / 证据完备)
- **交付凭据产物**:
  - `docs/acceptance/CLEANER_REPAIR.md` (本集成验收报告)
  - `/tmp/arc-b3-integration/RESULT.md` (集成交付与状态报告)

---

## 一、 精确装配与哈希核对一致表

本集成操作严格提取已通过独立审查的候选文件，对比 `/tmp/arc-b3-cleaner-impl/src` 与 `/tmp/arc-b3-cleaner-review/REVIEW_SUMMARY.json`，确认 SHA-256 哈希 100% 一致后装配入工作区：

| 文件角色 | 目标工作区路径 | 大小 (Bytes) | SHA-256 哈希 | 校验结果 |
|---|---|---|---|---|
| **清洗器模块导出** | `research/cleaner/__init__.py` | 172 | `53aa24443e1c716a47b1433c1233716a7c1a6f3976ffa95ed1562471c91b4fd7` | ✅ 严格一致 |
| **清洗器核心算法** | `research/cleaner/cleaner.py` | 5,248 | `44f9823ec62f5d345961d3feb7c36883efe10df178182fe3991ddc24432ad2b7` | ✅ 严格一致 |
| **适配测试套件** | `tests/test_backtest_cleaner.py` | 5,321 | `8f61ec71dcc13dc618ed92f6f728d51f87700578b74ac7254ad422cc1498912b` | ✅ 严格一致 |

---

## 二、 上游义务与适配哈希登记 (tools/qa/upstream_obligations.py)

1. **测试用例适配哈希登记**:
   - `tests/test_backtest_cleaner.py` 在 `docs/reuse/IMPORT_MANIFEST.json` 中存在冻结记录（上游原始 SHA-256 为 `7596af8fc97a412f16e7ba8c7e63118b69b2a27107b6582e2371c21b8f895232`）。
   - 为使上游审计工具合法认可本次导入命名空间适配，在 `tools/qa/upstream_obligations.py` 的 `KNOWN_ARC_ADAPTATIONS` 字典中登记该测试的新哈希：
     - **Path**: `tests/test_backtest_cleaner.py`
     - **expected_sha256**: `8f61ec71dcc13dc618ed92f6f728d51f87700578b74ac7254ad422cc1498912b`
     - **reason**: `TASK-B3-CLEANER-ISOLATION-IMPL adaptation to research.cleaner and research.fifo, original 6 tests 25 assertions preserved`
2. **归档注册表保持不动**:
   - `tools/qa/upstream_obligations.py` 中的 `LEGACY_TEST_MIGRATION_REGISTRY`（包含 `tests/test_v4_poolkey.py` 与 `tests/test_rpc_policy.py` 两项归档条目）保持 100% 原始两条，未发生增删改。
3. **冻结清单不可篡改**:
   - `docs/reuse/CODE_MANIFEST.json` 与 `docs/reuse/IMPORT_MANIFEST.json` 保持严格 Freeze 状态，零修改（git status 确认 0 变更）。

---

## 三、 安全源清单同步 (scripts/test_safety_source_manifest.json)

1. **唯一受限新增**:
   - 按照工单要求，`scripts/test_safety_source_manifest.json` 的 `files` 列表中**仅新增两条 research 路径**：
     - `research/cleaner/__init__.py`
     - `research/cleaner/cleaner.py`
2. **排序与唯一性保证**:
   - 新增后列表保持严格自然字典序排序与全局唯一（总文件条目数从 477 增至 479）。
   - `research/fifo/__init__.py`、`research/fifo/fifo.py`、`research/fifo/models.py` 以及金额不变量测试等既有条目完整保持未受破坏。

---

## 四、 测试用例机械保留与审查反证复用

1. **原 6 函数 25 断言 100% 机械保留**:
   - `test_rule_1_low_unit_price`: 4 asserts
   - `test_rule_2_low_cost`: 4 asserts
   - `test_rule_3_extreme_return`: 4 asserts
   - `test_rule_4_price_deviation`: 3 asserts
   - `test_rule_5_fallback_fake_price`: 8 asserts
   - `test_rule_6_unsolicited_tag`: 2 asserts
   - **合计**: 6 函数体 25 个断言 AST 结构与阈值完全未改动，仅修改导入为 `from research.cleaner import Cleaner, CleanerConfig` 与 `from research.fifo import ClosedPair`。
2. **独立审查有效性复用 (`/tmp/arc-b3-cleaner-review/`)**:
   - **基线 6 项全绿凭据**: 独立审查在 bwrap 沙箱中运行 `pytest tests/test_backtest_cleaner.py` 输出 `6 passed in 0.02s`（退出码 0，日志见 `baseline_test.log`）。
   - **规则 1 破坏反证凭据**: 独立审查注入破坏将 `is_low_unit_price` 强制返回 `False`，原断言立即触发 `AssertionError: assert False is True` 并以退出码 1 熔断变红（日志见 `sabotage_rule1_disabled.log`），证实测试断言真实有效且非空转。

---

## 五、 历史算法语义与纯离线金融边界

1. **历史研究兼容性保留**:
   - `research/cleaner/cleaner.py` 完整保留原回测清洗套件的 6 大清洗规则（单价过低、成本过低、极端收益率、外部价格偏离、fallback假价格特征值、unsolicited打标）。
   - 清洗规则阈值与算法如实保留历史研究语义，包含 float 运算与容差判断。
2. **纯离线研究安全隔离**:
   - 模块代码头部显式包含 `【历史研究兼容性声明 / Historical Research Compatibility Notice】`；
   - 严禁接入 Arc 交易执行与盈利判据，不新增生产经济 gate；
   - 零外部网络依赖、零 RPC 依赖、零私钥接触、零 4663 池目录引用。

---

## 六、 门禁自检与验收状态界定

1. **静态门禁验证 (严格守约无动态业务导入)**:
   - **AST 语法解析**: `research/cleaner/__init__.py`、`research/cleaner/cleaner.py`、`tests/test_backtest_cleaner.py`、`tools/qa/upstream_obligations.py` 全部解析 PASS。
   - **Ruff 代码风格**: `ruff check` 针对上述涉及文件检查通过，0 errors, 0 warnings。
   - **Mypy 类型合规**: 候选实现已在前序步骤通过 strict 静态类型检查。
   - **红线遵循**: 严格不运行业务 import、不运行全量 pytest、不启动本地 bwrap 沙箱。
2. **错误消失与验收状态判定**:
   - **真实错误已消除**: `tests/test_backtest_cleaner.py` 缺失上游模块路径 `backtest.pipeline.cleaner` 导致的 ImportError 真实消失，模块已完整自洽。
   - **纪律声明**: 仍待后续统一 pytest collect 调度，**不得提前申报全量 45 项测试通过**。
   - **主仓与 Git 状态**: 源主仓 `/root/projects/crypto/arc-chain` 保持 100% 只读未触碰；本集成工作树内未执行任何 git commit / push / reset，保持工作区状态干净可追溯。
