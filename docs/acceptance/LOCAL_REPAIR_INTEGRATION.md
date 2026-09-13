# LOCAL_REPAIR_INTEGRATION 集成验收与证据报告 (M1 Delivery)

- **工单编号**: TASK-INTEGRATION-R1
- **集成主脑**: M1 总控与集成 (Master Control & Integration)
- **交付状态**: `awaiting_independent_integration_review`
- **工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **基线 Commit**: `02bb55b1525f4ce57ba6ae88f78d83310cc9fc20`
- **受审源候选**:
  1. RUNTIME-R2: `/root/projects/crypto/arc-chain/.worktrees/repair-runtime-package-20260912`
  2. AUDIT-R3: `/root/projects/crypto/arc-chain/.worktrees/repair-local-audit-20260912`
- **证据目录**: `/tmp/arc-integration-evidence/`
- **红线规约**: 零真实资金、零外部网络/RPC调用、零合约部署、禁止 push / commit / merge / 服务重启。主仓不动，源候选 worktree 只读不动。

---

## 1. 候选装配与变更白名单核验

### 1.1 变更文件清册 (16 项精确白名单)

| 来源 | 文件路径 | 变更类型 | SHA-256 (集成后实际值) |
| :--- | :--- | :--- | :--- |
| **Runtime-R2** | `apps/arc_collect.py` | MODIFIED | `085fdbdf0a2b93c34459f7153d5e8f37b1898fd0366ce6c1390fe43e2c0d7090` |
| **Runtime-R2** | `arc_runtime/collect.py` | MODIFIED | `dfacb0412f23210c59d0c3c09be402ff6ae997c80f0ec64b64a545c8338706b1` |
| **Runtime-R2** | `arc_runtime/history.py` | MODIFIED | `007ac7a38c1d39dbf81f552f5e261bc3a0f0ab25b10c9f13113409d1e4c90f1a` |
| **Runtime-R2** | `arc_runtime/shadow.py` | MODIFIED | `41b5368242ab362ac653124b2377ab7e7780310ae4cae2aa53967e59da36e204` |
| **Runtime-R2** | `tests/arc_v3/runtime/test_collect_cli.py` | MODIFIED | `2a1c1149bbc0f63fb19ed6def4e51e8d2e8cf23b6e3053169f83a410ca197a20` |
| **Runtime-R2** | `tests/arc_v3/runtime/test_history_cli.py` | MODIFIED | `3ace43fb84c6e55ce7a247c757db4e22f726c2b8ff8352dfffb04107c4e7b4b0` |
| **Runtime-R2** | `tests/arc_v3/runtime/test_shadow_cli.py` | MODIFIED | `00c7e4ad40fde198cf3c1a6440635429a708430fec6666d164e33d7938c98edf` |
| **Runtime-R2** | `docs/acceptance/RUNTIME_R1.md` | CREATED | `4b7fab9257065a9d6f80a5d7a5b881aabad8577838389b6bbc214a2cfa2a24b0` |
| **Audit-R3** | `.github/workflows/arc-audit.yml` | DELETED | *(已安全移除)* |
| **Audit-R3** | `tests/conftest.py` | MODIFIED | `5e4e91f7fbc4b8c4e364197adb831f2b58e14f929f15dc313e17c809dd69efdc` |
| **Audit-R3** | `tools/checks/arc_audit_local.py` | CREATED | `897c0a2c74942acba564acde9388ea02e00248435d52fe66d8a5e5830a73161b` |
| **Audit-R3** | `tests/arc_v3/runtime/test_local_audit_runner.py` | CREATED | `9d377d929d205376669bed178e9308f206187332d027b198f45b354aa4c441af` |
| **Audit-R3** | `docs/acceptance/LOCAL_AUDIT_R1.md` | CREATED | `0bcda09d66ea84d8e7741e86cb4e7b7422b166c44179d87fb9a61f8c23643c5b` |
| **M1 Adaptation**| `tools/qa/upstream_obligations.py` | MODIFIED | *(登记 tests/conftest.py 获准哈希及理由)* |
| **M1 Adaptation**| `README.md` | MODIFIED | *(纠正全测描述，同步 bwrap 命令与已知边界)* |
| **M1 Adaptation**| `docs/acceptance/LOCAL_REPAIR_INTEGRATION.md` | CREATED | *(本报告)* |

