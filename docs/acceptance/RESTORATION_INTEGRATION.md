# RESTORATION_INTEGRATION 恢复成果装配与验收对账报告 (M1 Delivery)

- **工单单号**: TASK-RESTORATION-INTEGRATE / v1
- **执行角色**: M1 总控与集成 (Master Control & Integration)
- **集成目标工作树**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **基线 Commit**: `02bb55b1525f4ce57ba6ae88f78d83310cc9fc20`
- **受审源快照**: `/tmp/arc-restoration-fix-r2/src` (纯净 666 文件受管源码树，零构建缓存)
- **审查与复验依据**: `/tmp/arc-restoration-final-review/REVIEW.md` 及关联运行日志
- **红线规约**: 零真实资金、零外部网络/RPC调用、零合约部署、禁止 push / commit / 服务重启。主仓 `/root/projects/crypto/arc-chain` 保持干净且零写入；目标工作树确认承接既有已审 dirty 状态，严禁 reset。

---

## 一、 治理规范与准入范围对账

根据 `AGENTS.md` (v3.1) 与 `TASK-RESTORATION-INTEGRATE.md`：
1. **工作树状态**: 目标集成树此前已包含前序经审工单修改，确认处于正常 dirty 状态（含已移除的 `.github/workflows/arc-audit.yml` 及 Runtime/Audit 修订），严禁宣称“工作树完全 clean”，严禁执行 `git reset`。
2. **主仓只读**: 主仓库 `/root/projects/crypto/arc-chain` 处于 `main` 分支，工作区完全 clean，零文件被修改。
3. **无活跃冲突写入者**: `.coord` 协调器无活跃写租约，后台无并发冲突进程。
4. **受控纯静态装配**: 本工单执行纯静态哈希比对与文件装配，不运行内部 bwrap，不降低沙箱隔离级别，不重复执行耗时受限业务测试。
5. **冻结契约完整性**: `docs/reuse/IMPORT_MANIFEST.json` 与 `AGENTS.md` 保持完全未修改（0 差异）。

---

## 二、 动态验收已过实测证据 (Verified Evidence)

以下动态测试与离线门禁已由 Hermes 宿主在 Bubblewrap 物理隔离环境中全量运行并验证通过，关键日志均已落盘存证：

| 校验套件 / 离线门禁 | 测定结果 | 耗时 / 状态码 | 引用存证日志路径 |
| :--- | :--- | :--- | :--- |
| **Manifest 契约覆盖度** | **5 passed** | 0.23s / Exit 0 | `/tmp/arc-restoration-final-review/logs/manifest_5_tests.log` |
| **RWA 套件 (含真实子进程 CLI)** | **44 passed** | 0.71s / Exit 0 | `/tmp/arc-restoration-final-review/logs/rwa_44_tests.log` |
| **Arc V3 核心测试套件** | **375 passed, 1 warning** | 10.76s / Exit 0 | `/tmp/arc-restoration-final-review/logs/arc_375_tests.log` |
| **W5 离线门禁基线检查** | **ALL CHECKS PASSED** | Exit 0 | `/tmp/arc-restoration-final-review/logs/w5_offline_check.log` |
| **W7 离线门禁基线检查** | **ALL CHECKS PASSED** | Exit 0 | `/tmp/arc-restoration-final-review/logs/w7_offline_check.log` |
| **W3 离线门禁基线检查** | **ALL CHECKS PASSED** | Exit 0 | `/tmp/arc-restoration-host-validation/logs/09_w3_offline_check.log` |

### 物理变异破坏实验 (Sabotage Mutations)

三套独立物理变异实验均严格复现 `0 -> !=0 -> 0`（基线通过 -> 注入破坏报错 -> 恢复源码后重新通过）：
1. **W3 Sabotage (5/5)**: 5 项物理破坏实验全量捕获，验证 exit 0 (`/tmp/arc-restoration-host-validation/logs/10_w3_sabotage.log`)；
2. **W5 Sabotage (6/6)**: 6 项物理破坏实验全量捕获，耗时 27.59s，验证 exit 0 (`/tmp/arc-restoration-final-review/logs/w5_sabotage.log`)；
3. **W7 Sabotage (5/5)**: 5 项物理破坏实验全量捕获，验证 exit 0 (`/tmp/arc-restoration-host-validation/logs/08_w7_sabotage.log`)。

