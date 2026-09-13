# Arc-Chain 市场 M4A 合流集成验收报告：已验 V4 现货读取器与旧测试迁移套件 (Uniswap V4 Reader & Legacy Test Adaptation)

- **工单编号**: `TASK-M4A-V4-READER-INTEGRATION`
- **集成主脑**: M4A 独立审查 / M1 合流集成
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **候选来源**: `/tmp/arc-m4-v4-reader/src` (`research/market_data/v4_reader.py` 与 `tests/test_v4_reader.py`)
- **独立审查依据**: `/tmp/arc-m4-v4-review` (`RESULT.md` / 17 项测试通过 + 5 项探针通过 + signed 反证实验确证)
- **格式收尾依据**: `/tmp/arc-m4-v4-lint` (`RESULT.md` / Ruff I001 换行修正)
- **安全基线**: 严格遵守 `AGENTS.md` v3.1；零真实资金、零网络请求、零合约部署、未运行业务 import / bwrap、未变动冻结清单与 CLI 入口、排除多余父 init。

---

## 一、 精确装配与哈希核验一致表

本集成操作严格提取并装配已通过独立审查与 Lint 格式收尾的产物，SHA-256 哈希与前序产物保持 100% 精确一致：

| 序号 | 模块角色 | 目标路径 | 大小 (Bytes) | SHA-256 哈希 | 校验结果 |
|---|---|---|---|---|---|
| 1 | **Uniswap V4 读取器核心** | `research/market_data/v4_reader.py` | 19,485 | `a461ec6392fb3b0907bf6b2f8490edd56bb4fe5c58d6f1f8356508f10f1f4bfb` | ✅ 严格一致 (100% MATCH) |
| 2 | **V4 读取器单元测试** | `tests/test_v4_reader.py` | 12,198 | `592b42e80453f1f4857870189af0e14c1bfb9a3665ad54f2cd50ea885d43fd7e` | ✅ 严格一致 (100% MATCH) |
| 3 | **安全源清单** | `scripts/test_safety_source_manifest.json` | - | - | ✅ 仅新增 1 项 (502 -> 503) |
| 4 | **上游测试义务** | `tools/qa/upstream_obligations.py` | - | - | ✅ 仅旧 test 适配新哈希 |
| 5 | **集成验收报告** | `docs/acceptance/RESEARCH_V4_READER.md` | - | - | ✅ 本报告归档 |

---

## 二、 AST 语义等价性与原测试义务核验证据

### 2.1 格式收尾最后空行等价性核验
- **Lint 修正差异**: 前序 `b1ce8638ec9ccbe4d413a7dbace4b8c3f56f8083c4f33e702719dfcb605cb98b` 与 Lint 修复后 `592b42e80453f1f4857870189af0e14c1bfb9a3665ad54f2cd50ea885d43fd7e` 之间仅在第三方 `import pytest` 与第一方 `from research.market_data.v4_reader import (...)` 之间插入单个换行符以满足 Ruff I001 规范。
- **AST 语法树等价性**: 经 Python `ast.parse` 解析比对，两版本抽象语法树 dump 序列完全等价 (`ast.dump(t1) == ast.dump(t2)` 为 `True`)，证明最后空行修改对运行时没有任何行为变动或逻辑漂移。

### 2.2 原测试断言与异常拦截 100% 完整继承
经 AST 遍历与断言统计：
- **测试类**: 共 6 个测试类 (`TestV4PoolSpec`, `TestStorageSlotCalculation`, `TestDecodeSlot0Data`, `TestDecodeSwapLogData`, `TestV4PriceCalculation`, `TestV4ReaderFlow`)。
- **测试方法**: 共 17 个独立测试方法，与旧测试一一对应。
- **断言数量**: 原 34 项 `assert` 表达式全部严格保留，容差界限与比较目标零削弱。
- **异常捕获**: 原 6 项 `pytest.raises` 上下文及其匹配正则全部保留。
- **代码重构面**: 仅将已废弃的旧路径 `arbitrage.v4_reader` 与 `core.wallet_guard.ZeroSlippageError` 替换为解耦后的 `research.market_data.v4_reader`。

---

## 三、 架构隔离与安全红线核验

1. **排除多余父 init**:
   - 工作树中 `research/__init__.py` 与 `research/market_data/__init__.py` 保持现状，未额外创建任何父级或子级 `__init__.py`。
2. **零侵入原则**:
   - M1 / M2 / CLI 入口及旧 fee 扫描逻辑保持完全未动。
3. **冻结清单与注册表零修改**:
   - `docs/reuse/IMPORT_MANIFEST.json` (281 项) 保持严格冻结。
   - `market_catalog/registry.py` (registry2) 保持未动。
4. **安全红线遵守**:
   - 零真实资金、零网络请求、零合约部署、未运行 pytest / audit 业务 import / bwrap、零 commit / push。

---

## 四、 安全清单与适配登记

1. **安全源清单更新 (`scripts/test_safety_source_manifest.json`)**:
   - `files` 列表自 502 项精确扩展至 503 项。
   - 仅新增 `research/market_data/v4_reader.py`，保持严格字母序排序与无重复。
   - `public_fixtures` 保持不变。

2. **上游测试义务适配 (`tools/qa/upstream_obligations.py`)**:
   - 仅更新 `KNOWN_ARC_ADAPTATIONS` 中 `tests/test_v4_reader.py` 的 `expected_sha256` 为 `592b42e80453f1f4857870189af0e14c1bfb9a3665ad54f2cd50ea885d43fd7e`。
   - 运行独立 QA 审计：`Overall Status: PASSED [OK]` (Exit Code: 0)。

---

## 五、 静态质量门禁核查结果

1. **静态语法与 AST 解析**:
   - `research/market_data/v4_reader.py` 与 `tests/test_v4_reader.py` 均通过 `ast.parse` 校验，语法零错误。
2. **代码规范检查 (`ruff check`)**:
   - 基于工作树 `pyproject.toml` 显式配置执行：
     ```bash
     ruff check --config pyproject.toml research/market_data/v4_reader.py tests/test_v4_reader.py
     ```
   - 结果：**All checks passed!** (Exit Code: 0)。
3. **类型系统检查 (`mypy`)**:
   - 基于工作树 `pyproject.toml` 显式配置执行：
     ```bash
     mypy --config-file pyproject.toml research/market_data/v4_reader.py tests/test_v4_reader.py
     ```
   - 结果：**Success: no issues found in 2 source files** (Exit Code: 0)。

---

## 六、 结论与后续验证说明

本集成工单已圆满完成 M4A 的已验 V4 读取器及适配测试合流装配。依据项目指令约定，不提前上报收集 40 项用例，留待后续阶段统一进行后验。
