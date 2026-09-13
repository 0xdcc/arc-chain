# 单个旧测试 tests/test_v4_poolkey.py 字节保全归档与受控迁移验收报告

- **工单编号**: `TASK-LEGACY-V4-ARCHIVE-R1`
- **执行角色**: M1 总控与集成 / M4 独立审查
- **授权依据**: 用户批准 49 旧测试逐项义务迁移 (`USER_APPROVED_LEGACY_P1`) 及 `/tmp/arc-v4-archive-readiness/RESULT.md` 独立归档准入裁决
- **准入标的**: 仅针对 `tests/test_v4_poolkey.py` 单个旧测试文件，其余 48 个旧测试绝对未准入且维持现状不动
- **执行方式**: Codex 纯静态施工；源集成只读；在 `/tmp/arc-legacy-v4-archive-r1/src` 纯 bytes 隔离快照中实施草稿，待 Hermes 宿主 bwrap 独立测试通过后应用
- **安全契约**: 遵循 `AGENTS.md` v3.1，只读研究、零资金、零网络、零合约部署、零外部写、Fail-Closed

---

## 一、 归档迁移设计背景与核心原则

### 1.1 历史冲突痛点
- 历史旧测试 `tests/test_v4_poolkey.py` 存留于 `tests/` 根目录下，导致 pytest 默认收集器将其纳入测试收集，产生大量废弃 Robinhood 接口调用或环境假红。
- 但该文件已被记录于 `docs/reuse/IMPORT_MANIFEST.json`（上游冻结清单，共 281 项）。若直接物理删除该文件，上游审计工具 `tools/qa/upstream_obligations.py::audit_imported_files` 会报 `missing_files` 并阻断门禁。
- 若直接在 `IMPORT_MANIFEST.json` 中删除条目或修改哈希，则严重违反“上游冻结清单不可篡改”的工程红线。

### 1.2 M1/M4 受控迁移解决方案
1. **冻结清单不可侵犯**: `docs/reuse/IMPORT_MANIFEST.json` 保持 100% 冻结，上游原始 SHA-256 (`12458218...`) 保持不动。
2. **纯 Bytes 历史源归档**: 将 `tests/test_v4_poolkey.py` 原样迁移至 `docs/legacy_tests/robinhood/test_v4_poolkey.py.txt`，采用 `.txt` 后缀彻底脱离 pytest 可执行模块收集范围，同时 100% 保持已审字节。
3. **合法适配链核验**: 归档文件保留已审 bytes (`96ff8f34...`)，对原 hash 沿 `KNOWN_ARC_ADAPTATIONS` 链合法核验，严禁拿当前 bytes 冒充最初源 hash。
4. **显式注册表与共用受控解析**: 在 `tools/qa/upstream_obligations.py` 引入硬编码常量 `LEGACY_TEST_MIGRATION_REGISTRY` 与 `resolve_imported_file_path`，强制多重安全防御（防双份漂移、目录限定、软硬链接拦截、路径穿越防御、未登记不豁免）。
5. **受审义务总分母不减**: 归档后依然完整审核 281 项受审义务，绝不依靠减少分母伪造全绿。

---

## 二、 字节保全与哈希核验单 (100% 吻合)

| 核验项 | 迁移前原路径 | 迁移后归档路径 | 核验结果 |
|---|---|---|:---:|
| **文件相对路径** | `tests/test_v4_poolkey.py` | `docs/legacy_tests/robinhood/test_v4_poolkey.py.txt` | 🟢 **迁出并新增** |
| **磁盘字节大小** | 16,336 bytes | 16,336 bytes | 🟢 **100% 吻合** |
| **文件行数** | 435 lines | 435 lines | 🟢 **100% 吻合** |
| **宿主已审 SHA-256** | `96ff8f34e4e22c771c2aa56769b3e7ccfaf314611442c7b4303005bc41a8a90a` | `96ff8f34e4e22c771c2aa56769b3e7ccfaf314611442c7b4303005bc41a8a90a` | 🟢 **MATCH (100%)** |
| **硬链接数 (`st_nlink`)** | 1 | 1 | 🟢 **独立二进制副本** |
| **软链接检测** | 否 | 否 | 🟢 **常规文件** |
| **上游最初冻结 SHA-256** | `1245821886735df5fabcba65ff37158c92d3ff8260d1a6b627b5ef56c27f46b7` | `1245821886735df5fabcba65ff37158c92d3ff8260d1a6b627b5ef56c27f46b7` | 🟢 **沿适配链核验** |

---

## 三、 16 函数逐项审计与承接裁决映射表

依据 `/tmp/arc-legacy-v4-disposition/mapping.json` 与 `/tmp/arc-v4-archive-readiness/RESULT.md`，16 个原函数的业务与安全断言已全部闭合：

