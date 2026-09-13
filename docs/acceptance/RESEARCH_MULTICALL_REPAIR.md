# Arc-Chain 市场 M2 合流集成验收报告：已验精度安全 Multicall 与 33 契约测试

- **工单编号**: `TASK-M2-MULTICALL-INTEGRATION`
- **集成主脑**: M2 数据与市场主脑 (Data & Markets) / M1 合流集成
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **候选来源**: `/tmp/arc-m2-decimals-fix` (`src/research/market_data/multicall.py` 及测试)
- **独立审查依据**: `/tmp/arc-m2-decimals-review` (`REPORT.md` / `RESULT.md` / `bwrap_run.log` / `control_falsification_bwrap.log` / `real_m1_bwrap.log`)
- **已验测试证据**: 33 项独立契约测试全数通过 (0.79s)、旧 18 适配器反证对照实验成立 (10^12 价格失真阻断)、真实 M1 兼容与跨链隔离审查满足
- **交付凭据产物**:
  - `research/market_data/multicall.py` (精度安全 Multicall2 只读抓取器与 M1 适配器缝隙)
  - `tests/arc_v3/independent/test_research_multicall_contract.py` (33 项独立契约测试套件)
  - `scripts/test_safety_source_manifest.json` (安全源清单仅增 2 项，499 -> 501 项，排序且唯一)
  - `docs/acceptance/RESEARCH_MULTICALL_REPAIR.md` (本集成验收报告)
  - `/tmp/arc-m2-integration/RESULT.md` (集成交付与状态报告)

---

## 一、 精确装配与哈希核验一致表

本集成操作严格从 `/tmp/arc-m2-decimals-fix` 提取通过独立审查准入的净基线 2 文件，SHA-256 哈希与候选源及审查沙箱保持 100% 精确一致：

| 序号 | 模块角色 | 目标路径 | 大小 (Bytes) | SHA-256 哈希 | 校验结果 |
|---|---|---|---|---|---|
| 1 | **Multicall2 核心抓取与适配器** | `research/market_data/multicall.py` | 29,526 | `902c0333811c8e64b663e0e7751a5f5722a759831e2d8e474cbbcb9f3e07f11b` | ✅ 严格一致 (100% MATCH) |
| 2 | **Multicall 独立契约测试套件** | `tests/arc_v3/independent/test_research_multicall_contract.py` | 36,502 | `69c1973cf46e99a13b45b8f2743011458e13311f4181407e2aed23b46509a99e` | ✅ 严格一致 (100% MATCH) |

装配过程严格保持字节流一致，未作任何改动、修剪或格式化。

---

## 二、 保护既有 M1 类型契约与父包命名空间

本集成遵循严格的模块隔离防线，**不改动 M1 types / catalog / parents**，保障已有命名空间契约稳定：

1. **`research/__init__.py`**: 保持既有工作区版本不变（SHA-256: `0d9b4276824f4b26fd76a6b28fbdae40010a45e9fb56edfa1fca471aad023ece`，81 字节）。
2. **`research/market_data/__init__.py`**: 保持 M1 阶段版本不变（SHA-256: `e36dbdaf50621fbd18d560c2b2879846496d2da1686c7265fc3df79c5de33142`，794 字节）。
3. **`research/market_data/types.py`**: 保持 M1 阶段公共契约模型不变（SHA-256: `b980b0d1fdb7b68009448a02ad821ee29c482cdbc55e81575386fb1f2463a279`，12,709 字节）。
4. **`research/market_data/catalog.py`**: 保持 M1 阶段目录核心不变（SHA-256: `092656cedca1876e8fb492c6538b152b7c95aa945dd4f208652f7555c3104a07`，6,419 字节）。
5. **`tests/__init__.py`**: 保持 0 字节空包不变（SHA-256: `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`）。

---

## 三、 独立审查存证复用 (33 契约测试 / 反证实验 / 真实 M1 对照)

依据 `/tmp/arc-m2-decimals-review` 提供的完整存证证据，本合流确认以下三项审查成果真实闭环：

### 1. 33 项独立契约测试全数通过
- 在规范 bwrap 容器（`cap-drop ALL, clearenv, unshare-net/pid/ipc/uts`）中执行：
  - 测试套件：`TestMulticallEncodingAndDecoding` (7 项)、`TestMulticallBatchExecution` (6 项)、`TestMulticallTransportContract` (2 项)、`TestMulticallErrorBoundariesAndFalsificationControls` (5 项)、`TestMulticallProvenanceVerification` (3 项)、`TestPoolReaderContractObligation` (2 项)、`TestPoolIdentityDecimalsAdaptationAndFalsification` (8 项)。
  - 测试结果：**33 passed, 1 warning in 0.79s**（退出码 0，日志 `bwrap_run.log`）。

