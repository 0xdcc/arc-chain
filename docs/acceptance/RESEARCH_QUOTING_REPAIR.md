# Arc-Chain 报价与领域模型合流集成验收报告 (RESEARCH_QUOTING_REPAIR.md)

- **工单编号**: `TASK-Q1-Q2-INTEGRATION`
- **集成角色**: M1 总控与集成 (Master Control & Integration)
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **受审来源**: `/tmp/arc-q-replay-r2-review/workspace`
- **对比源**: Q1 `/tmp/arc-q1-domain/src` & Q2 `/tmp/arc-q-replay-r2`
- **审查准入凭证**: `/tmp/arc-q-replay-r2-review/RESULT.md` (已独立复现 14 测试全通、破坏反证成立)
- **交付结论**: **🟢 INTEGRATION_COMPLETED — 精确装配六文件、源码清单 3 新路径登记、两项旧测试适配哈希对齐、父包与历史数据零覆盖**

---

## 一、 精确装配文件与 SHA-256 现场哈希比对表

依据“以 `/tmp/arc-q-replay-r2-review/workspace` 六正式文件为准，现场计算哈希，排除 Q1 父 init”原则，全量装配文件与哈希校验如下：

| 模块角色 | 目标文件路径 | 前态 SHA-256 / 状态 | 集成后 SHA-256 | 大小 (Bytes) | 审查源校验结果 |
| :--- | :--- | :--- | :--- | :---: | :---: |
| **Q1 领域模型** | `research/market_data/types.py` | `b980b0d1fdb7b68009448a02ad821ee29c482cdbc55e81575386fb1f2463a279` (12,709B) | `3a071c78afeb97be923654b14f7242f98112d590f53d634d4aa53864c5dcb6da` | 27,081 | ✅ 100% MATCH (`3a071c...`) |
| **Q1 契约测试** | `tests/test_domain_contracts.py` | `52bd1ed378e1d9e0ea311b9af9f2241bf345927b7252e61c900eb9f29dae3d5a` (27,059B) | `d7685eefbc3538c160b7b08b41bdb64cc2a0111f56cc33e9fb5055c88d645108` | 27,125 | ✅ 100% MATCH (`d7685...`) |
| **Q2 报价入口** | `research/quoting/__init__.py` | *(不存在)* | `21804c8977b85951e845cdc280cc5123554cb7898863e2f0e73da00714f9c6c3` | 858 | ✅ 100% MATCH |
| **Q2 报价引擎** | `research/quoting/quoter.py` | *(不存在)* | `0eb61101b894c91b7f51eec88b28ace13ad4150d1149bb9fee03cf0877f77431` | 30,538 | ✅ 100% MATCH |
| **Q2 回放适配** | `research/quoting/replay_adapter.py` | *(不存在)* | `245a78572a39b3601f36837b07273f9fc85538968d15448511f7a1e81601b730` | 7,470 | ✅ 100% MATCH |
| **Q2 报价测试** | `tests/test_quoting.py` | `ca4aee58c0fc2a23f5b284a9806f84d38f6f7265362df4c883b3a45c26730deb` (9,056B) | `ae388c091d0396c133d0d6de17b236b0a0c1e1c320c8c10f69a5d1ff01121c4a` | 13,822 | ✅ 100% MATCH (`ae388c...`) |

---

## 二、 受保护资源与父包防覆盖核验 (Zero Drift)

严格遵守安全红线，杜绝跨包污染与历史回放数据漂移：

| 受保护文件路径 | 角色与约束 | 现场 SHA-256 | 状态说明 |
| :--- | :--- | :--- | :--- |
| `research/__init__.py` | 既有根包入口，排除 Q1 父 init | `0d9b4276824f4b26fd76a6b28fbdae40010a45e9fb56edfa1fca471aad023ece` | ✅ 零修改 (UNTOUCHED) |
| `research/market_data/__init__.py` | 既有市场包入口，排除 Q1 父 init | `e36dbdaf50621fbd18d560c2b2879846496d2da1686c7265fc3df79c5de33142` | ✅ 零修改 (UNTOUCHED) |
| `tests/fixtures/opportunities/v1/historical-rpc.jsonl` | 历史回放核心数据 | `c270bb81ae457a211ffe0dc422987678d1c7286ff015f612a1d4672fba9e1239` | ✅ 零修改 (严格一致) |
| `docs/reuse/IMPORT_MANIFEST.json` | 既有 Import 清单 | 保持冻结 (freeze) | ✅ 零修改 (UNTOUCHED) |
| `docs/reuse/CODE_MANIFEST.json` | 既有 Code 清单 | 保持冻结 (freeze) | ✅ 零修改 (UNTOUCHED) |
| CLI / registry2 | 运行入口与注册表 | 保持冻结 (freeze) | ✅ 零修改 (UNTOUCHED) |

---

## 三、 清单与测试义务精准适配