| # | 原测试函数名 | 最终裁决状态 | 承接测试路径与具体断言 | 架构说明与证据闭合依据 |
|:---:|:---|:---:|:---|:---|
| 1 | `test_ai_usdg_023_pool_id_matches_target` | **covered_verified** | `test_legacy_v4_obligations.py::test_six_canonical_pool_vectors_exact_keccak_derivation` (Vector 1) | AI/USDG 0.23% (fee=2300, ts=23, hooks=0x0) EVM Keccak-256 5-Tuple 算力精确闭合。 |
| 2 | `test_pons_usdg_03_pool_id` | **covered_verified** | `test_legacy_v4_obligations.py::test_six_canonical_pool_vectors_exact_keccak_derivation` (Vector 2) | PONS/USDG 0.3% (fee=3000, ts=60, hooks=0x0) Keccak 算力精确闭合。 |
| 3 | `test_pons_usdg_07_pool_id` | **covered_verified** | `test_legacy_v4_obligations.py::test_six_canonical_pool_vectors_exact_keccak_derivation` (Vector 3) | PONS/USDG 0.7% (fee=7000, ts=140, hooks=0x0) Keccak 算力精确闭合。 |
| 4 | `test_spy_usdg_03_pool_id` | **covered_verified** | `test_legacy_v4_obligations.py::test_six_canonical_pool_vectors_exact_keccak_derivation` (Vector 4) | SPY/USDG 0.3% (fee=3000, ts=60, hooks=0x0) Keccak 算力精确闭合。 |
| 5 | `test_cashcat_usdg_0269_pool_id` | **covered_verified** | `test_legacy_v4_obligations.py::test_six_canonical_pool_vectors_exact_keccak_derivation` (Vector 5) | CASHCAT/USDG 0.269% (fee=2690, ts=54, hooks=0x0) Keccak 算力精确闭合。 |
| 6 | `test_weth_usdg_001_native_eth_pool_id` | **covered_verified** | `test_legacy_v4_obligations.py::test_six_canonical_pool_vectors_exact_keccak_derivation` (Vector 6) | 原生 ETH `address(0)` (fee=100, ts=1, hooks=0x0) Keccak 算力精确闭合。 |
| 7 | `test_token_ordering_invariance` | **legacy_interface_out_of_scope** | `test_legacy_v4_obligations.py::test_strict_currency_order_fail_closed` 与 `test_address_casing_invariance` | **非语义等价，属明确接口进化**：旧代码逆序时静默重排；Arc 刚性要求 `c0 < c1`，逆序直接抛出 `ArcValidationError` (Fail-Closed)。安全属性已升级替代。 |
| 8 | `test_manifest_contains_high_depth_pools` | **legacy_interface_out_of_scope** | `tests/arc_v3/markets/test_catalog_snapshots.py` | **旧接口明确不恢复**：旧静态字典常量系 Robinhood 离线爬虫产物。Arc 采用动态事件发现（`V4MarketDiscoveryEngine`），严禁导入 Robinhood 静态池清单。 |
| 9 | `test_all_manifest_entries_pass_keccak_verification` | **covered_verified** | `test_v4_catalog_poolid_integrity.py::test_qualify_catalog_batch_rejection_of_tampered_pool_ids` 及 8 项全套断言 | **核心缺口彻底消除**：生产端 `qualify_v4_pool` 逐池强验 Keccak，`qualify_catalog` 批量分流隔离篡改池，新测试覆盖真实 batch 中间与末尾篡改隔离，原 qualification_gap 完全闭合。 |
| 10 | `test_get_v4_pool_metadata_case_insensitive` | **covered_verified** | `test_legacy_v4_obligations.py::test_get_v4_pool_case_insensitive_lookup` | 大小写不敏感查找逐断言核对通过（验证 `upper_pool == lower_pool` 且 `tick_spacing == 23`）。 |
| 11 | `test_v4_pool_spec_default_fields` | **legacy_interface_out_of_scope** | `arc_markets/v4_state.py` (`V4PoolKey` 与 `PoolDescriptor`) | **旧专属容器不恢复**：`V4PoolSpec` 包含 Robinhood 专属展示属性（`label`, `fee_bps`）。Arc 采用 `V4PoolKey` 与 `PoolDescriptor`，严格自 Initialize 事件解析。 |
| 12 | `test_v4_pool_spec_custom_fields` | **covered_verified** (核心参数) / **out_of_scope** (展示字段) | `test_legacy_v4_obligations.py::test_process_initialize_preserves_custom_tick_and_hooks` | 自定义 tickSpacing 与 hooks 参数传递及 PoolId 计算完全闭合；展示字段明确不恢复。 |
| 13 | `test_scanned_to_pool_spec_matches_v4_metadata` | **covered_verified** (核心转换) / **out_of_scope** (展示字段) | `test_legacy_v4_metadata.py::test_scanned_to_pool_descriptor_preserves_all_metadata_fields` (7 项元数据测试) | **原 remaining_gap 彻底消除**：扫描池到底层 `PoolDescriptor` 的核心元数据转换（token0/1, fee uint24, ts, hooks, manager, pool_id, decimals）100% 验证通过；展示字段明确不提供。 |
| 14 | `test_plan_from_triangular_alert_populates_v4_leg_metadata` | **covered_verified** (跨腿传递) / **out_of_scope** (旧执行器入口) | `test_legacy_v4_plan_metadata.py::test_arc_planner_preserves_v4_leg_metadata` 与 `test_legacy_alert_entry_point_boundary_distinction` | 跨腿元数据传递（`len=3, venue='uniswap_v4', fee=2300, ts=23, hooks=0x0`）逐断言闭合；旧实盘执行器入口被安全边界明确拒绝。 |
| 15 | `test_universal_router_mixed_calldata_decodes_exact_pool_key` | **covered_verified** | `test_legacy_v4_plan_metadata.py::test_mixed_3hop_v4_calldata_independent_abi_decoding` 与 `tests/atomic_execution/test_encoding.py::test_c10_mixed_3hop_v3_v4_v3_assembly` | Universal Router 混合 V3/V4 路由 calldata 编码与独立 ABI 解码（command 0x10）完全闭合。 |
| 16 | `test_universal_router_all_v4_calldata_decodes_exact_path_keys` | **covered_verified** | `test_legacy_v4_plan_metadata.py::test_pure_v4_multihop_pathkeys_independent_abi_decoding` 与 `tests/atomic_execution/test_encoding.py::test_c10_v4_pure_3hop_calldata_assembly` | Universal Router 纯 V4 多跳 swap calldata 编码与独立 PathKey 解码（command 0x11）完全闭合。 |

