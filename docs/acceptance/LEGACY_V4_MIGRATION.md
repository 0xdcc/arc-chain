# 旧 V4 测试义务迁移与准入核对文档 (LEGACY_V4_MIGRATION)

- **工单编号**: TASK-V4-METADATA-ADMIT
- **审查与装配主脑**: M1 总控与集成 (Master Control & Integration)
- **受管工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **审查目标旧文件**: `tests/test_v4_poolkey.py` (SHA-256: `96ff8f34e4e22c771c2aa56769b3e7ccfaf314611442c7b4303005bc41a8a90a`, 435 行, 16 函数)
- **准入新测试文件**: `tests/arc_v3/independent/test_legacy_v4_metadata.py` (SHA-256: `b7cfd71dbb971ee54328b3c799cf4c98aa889e7720f5bb2afe948544d3a54f54`, 358 行, 7 用例)
- **文档基准时间**: 2026-09-13

---

## 1. 准入背景与架构原则

### 1.1 逐项迁移与闭环前置原则
依据用户批准的旧 49 测试迁移准则：只有在通用义务测试与证据完全闭合后，纯 Robinhood 专属原测试文件方可进行后续归档。
本工单严格执行**单向静态装配**：
1. 仅准入已在隔离沙箱通过 EVM Keccak-256 复验的 7 项元数据测试（`tests/arc_v3/independent/test_legacy_v4_metadata.py`）；
2. **严禁立即物理归档旧文件**：旧测试文件 `tests/test_v4_poolkey.py` 保持在原路径冻结不动；
3. 不破坏现有工作树的 dirty 状态；不改动任何生产算法；不新增超出元数据准入范围的功能；
4. 严格执行静态检查（AST/ruff/mypy），杜绝任何网络、真实资金或自动 push/commit 操作。

### 1.2 核心映射措辞纠偏 (严禁虚构等价)
在梳理 `tests/test_v4_poolkey.py` 的 16 个旧函数映射关系时，必须遵循客观事实，严禁将行为变化伪称为语义等价：

1. **纠偏一（代币排序：行为改变而非原语义等价）**:
   - **旧逻辑** (`test_token_ordering_invariance`): 旧 Robinhood 实现内部在遇到反序输入时，进行静默自动调换（`token0 = min(c0, c1)`），因此传入 `(currency1, currency0)` 计算出的 `pool_id` 与正序完全相同。
   - **Arc 行为**: Arc 架构严格遵循以太坊 EVM 底层规范，刚性要求 `currency0 < currency1`。若传入逆序输入，系统直接抛出 `ArcValidationError`（Fail-Closed 刚性拒绝）。
   - **判定结论**: 这是按 Arc 严格接口规范做出的**明确行为变化**，属于 `legacy_interface_out_of_scope` 并辅以替代安全验证（由 `test_strict_currency_order_fail_closed` 与 `test_address_casing_invariance` 提供），**绝不能称为“原语义等价”**。

2. **纠偏二（全表 Keccak 遍历与通用目录一致性边界）**:
   - **旧逻辑** (`test_all_manifest_entries_pass_keccak_verification`): 旧测试对离线爬取的静态字典 `V4_POOL_METADATA`（>=100 条目）进行全表循环遍历验证。
   - **Arc 覆盖边界**: 独立套件中的 6 大标准测试向量（`test_six_canonical_pool_vectors_exact_keccak_derivation`）和字段突变敏感性测试已证明底层 Keccak-256 5-Tuple 算力实现的正确性，`test_v4_identity.py` 覆盖了事件发现池 ID 计算。
   - **判定结论**: 6 个标准向量的数学推导**不能完全豁免通用目录一致性**。针对动态目录全表的一致性遍历目前尚未形成通用测试覆盖，该边界必须如实保留为 **qualification_gap**，**绝不能拍板现在直接归档旧文件**。

---

## 2. 16 项旧测试函数精确审计与映射表

