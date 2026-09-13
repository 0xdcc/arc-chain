# V4 Keccak-256 算法修复与十项义务迁移验收报告

- **工单编号**: TASK-V4-KECCAK-INTEGRATE (v1)
- **集成主脑**: M1 总控与集成 (Master Control & Integration)
- **工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **准入源**: `/tmp/arc-v4-keccak-fix-r1/` 与独立审查 `/tmp/arc-v4-keccak-final-review/`
- **执行方式**: Codex 本地静态施工 (无网络、无真实资金、无RPC调用、无合约部署、禁止 commit/push/服务重启)
- **交付产物**:
  - `docs/acceptance/V4_KECCAK_REPAIR.md` (本验收报告)
  - `/tmp/arc-v4-keccak-integration/RESULT.md` (交付审查凭据)

---

## 1. 修复背景与技术实现

### 1.1 根因与 EVM 规范对齐
- **原伪哈希缺陷**: 原 `arc_markets/v4_events.py` 中 `compute_pool_id()` 采用 Python 标准库 `hashlib.sha3_256` 进行哈希计算。由于 NIST SHA-3 与以太坊 Keccak-256 在填充常数（Padding: 0x06 vs 0x01）上存在数学差异，导致生成的 `pool_id` 与以太坊链上 Uniswap V4 真实合约计算结果完全不符。
- **规范 5-Tuple 编码**: 切换至 `eth_abi.abi.encode`，按 Solidity `struct PoolKey` 契约严格使用 `["address", "address", "uint24", "int24", "address"]` 编码。
- **Keccak-256 哈希**: 引入 `eth_utils.crypto.keccak`，生成标准以太坊 `keccak256(abi.encode(...))`。
- **Fail-Closed 刚性门禁**: 缺少 `eth_abi` 或 `eth_utils` 依赖时直接抛出显式 `RuntimeError`，杜绝任何降级 fallback 到 SHA-3 的静默错误；`V4PoolKey` 维持 `currency0 < currency1` 逆序拦截与地址格式校验。

---

## 2. 精确写范围与文件哈希核验单 (SHA-256)

本次集成严格限制于 5 个文件，原候选 worktree 及主仓保持不动：

| 序号 | 文件路径 | 操作类型 | 目标 SHA-256 | 落盘一致性 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `arc_markets/v4_events.py` | MODIFIED | `e41618eaa868ea1e789fd41f7a5bed609df9dcae7dd1b3945f55cd5575cb2aad` | **MATCH (100%)** |
| 2 | `tests/arc_v3/markets/test_v4_identity.py` | MODIFIED | `27f2d74afa7ebe020b9e9cb179b172eb5b87c75cc062f0311f4a2741d3105992` | **MATCH (100%)** |
| 3 | `tests/arc_v3/independent/test_legacy_v4_obligations.py` | CREATED | `6bf8bca18c42e9679db80aed410ef6ff51646b65d0bba73115463be4e2d9db8e` | **MATCH (100%)** |
| 4 | `scripts/test_safety_source_manifest.json` | MODIFIED | *(增补新测试路径，保持结构与排序)* | **MATCH (100%)** |
| 5 | `docs/acceptance/V4_KECCAK_REPAIR.md` | CREATED | *(本文件)* | **MATCH (100%)** |

### 冻结清单 Membership 审查
- 检索 `docs/reuse/IMPORT_MANIFEST.json`：上述 3 个源码/测试文件均不在冻结清单中，确为 Arc 原生文件。
- `docs/reuse/IMPORT_MANIFEST.json` 保持原样，零修改。

---

## 3. 独立审查证据与动态日志存证引用

依据独立审查报告 `/tmp/arc-v4-keccak-final-review/RESULT.md` 及其归档沙箱执行日志：

### 3.1 红方反证 (01_sha3_control_red.log)
- **环境**: `/tmp/arc-v4-keccak-sha3-control` (保留 SHA3-256 原逻辑)
- **结果**: **FAILED (3 failed, 7 passed in 0.20s)**
- **证据点**:
  - `test_six_canonical_pool_vectors_exact_keccak_derivation`: SHA3 产生 `0x3f7b8788...`，与规范 Keccak `0x7aebd805...` 不符，精确拦截。
  - `test_nist_sha3_mathematical_divergence_proof`: 数学反证断言命中，确认 SHA3 错误实现。
  - `test_address_casing_invariance`: 捕获大小写与算法不匹配。