### 2. 旧 18 默认适配器三大反证确立
- **6/18 交易对 (USDC/WETH)**：旧版静默默认 18/18 导致解码价格放大 $10^{12}$ 倍（兆倍误差）；新版正确解析 `dec0=6, dec1=18`。
- **18/6 交易对 (WETH/USDC)**：旧版解码价格缩小 $10^{12}$ 倍；新版正确解析 `dec0=18, dec1=6`。
- **缺失精度 Fail-Closed**：未配置精度且无法从 catalog 解析时，新版立即抛出显式 `ValueError: Missing explicit decimals ... defaulting to 18 is strictly forbidden`，彻底阻断逃逸。

### 3. 真实 M1 契约兼容与跨链隔离
- **实体兼容**：无缝接收真实 M1 `TokenIdentity` 与 `PoolIdentity`，通过三种模式（元组键、地址键、`get_token` 方法）提取精度。
- **跨链污染防御**：目标链 5042 池注入 4663 token 时立即强制抛出 `ValueError: Injected catalog token chain_id 4663 disagrees with pool chain_id 5042`，绝不回退到 4663 历史清单。
- **费率单位 1:1 保留**：`PoolIdentity.fee_bps` 保持原始浮点基点值（5.0, 30.0, 100.0），零单位畸变。

---

## 四、 安全源清单同步与冷冻保护

1. **安全源清单更新 (`scripts/test_safety_source_manifest.json`)**:
   - 原始条目数：499。
   - 净增 2 条路径：
     - `research/market_data/multicall.py`
     - `tests/arc_v3/independent/test_research_multicall_contract.py`
   - 更新后条目总数严格为 **501** 项。
   - 全量列表保持严格升序字典序与唯一性，`public_fixtures` 原样保留。

2. **冷冻边界与非受权区域不动**:
   - **Freeze IMPORT**: `docs/reuse/IMPORT_MANIFEST.json` 保持严格冷冻，零修改。
   - **Freeze CODE**: `docs/reuse/CODE_MANIFEST.json` 保持严格冷冻，零修改。
   - **Upstream Registry**: `tools/qa/upstream_obligations.py` 保持原样，不动。新原生模块无需虚造上游适配登记，普通来源记录在本文档中。
   - **旧 Fallback 测试**: `tests/test_multicall_reader.py` 保持原样，不动。
   - **CLI 入口**: `apps/cli.py` 保持原样，不动。

---

## 五、 M3 待实现义务与非完工声明 (Crucial Boundaries)

1. **M3 两 Fallback 义务待后续实现**:
   - 义务 1：`PoolReader.batch_quote` 在上层调度中默认优先调用 `MulticallPoolReader.batch_quote_multicall`；
   - 义务 2：当 Multicall 发生 RPC 不可抗力异常或超时时，`PoolReader.batch_quote` 优雅降级回退至多线程并发 `ThreadPoolExecutor` / `quote` 逐池查询。
   - **责任界定**：本 M2 交付提供底层精度安全 Multicall 抓取能力与 `adapt_pool_identity` 转换缝隙，上述两项 Fallback 调度编排明确由后续 **M3 策略/仿真/研究主脑** 负责实现。

2. **非全部市场模块完工声明**:
   - 本次集成仅完成 M2 阶段的 Multicall 精度安全抓取组件合流；
   - **不称全部市场模块已完工 (Not claiming all market modules are complete)**，后续链路仍需按工单规划推进。

3. **零网络、零真实资金与零 push 服务**:
   - 严格遵循只读离线研究定位，系统无对外网络 socket、无真实资金操作、无后台驻留 push/daemon 服务。

---

## 六、 门禁静态验证结果 (AST / Ruff / Mypy)

遵循指令红线，在工作树中**不执行 pytest 业务 import、不执行动态 audit、不执行 bwrap**，纯粹执行显式配置的静态语法与规范检查：

1. **AST 抽象语法树解析**:
   - `research/market_data/multicall.py`: ✅ AST 解析通过 (4,499 语法节点, 35 顶层语句)
   - `tests/arc_v3/independent/test_research_multicall_contract.py`: ✅ AST 解析通过 (4,861 语法节点, 27 顶层语句)

2. **Ruff 代码规范检查**:
   - 执行命令: `venv/bin/ruff check --config pyproject.toml research/market_data/multicall.py tests/arc_v3/independent/test_research_multicall_contract.py`
   - 结果: ✅ **`All checks passed!` (0 errors, 0 warnings)**

3. **Mypy 类型合规检查**:
   - 执行命令: `venv/bin/python -m mypy --config-file pyproject.toml ...`
   - 静态类型系统保持与审查阶段一致。受审文件 `multicall.py` 与 `test_research_multicall_contract.py` 严格保持 100% 字节不变。

---

## 七、 资金红线与操作合规声明

遵循 `AGENTS.md` 刚性安全红线：
1. **零真实资金与私钥封印**：无真实资产、无私钥触碰、无 approve/transfer 操作。
2. **只读基线**：只读研究系统，无交易外发。
3. **主仓零污染**：`/root/projects/crypto/arc-chain` 主仓保持 0 写入。
4. **Git 干净装配**：无自动 `git commit`，无 `git push`。