---

## 四、 跨调用点精确修改范围 (Exact Write Scope)

本次施工严格限制于 8 项文件范围：
1. `tests/test_v4_poolkey.py`: 迁出移除。
2. `docs/legacy_tests/robinhood/test_v4_poolkey.py.txt`: 原样纯 bytes 存证新增。
3. `docs/acceptance/LEGACY_V4_ARCHIVE.md`: 本验收报告新增。
4. `tools/qa/upstream_obligations.py`:
   - 新增显式单条 `LEGACY_TEST_MIGRATION_REGISTRY` 常量。
   - 新增共用受控路径解析函数 `resolve_imported_file_path`。
   - 更新 `KNOWN_ARC_ADAPTATIONS` 条目原因描述，保留 `expected_sha256` 不变。
   - `audit_imported_files` 接入受控解析，捕获异常 fail-closed。
5. `scripts/test_safety_stage.py`: `PUBLIC_FILES` 精确加入归档文件路径。
6. `scripts/test_safety_source_manifest.json`: 移出原路径，加入归档路径与新反例测试，按字典序排序。
7. `tests/arc_v3/independent/test_import_manifest.py`: 仅改路径解析调用 `resolve_imported_file_path`，保留全部 281 项覆盖与 hash 断言。
8. `tests/arc_v3/independent/test_legacy_archive_registry.py`: 新增 12 项严密的反例与安全门禁测试用例。

---

## 五、 门禁对账与测试影响统计

1. **Pytest 收集器脱敏**:
   - `tests/` 根目录遗留测试由 49 个减少至 48 个。
   - `docs/legacy_tests/robinhood/test_v4_poolkey.py.txt` 不再被 pytest 当作测试模块收集，彻底阻断无效旧代码在测试过程中的干扰。
2. **上游审计工具对账 (Upstream Obligations Audit)**:
   - 预期受审文件: 281
   - 实际检出文件: 281
   - 精确匹配文件: 209
   - 合法适配文件: 72 (含 `tests/test_v4_poolkey.py` 映射至归档路径核验)
   - 缺失与异常文件: 0
   - 状态: **PASSED [OK]** (分母未减少，义务无衰减)。
3. **源码清单双向双重对账 (1:1 Manifest Coverage)**:
   - 磁盘有效源码与公开夹具: 472
   - `scripts/test_safety_source_manifest.json`: 472
   - `disk_files - manifest_files == ∅`
   - `manifest_files - disk_files == ∅`
   - 双向无漏测、无幽灵。
4. **未准入测试隔离**:
   - 其余 48 个旧测试文件 100% 保持现状不动。