### 3.2 绿方验证 (02_remediated_obligations_green.log)
- **环境**: `/tmp/arc-v4-keccak-final-review/src` (EVM Keccak-256 修复版)
- **结果**: **PASSED (10 passed in 0.08s)**
- **通过的十项义务测试**:
  1. `test_six_canonical_pool_vectors_exact_keccak_derivation` (AI/USDG 0.23%, PONS/USDG 0.3%, PONS/USDG 0.7%, SPY/USDG 0.3%, CASHCAT/USDG 0.269%, WETH/USDG 0.01% 全部 100% 吻合)
  2. `test_nist_sha3_mathematical_divergence_proof` (数学证明 Keccak != SHA3 且修复版本绝不产生 SHA3 输出)
  3. `test_field_mutation_sensitivity` (5-tuple 任一字段变动哈希立即雪崩变化)
  4. `test_address_casing_invariance` (地址大小写输入产出完全一致 canonical poolId)
  5. `test_strict_currency_order_fail_closed` (币对逆序 fail-closed 拦截)
  6. `test_parameter_bounds_enforcement` (fee/tickSpacing 越界校验)
  7. `test_dynamic_fee_and_missing_hooks_spec` (PoolSpec 默认标志位正确)
  8. `test_get_v4_pool_case_insensitive_lookup` (池检索大小写无关)
  9. `test_process_initialize_preserves_custom_tick_and_hooks` (初始化事件保留自定义 hooks/tick)
  10. `test_process_initialize_unauthorized_manager_rejected` (未授权 manager 拒绝)

### 3.3 领域与基线回归 (03_v4_identity_green.log / 05_arc_v3_tests.log)
- `tests/arc_v3/markets/test_v4_identity.py`: **6 passed in 0.04s** (`03_v4_identity_green.log`)
- `tests/arc_v3/` 全套测试: **386 passed in 10.92s** (`05_arc_v3_tests.log`)

---

## 4. 旧测试审计与归档封禁判定

### 4.1 原 16 项旧测试义务覆盖对账
根据 `/tmp/arc-v4-keccak-final-review/mapping.json`，原 `tests/test_v4_poolkey.py` 的 16 个函数分类如下：
- `covered_by_new_test` (9项): 6组官方向量、大小写无关、自定义参数保留、动态费率等已完整移植至新测试。
- `covered_by_existing` (2项): Universal Router calldata 解码由 `tests/arc_v3/simulation/` 承接。
- `superseded_by_architecture` (1项): 逆序自动翻转被更严格的 fail-closed 机制取代。
- `chain_specific_unhandled` (2项): Robinhood 专用硬编码静态清单，Arc 采用动态事件发现。
- `not_covered` (2项):
  1. `test_scanned_to_pool_spec_matches_v4_metadata` (扫描池到定价模型转换)
  2. `test_plan_from_triangular_alert_populates_v4_leg_metadata` (三角套利告警腿 V4 元数据填充)

### 4.2 归档封禁决议
- **决议**: **禁止归档 (Archive Prohibited, `archive_permitted: false`)**。
- **处置**: 原 `tests/test_v4_poolkey.py` **保留在原位且不作任何删除或归档**，待后续工单针对未覆盖的 2 项转换逻辑完成迁移闭环后，方可启动归档流程。已批准迁移不等于放弃未覆盖义务。

---

## 5. 全库现状与待迁移说明

- **未执行全库 pytest**: 严格遵守静态施工指令，未执行全库 pytest。
- **已知未迁移错误**: 全库收集时根目录下上游遗留测试包含 49 个错误（如 `test_robinhood.py`, `test_tax_guard.py` 等），属于上游 Robinhood 遗留代码，等待后续独立工单迁移处理，严禁虚报“全库 1000+ PASS”。
- **安全边界**: 零真实资金、零网络、零部署、无 git commit/push 操作，纯本地静态合规。
