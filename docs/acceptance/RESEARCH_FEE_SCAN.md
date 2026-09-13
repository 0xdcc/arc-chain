# Arc-Chain 市场 M4C 合流集成验收报告：已验费率扫描确权三文件装配与清单测试适配

- **工单编号**: `TASK-M4C-FEE-SCAN-INTEGRATION`
- **执行角色**: M1 总控与集成 (Master Control & Integration) / M4C 合流执行器
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **候选物理来源**: `/tmp/arc-m4c-r3/src`
- **独立审查存证依据**: `/tmp/arc-m4c-r3-review` (`RESULT.md` / `logs/bwrap_candidate_combined_test.log` (40 passed) / `logs/bwrap_r2_control_red.log` (7 failed))
- **安全与合规基线**: 严格遵循 `AGENTS.md` (v3.1)；只读离线研究范围，零真实资金、零网络请求、零合约部署、零远端 push；冻结 IMPORT/CODE、registry 两项、父 `__init__`、CLI 入口与原其他模块不动；禁止运行 pytest/audit/业务 import/bwrap；不提交代码、不清 dirty。

---

## 一、 候选受试快照与装配哈希核验表 (100% 精确装配，不重构)

装配前严格对账候选源、审查存证及工作区基线，哈希比对结果如下：

| 序号 | 模块角色 | 目标文件路径 | 装配属性 | 前态 SHA-256 (Baseline) | 集成后 SHA-256 | 大小 (Bytes) | 哈希比对状态 |
|:---:|:---|:---|:---:|:---|:---|:---:|:---:|
| 1 | **生产核心** | `research/market_data/fee_scan.py` | 新增 | *(不存在)* | `703b506ec4647b0a68cdbdbf5a002dcce7381b0518e63d93f84d9365cb0325e8` | 22,660 | ✅ 100% MATCH |
| 2 | **原测试适配** | `tests/test_pool_fee_verification.py` | 适配 | `6e6751194927f6c5c6c000c9af2558868f3113187b787717b935128ffcc1e8d5` | `077e654142798a948b343e0da929afd2911a4b1266a37ff1fa864b83f560c72b` | 12,712 | ✅ 100% MATCH |
| 3 | **独立契约测试** | `tests/arc_v3/independent/test_fee_scan_contract.py` | 新增 | *(不存在)* | `97b557377b3e41d1ece2f35eb2b8cdfa58b1244d47540e03549ac90ec6bda28e` | 29,266 | ✅ 100% MATCH |
| 4 | **安全源清单** | `scripts/test_safety_source_manifest.json` | 登记 | `505` 项 (唯一排序) | `507` 项 (仅新增 2 路径，唯一排序) | 21,349 | ✅ 仅增 2 项 |
| 5 | **上游测试义务** | `tools/qa/upstream_obligations.py` | 适配 | `6e6751194927f6c5c6c000c9af2558868f3113187b787717b935128ffcc1e8d5` | `077e654142798a948b343e0da929afd2911a4b1266a37ff1fa864b83f560c72b` | 52,238 | ✅ 仅适配 Hash |
| 6 | **集成验收报告** | `docs/acceptance/RESEARCH_FEE_SCAN.md` | 新建 | *(不存在)* | *(本报告)* | - | ✅ 新建归档 |

- **基线核验**: 工作区原 `tests/test_pool_fee_verification.py` 前态哈希为 `6e6751194927f6c5c6c000c9af2558868f3113187b787717b935128ffcc1e8d5`，与预期基线完全吻合，未发生非受控漂移。
- **源码装配原则**: 严格按原始候选纯 bytes 装配，未作任何重新编写或代码重构。

---

## 二、 语义契约、测试义务与技术适配声明 (拒绝零缺陷绝对话术)

### 2.1 原测试义务保全与断言统计
- **断言保全**: 原测试文件 `tests/test_pool_fee_verification.py` AST 遍历统计：原基线包含 **33 个 assert**，适配后同样包含 **33 个 assert**，原断言 100% 完整保留，未删减任何断言逻辑。
- **离线技术适配性质如实声明 (不声称线上真实费率)**:
  1. **Fixture 单位 100 -> 10000 ppm 修正**:
     - 在 `test_giga_and_up_v3_real_scenario_verification` 中，原用例传入 `mock_rpc(v3_fee_return=100)`，实际编码为 uint24 `100 ppm` (= 1.0 bps)，与池名 "0.01%" (= 1.0 bps) 完全相等，无法触发 `[FEE_MISMATCH]` 且与 `fee_bps == 100.0` 断言产生数值矛盾（旧未除以 100 时的历史遗留）；
     - 施工调整为 `10000 ppm` (对应 `100.0 bps`)，此项修改如实定性为**离线 Mock 夹具单位的技术适配**，绝不声称或等同于真实线上链上费率。
  2. **Fixture 身份字段补全**:
     - 在 `test_v4_pool_reads_fee_from_manifest_or_stateview` 中，为候选池输入补充了 `chain_id: 5042`、`token0_address`、`token1_address`；
     - 如实定性为满足接缝强身份比对所必需的**离线输入数据补全**，未构造链上伪 RPC 模拟，不代表线上真实链验证。