### 1.2 二进制补丁与哈希一致性
- **Runtime 补丁**: `ed843d5ccfefb3b3c84f06cd6b59dd5f517fb87ef3b1d0f9341fe39e9bf236d3` (`runtime_tracked.patch`)
- **Audit 补丁**: `a28ec41567f9b0af57070e062d60c622d1fd29575de45dc889ae3f528a05e053` (`audit_tracked.patch`)
- **比对结果**: 集成后 12 项候选代码与文档的 SHA-256 均与源工作区 100% 比特级一致。

### 1.3 `tools/qa/upstream_obligations.py` 登记说明
依据用户批准及 AUDIT-R3 建议，更新 `KNOWN_ARC_ADAPTATIONS["tests/conftest.py"]`：
- 旧登记哈希：`401e455cbfbfb4979fdd0b79dc19c2e81989b4b205c04fc837c06f46ee097e56`
- 现登记哈希：`5e4e91f7fbc4b8c4e364197adb831f2b58e14f929f15dc313e17c809dd69efdc`
- 登记原因说明：`Arc modular decoupling, safe config singleton handling, /tmp cache redirection harness, and local audit runner subprocess whitelist (AUDIT-R3)`
- 冻结清册守卫：`docs/reuse/IMPORT_MANIFEST.json` 保持严格只读未修改（实际文件 SHA-256 经 `sha256sum` 实测为 `97832326e49dbb15dc39bfb0db46c24a5c9aaed72ec6ed1dd659414e5fc5e211`；前次报告提及的 `9a1ba847d2d61ced27a1982313437ff5fd06f7c8e9f4a3400b5d5173b031e136` 实为清单内 `tests/conftest.py` 原始条目的冻结哈希，非清册全文哈希）。

---

## 2. 隔离验收环境规范 (Bubblewrap OS Isolation)

按照 `/tmp/arc-audit-os-verification/RESULT.md` 的既定规范，所有验收测试在原生 bubblewrap (`bwrap`) 受控沙箱中执行：
- **命名空间隔离**: `--unshare-net --unshare-pid --unshare-ipc --unshare-uts --clearenv`
- **只读根与解释器**: `--ro-bind /usr /usr`, `--ro-bind /lib /lib`, `--ro-bind /lib64 /lib64`, `--ro-bind /bin /bin`, `--ro-bind /root/.local/share/uv/python ...`, `--ro-bind /root/projects/crypto/arc-chain/venv ...`
- **宿主凭据隔离**: 宿主 `/root` 目录不进行全局绑定，`/root/.ssh`、`/root/.secrets` 在沙箱内物理不存在。
- **源码只读挂载**: `--ro-bind /tmp/arc-integration-evidence/src /sandbox/src`
- **专属输出目录**: 沙箱 `/tmp` 挂载独立 tmpfs，runner 日志定向至专属证据目录，不直接暴露宿主环境。

---

## 3. 实测结果与日志证据 (Zero Fail, Zero Skip)

### 3.1 Arc 模块化套件全量测试 (`tests/arc_v3/`)
- **执行命令**: 在 bwrap 隔离环境下运行 `pytest tests/arc_v3/ -v`
- **实测结果**: **`372 passed, 0 failed, 0 skipped`** (耗时约 10.0s)
- **涵盖领域**:
  - `foundation/`: 19 passed
  - `independent/`: 41 passed (含 11 passed `test_import_manifest.py` 双向哈希校验通过)
  - `runtime/`: 74 passed (含 63 项运行时加固测试与 11 项本地审计 runner 测试，0 skipped)
  - `simulation/`: 238 passed
- **全绿闭环**: 彻底解决了原 `.github/workflows/arc-audit.yml` 导致的 2 项违规 CI 测试失败，以及 `tests/conftest.py` 适配哈希不匹配失败。

### 3.2 三项 Fixture CLI 冒烟实测 (bwrap 隔离)
1. **`apps/arc_collect.py`**:
   - 参数: `--chain-id 5042 --from-block 100 --to-block 105 --output-dir /tmp/collect_out --fixture-mode`
   - 结果: Exit `0`，`status: SUCCESS`，生成 6 块 envelope 并校验 manifest。
2. **`apps/arc_history.py`**:
   - 参数: `--chain-id 5042 --from-block 1000 --to-block 1005 --output-dir /tmp/history_out --fixture-mode`
   - 结果: Exit `0`，`status: SUCCESS`，输出 6 块回放假设。
3. **`apps/arc_shadow.py`**:
   - 参数: `--chain-id 5042 --ledger-dir /tmp/shadow_out --fixture-mode`
   - 结果: Exit `0`，`status: SUCCESS`，安全落盘 opportunities 账本，无 0 字节文件残留。

