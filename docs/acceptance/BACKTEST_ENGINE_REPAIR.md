# Arc 回测批次 B4: 纯离线回测引擎隔离移植与适配哈希集成验收报告

- **工单编号**: `TASK-B4-ENGINE-INTEGRATION`
- **集成主脑**: M1 总控与集成 (Master Control & Integration) / B4 合流执行器
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **精确候选源**: `/tmp/arc-b4-r2/src` (12 文件只读装配)
- **独立审查对照源**: `/tmp/arc-b4-r2-review/REVIEW_SUMMARY.json` (审查核验 ADMITTED / 14 pass + 3 cache fail 证据完备)
- **终审 Lint 修正依据**: `/tmp/arc-b4-lint-final/RESULT.md` (独立静态核验 delta 仅 import 空行与 raw string 转义修正，AST 运行时机械等价)
- **交付凭据产物**:
  - `docs/acceptance/BACKTEST_ENGINE_REPAIR.md` (本集成验收报告)
  - `/tmp/arc-b4-integration/RESULT.md` (集成交付与状态报告)
  - `/tmp/arc-b4-integration/b4_exact.diff` (精确差分补丁)

---

## 一、 精确装配与哈希核对一致表

本集成操作严格提取候选 12 文件，核验 11 个核心模块哈希与 `/tmp/arc-b4-r2-review/REVIEW_SUMMARY.json` 100% 一致，且新 boundary 测试文件哈希与 `/tmp/arc-b4-lint-final/RESULT.md` 核验值严格吻合后，完成向目标工作区的安全装配：

| 序号 | 文件角色 | 目标工作区路径 | 大小 (Bytes) | SHA-256 哈希 | 校验结果 |
|---|---|---|---|---|---|
| 1 | **回测模块导出** | `research/backtest/__init__.py` | 702 | `fc70fc8ecc50cd3d4b1ddd979632dc10caa03db6c179ee67f5b88faa63ab0e4d` | ✅ 严格一致 |
| 2 | **配置定义** | `research/backtest/config.py` | 1,852 | `8a1ef81ff4fa6ee3e15cdbbba41149bb2f9e9335b0fbba18bfe4707a5df55e61` | ✅ 严格一致 |
| 3 | **核心引擎** | `research/backtest/engine.py` | 19,713 | `60c42f92e6007ca4f898a3dd6b0447e89f0ecc298cb7611ea3a91c03a724c3e4` | ✅ 严格一致 |
| 4 | **摩擦/滑点模型** | `research/backtest/impact.py` | 2,687 | `cc825912f35a3ef8caa6c0205422ebe81468e76823489eaeaa548128da0d81a2` | ✅ 严格一致 |
| 5 | **摄取器导出** | `research/backtest/ingesters/__init__.py` | 1,040 | `c881c83a9805b98f9d61c153e33e8c8b76c5128611db42435c0a011213342ad0` | ✅ 严格一致 |
| 6 | **摄取器基类** | `research/backtest/ingesters/base.py` | 1,128 | `31b2553169c6851eb34cbc76b72c2193e7a9f92b39fe1bb7ff15713755918d15` | ✅ 严格一致 |
| 7 | **FOMO 摄取器** | `research/backtest/ingesters/fomo.py` | 4,285 | `e3bd51cc9d30e57bffc56bd4f937aed3d50efda59a117bdfe6d534b5f30001a8` | ✅ 严格一致 |
| 8 | **通用 CSV 摄取器** | `research/backtest/ingesters/generic_csv.py` | 4,005 | `5447a249fb5bc299a402f665e4aba667c7f93d46b8e5521c9cf6580be09df796` | ✅ 严格一致 |
| 9 | **离线价格缓存** | `research/backtest/price_cache.py` | 9,171 | `a8efcd6b0d3ec7c05d1e6e3db139c587f5a71d33a700f2858589b36a7508c974` | ✅ 严格一致 |
| 10 | **报告生成器** | `research/backtest/reporter.py` | 9,571 | `0c793ebd3170ada74c2e6206c7aad40b30046749dd0ea5b8357fd61812819394` | ✅ 严格一致 |
| 11 | **回测引擎测试** | `tests/test_backtest_engine.py` | 8,714 | `af24a278f75e97c8e046a31c813cb0e29722f82d5783606c0a088800543d9233` | ✅ 严格一致 |
| 12 | **离线边界测试** | `tests/arc_v3/independent/test_backtest_offline_boundaries.py` | 7,178 | `c7c5cde3054501951eb96a36c401d2c9f5e7178e9625a63686853d4db8449686` | ✅ 修正核验一致 |

---

## 二、 边界测试定点 Lint 修正与 AST 机械等价性核验

对 `tests/arc_v3/independent/test_backtest_offline_boundaries.py` 的微小 delta 执行独立静态核验：
1. **Delta 范围**:
   - `import pytest` 与第一方 `from research...` 间补充 1 空行（满足项目 `pyproject.toml` isort 分组规则，消除 Ruff I001）。
   - `pc.load_candles("sub_dir\\pool_name")` 增加 raw string 前缀改为 `r"sub_dir\\pool_name"`（消除 Python 3.12 `invalid escape sequence '\p'` 警告）。