---

## 三、 装配差异清单与哈希对账 (Assembly Parity)

受审源码快照 `/tmp/arc-restoration-fix-r2/src` 共包含 **666** 个受管文件。与目标工作树比对结果如下：
- **一致文件**: 639 个文件装配前哈希已与受审快照完全一致；
- **增量修改文件**: 10 个已审文件完成覆盖装配；
- **新增模块文件**: 17 个文件完成写入装配；
- **批准删除文件**: 1 个 `.github/workflows/arc-audit.yml`（已确认删除，源缺失不自动连带删除其他目标专有文件）；
- **装配后逐文件对账**: 目标工作树中对应 666 个受管文件 SHA-256 与 `/tmp/arc-restoration-fix-r2/src` 达成 **666/666 (100%)** 精确一致。

### 1. 10 个修改装配文件对账

| 文件路径 | 装配前目标 SHA-256 | 装配后目标 SHA-256 (等于src) | 变更说明 |
| :--- | :--- | :--- | :--- |
| `apps/cli.py` | `f44a778d427b62b1...` | `055f8387b58164e601173d6fcf5cad3db97f5112cb647af6609a3dfa429c851e` | 规范导入排序 (I001) |
| `atomic_execution/arc_planning.py` | `369d41d79bcf4a6b...` | `dfd31d0072a8888d7d3d901aa8c65a90d81377c51d50ef51be216113581f773d` | 移除未使用的 Any |
| `atomic_execution/deployments.py` | `2abf496162161a27...` | `bcd23245d20c67cdce9fb327fd32cfcac7f13d1662923c0b0dc5c0d3e79c0f7a` | 移除未使用的异常类 |
| `scripts/test_safety_source_manifest.json` | `00b2c12b48b7b13d...` | `7569949eba34e2bb47d2e18fa4a9caa1fe2e2f0214188820e608b8517311b074` | 增补已审恢复源码 |
| `scripts/test_safety_stage.py` | `7a00f5c3a2fec1e3...` | `f02b2d77db4f6b4f171ca16c2b5d45ef2d36d1cd3ed2098c2eef30ae7b7eb9ed` | 同步 PUBLIC_FILES/SOURCE_DIRS |
| `tests/contracts/test_manifest_coverage.py` | `74d8389f27569e65...` | `5d813228e42503905eba49c17946807325cb3a3173eea847e6b1338b4def8609` | 优先收集 docs/reuse 与精确探针 |
| `tests/rwa/test_cli_e2e.py` | `7f03a09ab5658a41...` | `17bd280492f80f38e10d34bb4256f433ddc13c5e4336ea9150f82d97652c4602` | 隔离环境下绝对路径与 PYTHONPATH |
| `tools/checks/run_layers.py` | `bfb85ad9e7299627...` | `3c62da9b3279a53bd1013dde8fd4a8a0986fba1e4592293b812ec9f071fb4704` | 导入单行拆分规范 |
| `tools/coord/reduce_outbox.py` | `2c21cfbda55ddd6f...` | `6caceea583e3c9722b34b18c4869c63544944ce442a3cac050f32d8458f97260` | 撤销未授权 epoch 比较，清理无用导入 |
| `tools/qa/upstream_obligations.py` | `87c1e881bb5167b9...` | `cddbf93081c67557b6c1dd7ecb5037b8b18f4f12ae42052e5723bb348a788e15` | 登记 test_manifest 与 rwa 适配哈希 |

### 2. 17 个新增装配文件对账