| # | 类名 | 原函数名 | 原断言特征 | 修正分流状态 | 真实目标测试路径 / 承接位置 | 验证存证与证据日志 | 架构说明与边界界定 |
|:---:|:---|:---|:---|:---:|:---|:---|:---|
| 1 | `TestV4PoolKeyMath` | `test_ai_usdg_023_pool_id_matches_target` | `computed_id == AI_USDG_V4_POOL_ID.lower()` | **covered_verified** | `tests/arc_v3/independent/test_legacy_v4_obligations.py::test_six_canonical_pool_vectors_exact_keccak_derivation` | `/tmp/arc-v4-metadata-corrected/RESULT.md`; `logs/bwrap_candidate_test.log` | AI/USDG 0.23% (fee=2300, ts=23, hooks=0x0) EVM Keccak-256 5-Tuple 计算精确吻合。 |
| 2 | `TestV4PoolKeyMath` | `test_pons_usdg_03_pool_id` | `computed_id == target_pid.lower()` | **covered_verified** | `tests/arc_v3/independent/test_legacy_v4_obligations.py::test_six_canonical_pool_vectors_exact_keccak_derivation` | `/tmp/arc-v4-metadata-corrected/RESULT.md` (Vector 2) | PONS/USDG 0.3% (fee=3000, ts=60, hooks=0x0) EVM Keccak-256 计算精确吻合。 |
| 3 | `TestV4PoolKeyMath` | `test_pons_usdg_07_pool_id` | `computed_id == target_pid.lower()` | **covered_verified** | `tests/arc_v3/independent/test_legacy_v4_obligations.py::test_six_canonical_pool_vectors_exact_keccak_derivation` | `/tmp/arc-v4-metadata-corrected/RESULT.md` (Vector 3) | PONS/USDG 0.7% (fee=7000, ts=140, hooks=0x0) EVM Keccak-256 计算精确吻合。 |
| 4 | `TestV4PoolKeyMath` | `test_spy_usdg_03_pool_id` | `computed_id == target_pid.lower()` | **covered_verified** | `tests/arc_v3/independent/test_legacy_v4_obligations.py::test_six_canonical_pool_vectors_exact_keccak_derivation` | `/tmp/arc-v4-metadata-corrected/RESULT.md` (Vector 4) | SPY/USDG 0.3% (fee=3000, ts=60, hooks=0x0) EVM Keccak-256 计算精确吻合。 |
| 5 | `TestV4PoolKeyMath` | `test_cashcat_usdg_0269_pool_id` | `computed_id == target_pid.lower()` | **covered_verified** | `tests/arc_v3/independent/test_legacy_v4_obligations.py::test_six_canonical_pool_vectors_exact_keccak_derivation` | `/tmp/arc-v4-metadata-corrected/RESULT.md` (Vector 5) | CASHCAT/USDG 0.269% (fee=2690, ts=54, hooks=0x0) EVM Keccak-256 计算精确吻合。 |
| 6 | `TestV4PoolKeyMath` | `test_weth_usdg_001_native_eth_pool_id` | `computed_id == target_pid.lower()` | **covered_verified** | `tests/arc_v3/independent/test_legacy_v4_obligations.py::test_six_canonical_pool_vectors_exact_keccak_derivation` | `/tmp/arc-v4-metadata-corrected/RESULT.md` (Vector 6) | 原生 ETH `address(0)` (fee=100, ts=1, hooks=0x0) EVM Keccak-256 计算精确吻合。 |
| 7 | `TestV4PoolKeyMath` | `test_token_ordering_invariance` | `pid1 == pid2 == AI_USDG_V4_POOL_ID.lower()` | **legacy_interface_out_of_scope** (加替代安全验证) | `tests/arc_v3/independent/test_legacy_v4_obligations.py::test_strict_currency_order_fail_closed` 与 `test_address_casing_invariance` | `tests/arc_v3/independent/test_legacy_v4_obligations.py` | **行为变化说明**：旧逻辑静默重排反序输入；Arc 严格 EVM 规范强制 `currency0 < currency1`，逆序直接抛错拒收。此项非原语义等价，属严格接口规范替代。 |
| 8 | `TestV4ManifestIntegrity` | `test_manifest_contains_high_depth_pools` | `len(V4_POOL_METADATA) >= 100` | **legacy_interface_out_of_scope** | N/A (由事件发现与快照体系替代) | `tests/arc_v3/markets/test_catalog_snapshots.py` | 旧 Robinhood 离线静态全量字典 `V4_POOL_METADATA` 在 Arc 中明确不提供；Arc 采用动态事件发现引擎（`V4MarketDiscoveryEngine`）。 |
| 9 | `TestV4ManifestIntegrity` | `test_all_manifest_entries_pass_keccak_verification` | `assert computed == pid.lower()` | **qualification_gap** (边界明确) | 6 大标准向量覆盖数学算力；缺通用目录一致性全量测试 | `tests/arc_v3/independent/test_legacy_v4_obligations.py` | **边界保留说明**：6 大向量证明 Keccak 推导正确，但不能完全豁免全表目录一致性。缺少通用目录一致性遍历测试，明确保留 gap，不得作为归档依据。 |
| 10 | `TestV4ManifestIntegrity` | `test_get_v4_pool_metadata_case_insensitive` | `upper_meta == lower_meta` | **covered_verified** | `tests/arc_v3/independent/test_legacy_v4_obligations.py::test_get_v4_pool_case_insensitive_lookup` | `tests/arc_v3/independent/test_legacy_v4_obligations.py` | 大小写不敏感查找逐断言核对通过（验证 `upper_pool == lower_pool` 且 `tick_spacing == 23`）。 |
| 11 | `TestV4PoolSpec` | `test_v4_pool_spec_default_fields` | `spec.tick_spacing == 60`, `hooks == 0x0` | **legacy_interface_out_of_scope** | N/A (弃用旧 Robinhood 专有 dataclass) | `arc_markets/v4_events.py` | 旧实盘展示容器 `V4PoolSpec`（含 `label`, `fee_bps` 等）在 Arc 中彻底弃用，由 `V4PoolKey` 与 `PoolDescriptor` 替代。 |
| 12 | `TestV4PoolSpec` | `test_v4_pool_spec_custom_fields` | `spec.tick_spacing == 23`, `compute_pool_id()` | **legacy_interface_out_of_scope** (加底层验证) | `tests/arc_v3/independent/test_legacy_v4_obligations.py::test_process_initialize_preserves_custom_tick_and_hooks` | `tests/arc_v3/independent/test_legacy_v4_obligations.py` | 自定义 tickSpacing 与 hooks 提取已在链上 Initialize 事件解析与 `V4PoolKey` 构造中完整覆盖。 |
| 13 | `TestPoolConfigConversion` | `test_scanned_to_pool_spec_matches_v4_metadata` | `isinstance(spec, V4PoolSpec)`, `tick_spacing == 23`, `compute_pool_id()` | **covered_verified** (核心转换) / **legacy_interface_out_of_scope** (展示字段) | `tests/arc_v3/independent/test_legacy_v4_metadata.py::test_scanned_to_pool_descriptor_preserves_all_metadata_fields` (及全套 7 项元数据测试) | `/tmp/arc-v4-metadata-corrected/RESULT.md`; `logs/bwrap_candidate_test.log` | **核心闭合**：扫描池到 `PoolDescriptor` 的核心字段（token0/1, fee uint24, ts, hooks, manager, pool_id, decimals）完全承接并全绿验证；展示字段（name, tvl_usd, symbols, fee_bps）明确不承接。 |
| 14 | `TestExecutorV4CalldataAlignment` | `test_plan_from_triangular_alert_populates_v4_leg_metadata` | `len(legs) == 3`, `pool_fee == 2300`, `tick_spacing == 23` | **covered_verified** (跨腿传递) / **legacy_interface_out_of_scope** (旧执行器入口) | `tests/arc_v3/independent/test_legacy_v4_plan_metadata.py::test_arc_planner_preserves_v4_leg_metadata` & `test_legacy_alert_entry_point_boundary_distinction` | `/tmp/arc-v4-hook-final-validation/RESULT.md` | 跨腿元数据传递（`len=3, venue='uniswap_v4', fee=2300, ts=23, hooks=0x0`）逐断言闭合；旧实盘执行器入口被安全边界明确拒绝。 |
| 15 | `TestExecutorV4CalldataAlignment` | `test_universal_router_mixed_calldata_decodes_exact_pool_key` | `decoded_func == "execute"`, `fee == 2300`, `computed_hash == AI_USDG_V4_POOL_ID` | **covered_verified** | `tests/arc_v3/independent/test_legacy_v4_plan_metadata.py::test_mixed_3hop_v4_calldata_independent_abi_decoding` | `/tmp/arc-v4-hook-final-validation/RESULT.md` | Universal Router 混合 V3/V4 路由 calldata 编码与精准 ABI 解码（command 0x10）完全闭合。 |
| 16 | `TestExecutorV4CalldataAlignment` | `test_universal_router_all_v4_calldata_decodes_exact_path_keys` | `len(path_keys) == 3`, 精确 ts/fee 序列 | **covered_verified** | `tests/arc_v3/independent/test_legacy_v4_plan_metadata.py::test_pure_v4_multihop_pathkeys_independent_abi_decoding` | `/tmp/arc-v4-hook-final-validation/RESULT.md` | Universal Router 纯 V4 多跳 swap calldata 编码与精准 PathKey 解码（command 0x11）完全闭合。 |

