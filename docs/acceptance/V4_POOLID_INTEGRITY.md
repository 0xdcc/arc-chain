# V4 PoolId Keccak-256 校验与全目录准入装配验收报告

- **工单编号**: `TASK-V4-POOLID-INTEGRATE`
- **集成主脑**: M1 总控与集成 (Master Control & Integration)
- **工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **准入源**: `/tmp/arc-v4-poolid-qualification-r2/src` 与独立审查 `/tmp/arc-v4-poolid-review-r2/RESULT.md`
- **执行方式**: Codex 本地静态装配 (无网络、无真实资金、无 RPC 调用、无合约部署、禁止 commit/push/服务重启)
- **交付产物**:
  - `docs/acceptance/V4_POOLID_INTEGRITY.md` (本验收报告)
  - `/tmp/arc-v4-poolid-integration/RESULT.md` (交付审查凭据)

---

## 1. 修复背景与技术实现

### 1.1 根因与全目录动态准入演进
- **原旧测试义务溯源**: `tests/test_v4_poolkey.py::test_all_manifest_entries_pass_keccak_verification` 针对静态 Robinhood 池清单遍历校验。根据 `AGENTS.md` 及架构规范，Arc 严禁导入 Robinhood 静态池清单，且全目录在 Arc 中为动态流式/批处理发现集。
- **准入缺口消除**:
  - 在 `QuoteCatalogBridge.qualify_v4_pool` 增加了 `_HEX_BYTES32_RE` 格式拦截与 `pool.v4_key.compute_pool_id()` 刚性比对，对不匹配的 pool_id 抛出 `ArcMarketIneligibleError`。在 `qualify_catalog` 批量处理时精准分流至 `report.rejected`，防止被篡改池注入生产目录。
  - 在 `V4MarketDiscoveryEngine.process_initialize` 写入内部状态索引前强验 `v4_key.compute_pool_id()`，不匹配抛出 `ArcValidationError`，杜绝状态污染。
- **测试夹具与反测更新**:
  - 修正测试夹具中合规模拟池的 `pool_id` 为其实际计算的 Keccak 值。
  - 增补独立测试套件 `test_v4_catalog_poolid_integrity.py`，全覆盖 8 项边界场景（规范推导、单条校验、批量隔离、写前阻断、大小写归一化保留 0x、非法 0X 前缀拦截等）。

---

## 2. 精确写范围与文件哈希核验单 (SHA-256)

本次集成严格限制于受审的 5 个代码与测试文件，以及清单登记与本文档：

| 序号 | 文件路径 | 操作类型 | 目标 SHA-256 | 落盘一致性 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `arc_markets/quote_catalog.py` | MODIFIED | `788710972eb8d57cc4e38ccdc18eef458c9219b04da2f94ac262316d730d4b56` | **MATCH (100%)** |
| 2 | `arc_markets/v4_discovery.py` | MODIFIED | `f811e37adc05ae6b930dd6ce477e901727c113d167fdc19414658f7af973f72c` | **MATCH (100%)** |
| 3 | `tests/arc_v3/markets/test_catalog_snapshots.py` | MODIFIED | `2b58ee76df4a8748f00314b0f20cfd6f6cbf5b7b37f61d45a9ae5bcca98ab730` | **MATCH (100%)** |
| 4 | `tests/arc_v3/markets/test_v4_identity.py` | MODIFIED | `2458f0a2a8d12b667b99fb581e375c0a17e4f1f5554ba331b14151224ff9c544` | **MATCH (100%)** |
| 5 | `tests/arc_v3/independent/test_v4_catalog_poolid_integrity.py` | CREATED | `b5af7dd091c08cf3b56037d378bb037d709fd9a6098ca3d0c5510c9c3b3a1f38` | **MATCH (100%)** |
| 6 | `scripts/test_safety_source_manifest.json` | MODIFIED | *(增补新测试路径，保持排序与唯一)* | **MATCH (100%)** |
| 7 | `docs/acceptance/V4_POOLID_INTEGRITY.md` | CREATED | *(本文件)* | **MATCH (100%)** |

### 冻结清单 Membership 审查
- 检索 `docs/reuse/CODE_MANIFEST.json` 与 `docs/reuse/IMPORT_MANIFEST.json`：变更的 5 个文件均不属于外部复用包受管范畴，均为 Arc 5042 原生架构文件。
- 历史测试文件 `tests/test_v4_poolkey.py` 维持 **ARCHIVE FROZEN**，零修改、零移动。

---

## 3. 独立审查证据引用与动态验证汇总

引用自独立审查报告 `/tmp/arc-v4-poolid-review-r2/RESULT.md`：

### 3.1 核心测试通过情况
- 新套件 `test_v4_catalog_poolid_integrity.py` 8 项测试方法在宿主 bwrap 隔离沙箱中真实执行，**8 passed (0.31s)**。
- Arc 核心套件 8 大测试目录完整回归实测 **1514 项通过**。
- 覆盖率测试 `test_manifest_coverage.py` 在未登记时精准报出 1 项缺失，本次集成补齐登记后完成闭环。

### 3.2 破坏性反证与旁路转红存证
- 复用 R1 阶段真实的变异旁路日志（`control_quote_catalog_red.log`、`control_v4_discovery_red.log`），证明移除相应 Keccak 校验后测试立即转红，防御机制刚性有效。

---

## 4. 旧原文件归档准入说明

- 生产层面对 `qualification_gap` 的修复已通过全套测试验证。
- 历史旧文件 `tests/test_v4_poolkey.py` 的归档需待最终全断言核验与历史保存步骤完成，本单不予移动或删除，严格保持现有代码树与冻结清单的稳定性。