| 文件路径 | 装配后目标 SHA-256 | 分类 |
| :--- | :--- | :--- |
| `docs/w3/manifest-submission.json` | `ba35927203b27008fe4a5749237b159e041667430e2b65e07608e7cfebf1bf5c` | W3 门禁清单 |
| `docs/w5/manifest-submission.json` | `a10893e7dbb97ff630d783d2fb8dfd24a12dd13d87285f027f737f9c70f58fa1` | W5 门禁清单 |
| `research/__init__.py` | `0d9b4276824f4b26fd76a6b28fbdae40010a45e9fb56edfa1fca471aad023ece` | Research 包初始化 |
| `rwa_research/__init__.py` | `01ab3ebdcf72e9b121da00280f7810bc25d091c7264db71455e6bfdbe20687c4` | RWA 研究包初始化 |
| `rwa_research/classify.py` | `ae83a31d90d72f4940277a93862c6e3320a9b4fcbaca3fc2ac9a3454f0d6ad48` | RWA 资产分类器 |
| `rwa_research/codec.py` | `64ce7c131a30fe96c749404cb9acef7a7c918c98f95350d1adb118cf36e95d47` | RWA 编解码引擎 |
| `rwa_research/models.py` | `0b9bb2f533da09d1ccacc0183afc487e518be9bf564f61a63530dcc53f5d963c` | RWA 数据契约模型 |
| `rwa_research/normalize.py` | `148ce8871929bf6371aebbddb6bac7eb7e93c6fdaa55a5ba90914b3662e076bc` | RWA 归一化处理 |
| `rwa_research/quote_inputs.py` | `ca6abcad72c9f6c7e7c3db2c1e7ec8827febc5b9ed622e2fb1d6ccd50a97c24c` | RWA 报价输入抽取 |
| `rwa_research/report.py` | `7195a96773f9755c12d3a6e77bea87384bfb7b85109536e5504cb5c8e8e392c3` | RWA 分析报告生成 |
| `rwa_research/validity.py` | `64149c9065643ebe1f78ae6239f6ccea9d23c43cd2a2e67653628e1d69bf84cc` | RWA 有效性验证规则 |
| `tools/w3_offline_check.py` | `0c93100782d93c19a5f6cae73df4864fd20ff17bb82c58a460d7c2324834fd51` | W3 离线门禁核验工具 |
| `tools/w3_sabotage.py` | `02c9f0ea2243025dec115c7792ece01f6e6b5560b83995ef499e8811c9202d1c` | W3 物理变异破坏工具 |
| `tools/w5_offline_check.py` | `ea38a2fa7251d6625e2f10e0cbd08a4e73bb5801fef5f821f3ee22b3f31fd278` | W5 离线门禁核验工具 |
| `tools/w5_sabotage.py` | `6e0272c85f7244228b87f523f500842a30e14082937b97bec942f8d50bcb20f9` | W5 物理变异破坏工具 |
| `tools/w7_offline_check.py` | `e245f176e7918ed24b124b8f6a3db4d8bee43c963543e35d11241f7918e3eac0` | W7 离线门禁核验工具 |
| `tools/w7_sabotage.py` | `0bb85b31494b017d1be60ee89a7c4dcc4be3a68548a2fbe02f631d4c7ec13e63` | W7 物理变异破坏工具 |

### 3. TYPE 两核心文件最新哈希刚性保留 (Rigidly Preserved)
以下两个 TYPE 关键文件严格保留最新审查哈希，未引回任何旧版本：
- `arc_markets/quote_catalog.py`: `fea0999ffe3cef0463cb2269d7d7260f171dcf7abe27fa4cf26828a1f42903e5` (MATCH)
- `arc_runtime/shadow.py`: `d70a3eb621bec32e3bde9a2d4b21cd9bb2e24d04c8622e2968c51d823712970a` (MATCH)

---

## 四、 全库赤字透明披露与边界声明 (Whole-Repo Deficit Disclosure)

### 1. 全库收集赤字事实
执行全库用例收集：
```bash
pytest --collect-only
```
- **实测结果**: 收集到 1633 个用例，伴随 **49 个用例收集错误 (49 errors during collection in 5.02s)**（详见 `/tmp/arc-restoration-final-review/logs/pytest_collect_all.log`）。
- **错误成因**: 历史未迁移的旧测试模块依赖已废弃或未导入的 `arbitrage`、`core`、`backtest` 等旧包。
- **刚性结论**: **本项目不能称全工程完成 (NOT full-repo PASS)**。
  - 本轮动态合格证据严格限定于既有受审范围（Manifest 5 项、RWA 44 项、Arc V3 375 项、W5/W7 离线门禁及 W3/W5/W7 变异）。
  - 49 个旧模块收集错误属于历史架构遗留赤字，将在后续专项工单中逐一独立清理。
  - 本次集成严格禁止擅自引入旧交易器逻辑或在测试中添加跳过（skip/ignore）来掩盖赤字。

### 2. 安全与架构边界保留
- 本工单未修改 `AGENTS.md`、安全红线条款、滑点与保底门禁公式、500 USD 上限与 Dry-run 限制。
- 严禁提交至远端，严禁在本地工作树执行 commit 或 push。