### 1. 源码清单 (`scripts/test_safety_source_manifest.json`)
- **变更原则**: 仅增加 3 项新增 quoting 路径，其他既有路径与顺序保持严格排序与无重复。
- **新增路径**:
  - `research/quoting/__init__.py`
  - `research/quoting/quoter.py`
  - `research/quoting/replay_adapter.py`
- **计数现场解析**: 源码条目从 507 精确增至 510 项（不继承自报，通过 AST/磁盘真实采集验证）。
- **SHA-256**: `4772c66182046e56d2f339226b8aa98adef403d78955eaff7e63cfce20bc5aab`。

### 2. 测试义务映射 (`tools/qa/upstream_obligations.py`)
- **变更原则**: 仅更新两项旧测试适配的 `expected_sha256` 哈希，其余测试义务项零变动。
- **适配项 1**: `tests/test_domain_contracts.py` -> `d7685eefbc3538c160b7b08b41bdb64cc2a0111f56cc33e9fb5055c88d645108`
- **适配项 2**: `tests/test_quoting.py` -> `ae388c091d0396c133d0d6de17b236b0a0c1e1c320c8c10f69a5d1ff01121c4a`
- **SHA-256**: `03eea26814fed85a54ffc6644651e6f552ed5676d224ab9e3d1a4d999f35e311`。

---

## 四、 领域模型与报价契约行为分析

### 1. M1 五模型行为与缓存元数据保持
- `research/market_data/types.py` 完整保留已有 M1 的核心数据模型：
  - `TokenIdentity`: 链标识、地址、精度、符号校验不变；
  - `TokenAmount`: 高精度整型 atom 算术与浮点拦截不变；
  - `PoolIdentity`: 跨链与池唯一性标识验证不变；
  - `PoolStateSnapshot`: 池状态快照与无重构保持；
  - `MarketSnapshot`: 完整包含 `cache_metadata: dict[str, Any]` 字段与只读访问特性。
- 新增只读研究数据结构：
  - `RouteHop`, `CandidateRoute`, `QuoteStatus`, `QuoteResult`
  - `ExecutionPlan`: 纯研究数据容器（包含 `hops`, `expected_in`, `min_out`, `deadline`, `max_gas_cost` 等字段），明确声明**无任何交易执行器与链上交互逻辑**。

### 2. 技术 Imports 与 AST 路径适配
- 原 `tests/test_domain_contracts.py` 中过时的 `arbitrage.domain.types` 导入已统一适配至 `research.market_data.types`；
- 原 `tests/test_quoting.py` 中过时的 `arbitrage.domain.types` / `arbitrage.quoting` 导入已统一适配至 `research.market_data.types` 与 `research.quoting`。

### 3. 测试断言保全与 4 项新增回放测试
- 原 10 项报价测试全部断言完整保留（无削弱、无跳过）；
- 候选源针对回放适配器（`ReplayAdapter`）新增了 4 项针对性反证与边界保护测试：
  1. `test_zero_consumed_verify_complete_rejected`: 验证未消费任何记录即调用 `verify_complete()` 必须严格抛出 `RuntimeError` 拒绝；
  2. `test_empty_fixture_reasonable_semantics`: 空回放用例语义安全，调用即耗尽拒绝；
  3. `test_session_mid_failure_fails_closed_without_masking`: 会话中途故障阻断，fail-closed 保持；
  4. `test_tampered_block_identifier_isolated_rejection`: 孤立篡改块标识反证测试，精确拦截非法块推进。

---

## 五、 门禁静态自检与严格安全执行记录

依据子代理环境指令与 `AGENTS.md` 安全红线：
- **禁止项遵守**: 严格执行零 pytest 运行、零 audit、零业务 import、零 bwrap 沙箱嵌套调用、零网络操作、零资金操作、零 git commit/push。
- **静态 AST 校验**:
  - `research/market_data/types.py`: AST PARSE PASS
  - `tests/test_domain_contracts.py`: AST PARSE PASS (7 测试类, 29 测试函数)
  - `research/quoting/__init__.py`: AST PARSE PASS
  - `research/quoting/quoter.py`: AST PARSE PASS
  - `research/quoting/replay_adapter.py`: AST PARSE PASS
  - `tests/test_quoting.py`: AST PARSE PASS (4 测试类, 14 测试函数)
  - `tools/qa/upstream_obligations.py`: AST PARSE PASS
- **工作区配置 Ruff Lint**:
  - 命令: `/root/projects/crypto/arc-chain/venv/bin/ruff check --config pyproject.toml research/market_data/types.py tests/test_domain_contracts.py research/quoting tests/test_quoting.py`
  - Exit Code: `0` (`All checks passed!`)
- **工作区配置 Mypy 静态类型分析**:
  - 命令: `/root/projects/crypto/arc-chain/venv/bin/python -m mypy --config-file pyproject.toml research/market_data/types.py tests/test_domain_contracts.py research/quoting tests/test_quoting.py`
  - Exit Code: `0` (`Success: no issues found in 6 source files`)
- **集成后全库 Collect 与兼容回归**: 按照多脑协作规范，交由主脑在后续阶段统一调度执行。