2. **AST 机械等价比对**:
   - `ast.Constant.value` 比对：旧值 `'sub_dir\\\\pool_name'` 与新值 `'sub_dir\\\\pool_name'` 完全一致（`True`）。
   - 非 import 语句 AST 比对：剥离 import 后 `ast.dump(tree_old) == ast.dump(tree_new)` 结论为 **`True`**。
   - Python 3.12 编译警告从 1 处降为 0 处。

---

## 三、 上游义务与适配哈希登记 (tools/qa/upstream_obligations.py)

1. **测试用例适配哈希登记**:
   - `tests/test_backtest_engine.py` 在 `docs/reuse/IMPORT_MANIFEST.json` 中存在冻结记录。
   - 在 `tools/qa/upstream_obligations.py` 的 `KNOWN_ARC_ADAPTATIONS` 字典中登记该测试的新哈希与因果理由：
   ```python
   "tests/test_backtest_engine.py": {
       "reason": "TASK-B4-ENGINE-ISOLATION-IMPL adaptation to research.backtest and research.fifo, original 6 tests 39 assertions preserved",
       "expected_sha256": "af24a278f75e97c8e046a31c813cb0e29722f82d5783606c0a088800543d9233",
   },
   ```
2. **原测试义务完整保留**:
   - 保留原 6 个测试函数（`test_fomo_ingester_direction_rules`, `test_generic_csv_ingester`, `test_friction_model`, `test_price_cache_local`, `test_matrix_backtest_engine_and_reporter`, `test_trader_sample_size_protection`）。
   - 保留全部 39 处断言，零测试被删除、弱化或跳过。

---

## 四、 安全源清单同步 (scripts/test_safety_source_manifest.json)

1. **新增条目**:
   - 新增 10 个 `research/backtest` 模块与 1 个独立边界测试，共计 11 路径：
     - `research/backtest/__init__.py`
     - `research/backtest/config.py`
     - `research/backtest/engine.py`
     - `research/backtest/impact.py`
     - `research/backtest/ingesters/__init__.py`
     - `research/backtest/ingesters/base.py`
     - `research/backtest/ingesters/fomo.py`
     - `research/backtest/ingesters/generic_csv.py`
     - `research/backtest/price_cache.py`
     - `research/backtest/reporter.py`
     - `tests/arc_v3/independent/test_backtest_offline_boundaries.py`
   - 清单条目总数从 480 增加至 491。
2. **排序与唯一性**:
   - 经实数程序严格执行自然字典排序与 `set` 去重。
   - 完整保留税费门禁（`tests/atomic_execution/test_input_tax_admission.py` 等）、清洗器（`research/cleaner/*`）与 FIFO 撮合器（`research/fifo/*`）条目。

---

## 五、 冻结清册与注册表刚性防线

1. **冻结清单不可篡改**:
   - `docs/reuse/IMPORT_MANIFEST.json` 与 `docs/reuse/CODE_MANIFEST.json` 保持严格 Freeze 状态，`git status` 确认 0 变更。
2. **归档注册表保持不变**:
   - `LEGACY_TEST_MIGRATION_REGISTRY` 仅包含 `tests/test_v4_poolkey.py` 与 `tests/test_rpc_policy.py` 2 条历史记录，0 增删改。
3. **架构边界防线**:
   - 严禁顶层创建 `backtest` 包，所有回测逻辑收敛于 `research/backtest`。
   - 严禁创建任何生产 Tick 缓存目录或文件。

---

## 六、 独立审查凭据与反证真实性说明

复用独立审查 `/tmp/arc-b4-r2-review/` 的实证成果（不重复执行业务代码）：
1. **基线 14 测试全绿**: 6 原测试 + 8 离线边界测试全部 PASS（见 `/tmp/arc-b4-r2-review/combined_test.log`）。
2. **破坏性反证有效**:
   - 破坏价格缓存导致 3 个边界测试失败变红（见 `/tmp/arc-b4-r2-review/control_old_cache.log`）。
   - 破坏小样本交易员保护常数导致测试失败（见 `/tmp/arc-b4-r2-review/sabotage_sample_size.log`）。

---

## 七、 门禁静态自检与红线准入声明

1. **静态自检**:
   - **AST 语法解析**: 12 个候选文件 + `tools/qa/upstream_obligations.py` 共 13 个 Python 文件及清单 JSON 解析 100% PASS。
   - **Ruff 代码风格**: 使用显式 `--config pyproject.toml` 检查通过，0 errors, 0 warnings (`All checks passed!`)。
   - **Mypy 类型检查**: 使用显式 `--config-file pyproject.toml` 检查通过，0 issues (`Success: no issues found in 13 source files`)。
2. **红线遵循**:
   - 零真实资金、零外部网络、零 RPC 依赖、零私钥接触。
   - 主仓 `/root/projects/crypto/arc-chain` 100% 只读未触碰，零 commit / 零 push / 零 reset。
   - 本合流步骤未执行任何业务 import、未执行 pytest、未调用 bwrap 沙箱。