### 2.2 核心安全与物理边界声明
1. **元数据校验物理边界**:
   - `fee_scan.py` 中的元数据比对仅证明**输入接缝各字段的身份一致性**（PoolId、Chain ID、Token 集合），**绝非可信签名或真实链上验证**；
2. **只读注入与单点 latest 物理边界**:
   - Fallback 严格通过显式依赖注入的只读 reader (`v4_reader` / `v3_reader`)；
   - 对各池执行单点 `latest` 调用，仅反映顺序单次调用瞬间的最新块高，**不具备跨池原子性与区块一致性**，严禁误称为跨池状态快照 (snapshot)；
3. **0.01 bps 双表示一致性性质声明**:
   - `abs(fee_val_bps - direct_bps) > 1e-4` (0.01 bps / 1 ppm) 仅在同一字典元数据中同时存在 `fee` (ppm) 与 `fee_bps` 两种表示时核验内部表示一致性；
   - **绝不是新的交易经济 gate**，未擅自扩充交易规则或准入门槛。

---

## 三、 清单登记与最小修改范围核验

### 3.1 `scripts/test_safety_source_manifest.json` 登记
- **变更统计**:
  - 原实际文件计数: **505 项**
  - 新增文件路径 (严格仅 2 项):
    1. `research/market_data/fee_scan.py`
    2. `tests/arc_v3/independent/test_fee_scan_contract.py`
    *(注: `tests/test_pool_fee_verification.py` 原已存在于清单中)*
  - 变更后实际文件计数: **507 项**
- **合规核验**: 保持全列表严格字典序排序 (sorted) 且无重复项 (unique)。

### 3.2 `tools/qa/upstream_obligations.py` 适配
- **变更统计**:
  - 仅将 `KNOWN_ARC_ADAPTATIONS["tests/test_pool_fee_verification.py"]["expected_sha256"]` 由 `6e6751194927f6c5c6c000c9af2558868f3113187b787717b935128ffcc1e8d5` 更新为适配后的候选哈希 `077e654142798a948b343e0da929afd2911a4b1266a37ff1fa864b83f560c72b`；
- **边界冻结守则**:
  - `ALLOWED_POOL_CATALOG_PATHS` 与 `LEGACY_TEST_MIGRATION_REGISTRY` 两大 registry 保持零改动；
  - 冻结 IMPORT / CODE 边界不碰；
  - 父级 `__init__.py`、CLI 入口以及其它模块代码完全不动。

---

## 四、 本地静态检查执行结果与 Exit Code (显式工作区配置)

根据规范，仅执行静态 AST、Ruff 与 Mypy 检查，使用工作区显式配置 `pyproject.toml`，禁止动态 pytest/audit/业务 import/bwrap：

| 检查项 | 目标文件 | 采用配置 | 实测 Exit Code | 输出结论摘要 |
|:---|:---|:---|:---:|:---|
| **AST 语法解析** | `research/market_data/fee_scan.py` | Python 3.12 `ast.parse` | **0** | OK (24 root nodes) |
| **AST 语法解析** | `tests/test_pool_fee_verification.py` | Python 3.12 `ast.parse` | **0** | OK (13 root nodes) |
| **AST 语法解析** | `tests/arc_v3/independent/test_fee_scan_contract.py` | Python 3.12 `ast.parse` | **0** | OK (16 root nodes) |
| **AST 语法解析** | `tools/qa/upstream_obligations.py` | Python 3.12 `ast.parse` | **0** | OK (29 root nodes) |
| **Ruff 静态检查** | `research/market_data/fee_scan.py` | `--config pyproject.toml` | **0** | `All checks passed!` |
| **Ruff 静态检查** | `tools/qa/upstream_obligations.py` | `--config pyproject.toml` | **0** | `All checks passed!` |
| **Ruff 静态检查** | 2 个测试文件 (test_pool_fee & test_fee_scan) | `--config pyproject.toml` | **1** | 2 处 `I001` (isort 未分块/排序，严格按候选源码精确装配保持不变，如实记录不粉饰) |
| **Mypy 类型检查** | 全部 3 个装配源文件 | `--config-file pyproject.toml` | **0** | `Success: no issues found in 3 source files` |

---

## 五、 状态汇报与主脑验收交接

1. **状态交接**:
   - 本工单已完成受试三文件的精确原样装配、安全清单的唯一排序精确扩充 (505 -> 507)、上游义务哈希适配；
   - 静态 AST 与 Mypy 全部 Exit Code 0 通过，生产文件 Ruff Exit Code 0 通过；
   - 动态 pytest 联合执行 (40 项 pass) 与旧 R2 弱校验证伪反证 (7 项 failed) 前序已于独立沙箱存证 (`/tmp/arc-m4c-r3-review/RESULT.md`)；
   - 按照规范，动态回归与全库 collect 留由主脑独立验收；
2. **工作区状态**:
   - 不发起 git commit；
   - 不清理既有 dirty 文件；
   - 本阶段合流交付物就绪。