---

## 3. 清单管理与依赖冻结边界

1. **源码清单更新**:
   - 文件: `scripts/test_safety_source_manifest.json`
   - 操作: 新增条目 `"tests/arc_v3/independent/test_legacy_v4_metadata.py"`
   - 规则: 保持严格字母排序与唯一性。
   - 文件计数变化: **469 -> 470**。
2. **冻结清单不可侵犯**:
   - `docs/reuse/IMPORT_MANIFEST.json`: **绝对冻结**，零改动。本测试为 Arc 原生独立编写，不在该清单中。
   - `docs/reuse/EXCLUSION_MANIFEST.json`: **绝对冻结**，零改动。
   - `tools/qa/upstream_obligations.py`: **严禁虚造 `KNOWN_ARC_ADAPTATIONS`**，保持原样。
3. **旧测试文件状态**:
   - `tests/test_v4_poolkey.py`: 保持现状（SHA: `96ff8f34e4e22c771c2aa56769b3e7ccfaf314611442c7b4303005bc41a8a90a`），**不归档、不删除、不重命名**。

---

## 4. 归档决策与后续流程

- **当前归档判定**: **🔴 维持不归档 (HOLD / NOT ARCHIVED)**。
- **原因说明**:
  1. 尽管第 13 项函数的 7 项元数据测试已入库，但按本工单授权范围仅做测试准入与清单装配；
  2. 第 9 项函数的全表通用目录一致性边界留有 gap；
  3. 待后续宿主组合回归测试与 M4 独立审查后，方可启动独立归档裁定。
- **承诺约束**: 源码清单达到 470 项并不意味着自动清理旧文件，所有归档行为均需独立授权与证据闭合。
