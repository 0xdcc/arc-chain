# Arc-Chain 市场 M1 合流集成验收报告：Market Data 目录与类型契约

- **工单编号**: `TASK-M1-CATALOG-INTEGRATION`
- **集成主脑**: M1 总控与集成主脑 (Master Control & Integration)
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **候选来源**: `/tmp/arc-m1-catalog/src` (纯只读源)
- **独立审查依据**: `/tmp/arc-m1-review` (`REVIEW_REPORT.md` / `integrated/` / `control/`)
- **交付凭据产物**:
  - `docs/acceptance/RESEARCH_MARKET_CATALOG.md` (本集成验收报告)
  - `/tmp/arc-m1-integration/RESULT.md` (集成交付与状态报告)

---

## 一、 精确装配与哈希核验一致表

本集成操作严格提取通过 M1 独立审查准入的净基线 4 文件，SHA-256 哈希与候选源 `/tmp/arc-m1-catalog/src` 及受审区 `/tmp/arc-m1-review/integrated` 保持 100% 精确一致：

| 序号 | 模块角色 | 目标路径 | 大小 (Bytes) | SHA-256 哈希 | 校验结果 |
|---|---|---|---|---|---|
| 1 | **市场数据模块导出** | `research/market_data/__init__.py` | 794 | `e36dbdaf50621fbd18d560c2b2879846496d2da1686c7265fc3df79c5de33142` | ✅ 严格一致 |
| 2 | **公共契约领域模型** | `research/market_data/types.py` | 12,709 | `b980b0d1fdb7b68009448a02ad821ee29c482cdbc55e81575386fb1f2463a279` | ✅ 严格一致 |
| 3 | **代币与池目录核心** | `research/market_data/catalog.py` | 6,419 | `092656cedca1876e8fb492c6538b152b7c95aa945dd4f208652f7555c3104a07` | ✅ 严格一致 |
| 4 | **目录测试套件** | `tests/test_market_data_catalog.py` | 10,080 | `777094116fe97f9b747d9b01488c65354e7dcb9c6552602a9054ebe6a8666698` | ✅ 严格一致 |

---

## 二、 排除父包覆盖与命名空间保护

候选源包含的顶层父包文件严格**禁止覆盖**，保障已有命名空间契约与架构安全：
1. **`research/__init__.py`**: 保持既有工作区版本（SHA-256: `0d9b4276824f4b26fd76a6b28fbdae40010a45e9fb56edfa1fca471aad023ece`，81 字节），保留 W3 离线只读分析文档契约，候选 127 字节冗余文件被排除。
2. **`tests/__init__.py`**: 保持工作区既有 0 字节空包标记不变。
3. **导入链路验证**: Python 命名空间包机制正常工作，`research.market_data` 导入无任何阻断。

---

## 三、 AST 断言机械保留与 4663 语义解耦

1. **旧 39 个 Assert 机械保留**:
   - 对比原测试套件与装配测试套件，AST 解析确认 39 个 `assert` 语句结构、逻辑与阈值 100% 保留。
   - 差异严格限定于导入路径（`arbitrage.` -> `research.`）、文档说明及 line 115 mypy `# type: ignore[arg-type]` 注解。
   - 零测试跳过（无 `pytest.skip` / `xfail`）。
2. **4663 语义物理隔离**:
   - `ROBINHOOD_CHAIN_ID: int = 4663` 及相关常量严格限定为离线研究与历史回测向量。
   - 遵循 `AGENTS.md` 及 `07_本地池目录变化处理规则.md`，严禁将 4663 资产导入 Arc 主网 (5042) 活跃 registry 注册表。

---

## 四、 动态真实测试与单控制反证复核

1. **测试套件真实通过**:
   - `tests/test_market_data_catalog.py` 18 项参数化与单元测试全数通过（0.04s）。
   - 涵盖非法 token 查询异常拦截（6 组变异）、池白名单校验、AST 纯净度审计（无网络/无子进程）。
2. **单控制反证已验**:
   - 依据 `/tmp/arc-m1-review` 审查记录，单控制篡改 WETH 精度（18 -> 6）精准触发 `AssertionError: assert 6 == 18`，测试套件具备刚性防线。

---

## 五、 安全清单与义务登记

1. **源文件安全清单 (`scripts/test_safety_source_manifest.json`)**:
   - 仅新增 3 个 `research/market_data` 模块路径：
     - `research/market_data/__init__.py`
     - `research/market_data/catalog.py`
     - `research/market_data/types.py`
   - 清单项从 496 增至 499（精确净增 3），保持严格字典序排序。`tests/test_market_data_catalog.py` 原本已在清单中，不产生重复项。
2. **上游义务与适配哈希登记 (`tools/qa/upstream_obligations.py`)**:
   - 仅更新旧测试 `tests/test_market_data_catalog.py` 的 `expected_sha256` 为 `777094116fe97f9b747d9b01488c65354e7dcb9c6552602a9054ebe6a8666698`。
   - `LEGACY_TEST_MIGRATION_REGISTRY` 严格保持 2 条既有条目（`test_v4_poolkey.py` 与 `test_rpc_policy.py`），零改动。
3. **冻结保护与 CLI 保持不动**:
   - `docs/reuse/IMPORT_MANIFEST.json` 与 `docs/reuse/CODE_MANIFEST.json` 保持严格 Freeze 状态，0 变更。
   - `apps/cli.py` 保持不动。

---

## 六、 静态分析与代码质量

1. **Ruff 代码规范**:
   - `research/market_data` 目录执行 `ruff check`：**All checks passed! (0 errors)**。
2. **Mypy 类型系统**:
   - `research/market_data` 及 `tests/test_market_data_catalog.py` 执行 `mypy`：**Success: no issues found in 4 source files**。
3. **AST 纯净性**:
   - `test_catalog_ast_audit` 确认 AST 节点无网络、无子进程调用。

---

## 七、 资金红线与操作合规

- 遵循 `AGENTS.md` 安全红线：
  - 零真实资金、零合约交互、零私钥接触。
  - 源 `main` 仓库（`/root/projects/crypto/arc-chain`）保持零写入（0 writes）。
  - 无 git commit，无 git push，无外部网络请求。