### 3.3 离线审计诊断 Runner 实测 (`tools/checks/arc_audit_local.py`)
- **执行命令**: 在 bwrap 隔离环境下运行 runner 并留存完整日志。
- **实测退出码与分项结果**:
  1. `arc-tests`: **Exit 0** (372 passed, 0 failed, 0 skipped)
  2. `contracts`: **Exit 1** (存量 `tests/contracts/test_manifest_coverage.py` 历史清单与暂存覆盖度不匹配，非资产资格缺陷：`test_staging_roundtrip_and_cleanup` 因引用已解耦的 `live_pipeline.py` 抛出 FileNotFoundError，以及存量清单文件与磁盘增减差异导致 2 fail 1 error)
  3. `full-collection`: **Exit 2** (存量全仓跨模块缺失历史依赖)
  4. `lint`: **Exit 1** (存量 83 处历史 ruff 代码格式告警)
  5. `typing`: **Exit 1** (存量 174 处历史 mypy 类型告警)
- **定性声明**: **全工程非 PASS**。存量 4 项非零退出码如实保留，禁止将全工程标为 PASS。

---

## 4. 架构认知与边界披露

1. **调度器 vs 沙箱职责分离**:
   - `tools/checks/arc_audit_local.py` 定位为应用层调度器，具备凭据环境变量脱敏和独立进程组有界回收能力，但纯 Python 解释器在无特权下无法替代 OS 沙箱。
   - 生产与离线验收交付明确的 `bwrap` 受控隔离命令，不宣称裸跑安全承诺。
2. **破坏与中断 Fail-Closed 机制**:
   - 当历史数据被破坏或异常中断（如断电/崩溃遗留未完成批次）时，系统刚性拒绝原地覆写，抛出异常并保留历史完整状态。此状态必须由人工排查或指定新目录恢复，绝不执行静默冲刷。
3. **目录符号链接 TOCTOU 边界**:
   - 路径安全检查逐级校验祖先软链并为叶子节点配置 `O_NOFOLLOW`。
   - 针对本地并发特权进程在检查后至打开前重命名父目录的 OS 级 TOCTOU 竞态，受限于当前标准库能力，本单不作架构重构扩展，如实列为已知边界。

---

## 5. 最终交付物归档 (`/tmp/arc-integration-evidence/`)

- `RESULT.md`: 本次集成阶段与最终结果。
- `runtime_tracked.patch`: RUNTIME 候选 tracked 变更二进制补丁。
- `audit_tracked.patch`: AUDIT 候选 tracked 变更二进制补丁。
- `src/`: 仅包含已跟踪源码与准入新增文件的受审快照。
- `logs/`:
  - `pytest_arc_v3.log`: bwrap 下 `tests/arc_v3/` 全量 372 用例通过日志（纠正原文件名文档笔误）。
  - `fixture_collect.log`: CLI Ingest 运行日志。
  - `fixture_history.log`: CLI History 运行日志。
  - `fixture_shadow.log`: CLI Shadow 运行日志。
  - `audit_runner.log`: 本地离线诊断 runner 完整输出与 summary.json。
- 状态更新: `awaiting_independent_integration_review`。


---

## 6. COLLECTION-R1 收集修复与集成勘误记录

1. **同名测试模块收集冲突修复**:
   - 根因：`tests/atomic_execution/test_reconciliation.py` 与 `tests/arc_v3/simulation/test_reconciliation.py`、`tests/atomic_execution/test_cli.py` 与 `tests/catalog/test_cli.py` / `tests/settled_cycles/test_cli.py`、`tests/atomic_execution/test_inputs.py` 与 `tests/catalog/test_inputs.py` 因相同 basename 在默认 `prepend` 导入模式下发生命名空间冲突（`import file mismatch`）。
   - 解决方案：在 `pyproject.toml` 中配置 `[tool.pytest.ini_options] addopts = ["--import-mode=importlib"]`，不修改 `testpaths`，不删除或重命名既有测试。
   - 效果：全库 `pytest tests/ --collect-only` 收集数由 1478 项提升至 1586 项（+108 项），收集错误数由 53 项降至 50 项，3 处同名模块收集冲突彻底消除。
2. **测试义务与契约归因勘误**:
   - `tests/contracts` 失败确认系 `test_manifest_coverage.py` 对暂存历史清单覆盖度校验（含已解耦的历史文件），与“资产资格”无代码关联。
   - `docs/reuse/IMPORT_MANIFEST.json` 保持只读，其实际 SHA-256 为 `97832326e49dbb15dc39bfb0db46c24a5c9aaed72ec6ed1dd659414e5fc5e211`。
