# LOCAL_AUDIT_R1 交付验收报告 (v3 定向返修交付版)

## 1. 任务概述与交付状态

- **工单编号**: AUDIT-R1 / v3 返修 (依据 `/tmp/arc-resume-diagnosis/TASK-AUDIT-R3.md`)
- **交付状态**: `awaiting_review` (由独立上下文后续审查完整 diff 与证据)
- **执行工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-local-audit-20260912`
- **基线 Commit**: `02bb55b1525f4ce57ba6ae88f78d83310cc9fc20`
- **安全与操作红线**:
  - 零真实资金、零外部网络/RPC调用、零合约部署与远端 push。
  - 严禁执行 `git commit` 或 `git merge`（由 M1 集成主脑后续统一交割）。
  - 严格遵守 M1 扩充后的 5 文件精确写白名单，无越权修改、无跨包文件变动。

---

## 2. 审查演进与返修闭环声明

本工单历经三轮治理与定向返修，实现彻底收敛：

1. **R1 阶段**:
   - 初始交付使用 Mock executor 冒充子进程，外围反证使用自造脚本。
   - M4 独立复审阻断 (BLOCK)，证据撤回。
2. **R2 阶段**:
   - 彻底删除 Mock 参数与类，引入系统真实子进程、进程组清理与真实 pytest 原测试 Sabotage 反证。
   - M4 复审发现阻断项：因测试文件内设置了 `_is_conftest_guarded` autouse fixture，在默认 `pytest tests/arc_v3/` 下 8 个真实子进程用例被静默跳过（`333 passed, 8 skipped`）；同时发现 `--confcutdir` 破坏安全沙箱且缺 `PYTHONPATH` 无法独立加载；进程组清理后 `proc.communicate()` 存在无界等待潜在隐患。
3. **R3 阶段 (本轮修复)**:
   - M1 将写租约精确扩充为 5 文件，仅允许在 `tests/conftest.py` 的 `_REAL_SUBPROCESS_TEST_FILES` 登记 `test_local_audit_runner.py`。
   - 彻底移除 `tests/arc_v3/runtime/test_local_audit_runner.py` 中的 `_is_conftest_guarded` 与 `check_harness_guard` 跳过夹具。
   - 默认入口下 11 项新增测试（3 项单元测试 + 8 项真实子进程测试）**100% 实际执行，0 skipped**。
   - 修复 runner 超时后的 `communicate()` 无界等待：设置 `timeout=2.0` 有界截止时间，遇挂起或孙进程逃逸强关管道，安全回收进程，保留超时前全部日志，返回标准 exit 124。
   - 完成 5 组隔离负对照试验，证实未授权子进程、网络 Socket、越界写与读凭据均被线束刚性拒绝（Fail-Closed）。

---

## 3. 变更白名单与物理文件核对

| 操作类型 | 文件路径 | 变更说明 |
| :--- | :--- | :--- |
| **DELETE** | `.github/workflows/arc-audit.yml` | 彻底移除违规自动推送、写权限及 base64 补丁注入工作流 |
| **CREATE** | `tools/checks/arc_audit_local.py` | 离线纯本地诊断 runner，具备凭据清洗、进程组超时有界清理与完整日志留存 |
| **CREATE** | `tests/arc_v3/runtime/test_local_audit_runner.py` | 11 项真实无网络子进程与单元测试套件（0 skipped），验证退出码、孙进程清理与超时有界性 |
| **CREATE** | `docs/acceptance/LOCAL_AUDIT_R1.md` | 本交付与验收证据报告 (v3) |
| **MODIFY** | `tests/conftest.py` | 仅在 `_REAL_SUBPROCESS_TEST_FILES` 登记 `test_local_audit_runner.py`，不改动任何守卫逻辑与网络/文件规则 |

> 物理校验 (`git status --porcelain`):
> ```text
>  D .github/workflows/arc-audit.yml
>  M tests/conftest.py
> ?? docs/acceptance/LOCAL_AUDIT_R1.md
> ?? tests/arc_v3/runtime/test_local_audit_runner.py
> ?? tools/checks/arc_audit_local.py
> ```
> 变更范围 100% 吻合 M1 授权的 5 文件租约。未修改 `AGENTS.md`、`.coord`、`IMPORT_MANIFEST.json`、`EXCLUSION_MANIFEST.json` 或 `pyproject.toml`。

---

## 4. 本地离线诊断 Runner 核心设计 (`tools/checks/arc_audit_local.py`)

### 4.1 进程组会话与有界超时清理
- 子进程以 `subprocess.Popen(..., start_new_session=True)` 启动，独立成组（`pgid == proc.pid`）。
- **有界超时多级回收**:
  1. `proc.communicate(timeout=timeout_seconds)` 触发超时后标记 `timed_out = True`，`exit_code = 124`。
  2. 发送 `os.killpg(proc.pid, signal.SIGTERM)`，捕获已退出进程异常。
  3. 给予最多 1.0 秒优雅退出时间：`proc.communicate(timeout=1.0)`。
  4. 若仍未退出，升级为 `os.killpg(proc.pid, signal.SIGKILL)`。
  5. 最终通过有界等待 `proc.communicate(timeout=2.0)` 回收输出。若孙进程持有文件描述符逃逸导致再次超时，则强制关闭 `proc.stdout` 与 `proc.stderr`，回收僵尸进程，提取已读出的局部日志，并在错误信息中如实标注 `(cleanup communicate timed out; pipes force-closed, possible orphaned processes or held descriptors)`，严格杜绝无限挂起死锁。

### 4.2 凭据清洗与 CI 检查保全
- 自动剔除含 `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `AUTH`, `CREDENTIAL`, `TELEGRAM`, `DISCORD`, `HERMES`, `WEBHOOK` 的敏感环境变量。
- 注入 `PYTHONDONTWRITEBYTECODE=1` 与 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`。
- 保留 5 项与原 CI 工作流同义的离线诊断项：
  1. `arc-tests`: `pytest tests/arc_v3/ -q --tb=short -o cache_dir=...`
  2. `contracts`: `unittest discover -s tests/contracts -v`
  3. `full-collection`: `pytest tests/ --collect-only -q --tb=short -o cache_dir=...`
  4. `lint`: `ruff check --no-cache .`
  5. `typing`: `mypy --cache-dir=... .`

---

## 5. 真实子进程测试验证与线束白名单 (`tests/arc_v3/runtime/test_local_audit_runner.py`)

### 5.1 11 项用例明细 (全部真实执行，0 skipped)
1. `test_environment_credential_isolation` (单元): 验证敏感环境变量过滤、PATH 优先与 bytecode 禁用。
2. `test_build_audit_checks_structure` (单元): 验证 5 项诊断命令结构与解释器绑定。
3. `test_cli_argument_parsing` (单元): 验证 CLI 参数解析（`--repo-root`, `--evidence-dir`, `--timeout`, `--labels` 等）。
4. `test_real_subprocess_exit_zero` (子进程): 执行真实 Python 子进程输出问候语，断言 exit 0 与 stdout 捕获。
5. `test_real_subprocess_nonzero_exit` (子进程): 执行真实 Python 子进程向 stderr 写入并退出 42，断言 exit 42 与 stderr 捕获。
6. `test_real_subprocess_command_not_found` (子进程): 执行不存在命令，断言 `FileNotFoundError` 捕获并记录 exit 127。
7. `test_real_subprocess_timeout_handling` (子进程): 执行休眠 15 秒子进程，在 1 秒超时下触发清理，断言 exit 124、超时标记、超时前日志保留及有界清理（`duration < 5.0s`）。
8. `test_real_subprocess_process_tree_grandchild_cleanup` (子进程): 真实子进程派生休眠 60 秒孙进程并写入 PID。超时后 runner 终结整个进程组，测试通过 `os.kill(gc_pid, 0)` 抛出 `ProcessLookupError` 证明孙进程彻底消亡，无孤儿进程遗留，执行时间有界（`< 5.0s`）。
9. `test_real_subprocess_multiline_output_preservation` (子进程): 子进程输出 150 行结构化日志，断言日志与磁盘文件逐行完整，零截断。
10. `test_real_subprocess_path_with_spaces_and_custom_cwd` (子进程): 在含空格目录名下启动子进程，断言 `os.getcwd()` 解析准确。
11. `test_real_subprocess_run_audit_aggregation` (子进程): 真实多命令聚合执行，验证 summary JSON 结构、耗时与失败统计。

### 5.2 专项验证命令与结果 (使用真实线束，无 --confcutdir)
```bash
PYTHONPATH=. /root/projects/crypto/arc-chain/venv/bin/python -m pytest tests/arc_v3/runtime/test_local_audit_runner.py -v -o cache_dir=/tmp/arc-audit-r3-evidence/pytest_cache
```
**实测结果**: `11 passed in 2.27s` (11 passed, 0 skipped, 0 warnings)。

---

## 6. 安全线束负对照与沙箱防护验证 (`/tmp/arc-audit-r3-evidence/test_negative_controls.py`)

线束在未放宽全局安全守卫的前提下，通过调用栈白名单验证了严格的 Fail-Closed 机制：

| 序号 | 负对照项目 | 触发行为 | 预期拦截 | 实测结果 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | **未加白子进程** | 非白名单文件调用 `subprocess.Popen` | 抛出 `RuntimeError` | `[PASS]` 捕获 `Real subprocess creation forbidden in isolated tests` |
| 2 | **网络 Socket 拦截** | 调用 `socket.socket(socket.AF_INET, ...)` | 抛出 `RuntimeError` | `[PASS]` 捕获 `Network access forbidden in isolated test environment` (零网络接触) |
| 3 | **越界写拦截** | 尝试写入 `/root/.../forbidden_file.txt` | 抛出 `PermissionError` | `[PASS]` 捕获 `Writing outside /tmp is strictly forbidden during tests` |
| 4 | **凭据读取拦截** | 尝试读取 `/root/.../.env` | 抛出 `PermissionError` | `[PASS]` 捕获 `Access to secret/credential or production repository is forbidden` (零凭据接触) |
| 5 | **加白用例合法放行** | `test_local_audit_runner.py` 栈帧内调用子进程 | 正常放行底层 Popen | `[PASS]` 子进程正常执行输出 `whitelisted_ok` 并返回 exit 0 |

---

## 7. 真实原测试 Sabotage 破坏反证闭环 (Rule 95)

在独立沙箱中通过 `pytest` 驱动原测试 `tests/arc_v3/independent/test_import_manifest.py::TestExclusionEnforcement::test_no_automated_remote_push_workflows`，形成红绿闭环：

- **Phase 1 (RED - 注入违规工作流)**:
  - 注入 `.github/workflows/arc-audit.yml`。
  - **实测退出码**: **`1`** (原 pytest 真实变红)。
  - **断言日志**: `AssertionError: Forbidden automated push workflows found: [...]` (见 `/tmp/arc-audit-r3-evidence/sabotage_red.log`)。
- **Phase 2 (GREEN - 物理移除违规工作流)**:
  - 物理删除违规工作流。
  - **实测退出码**: **`0`** (原 pytest 真实变绿，`1 passed in 0.03s`) (见 `/tmp/arc-audit-r3-evidence/sabotage_green.log`)。

---

## 8. 现场验收证据与残余问题披露

### 8.1 R3 验收证据清单 (`/tmp/arc-audit-r3-evidence/`)
- `test_runner_real_subprocess.log`: 11 passed, 0 skipped，真实子进程与单元测试全绿日志。
- `negative_controls.log`: 5 项负对照（未加白子进程拒绝、Socket拒绝、越界写拒绝、凭据读拒绝、加白放行）全部通过日志。
- `arc-tests.log`: 退出码 1，340 passed, 1 failed (因 conftest.py 哈希更新引发 upstream obligations 校验，详见下文 8.2)。
- `contracts.log`: 退出码 1，存量历史契约测试失败项（非本单范围）。
- `full-collection.log`: 退出码 2，存量全仓 collect-only 遗留缺失模块（非本单范围）。
- `lint.log`: 退出码 1，存量历史 ruff 告警（非本单范围）。
- `typing.log`: 退出码 1，存量历史 mypy 类型错误（非本单范围）。
- `ruff_local.log`: 本次修改的 3 个源码文件 `ruff check` 退出码 0，零告警。
- `mypy_local.log`: 本次修改的 3 个源码文件 `mypy` 退出码 0，零错误。
- `audit_summary.json`: 5 项诊断的完整结构化执行耗时、退出码与输出记录。
- `sabotage_red.log`: 原测试注入违规工作流时的真实 exit 1 失败日志。
- `sabotage_green.log`: 原测试移除违规工作流时的真实 exit 0 成功日志。
- `changed_files.txt`: `git status --porcelain` 严格吻合 5 文件白名单。

### 8.2 上游适配哈希变更说明 (Upstream Adaptation Hash Notice for M1)
- 本轮按 M1 指令在 `tests/conftest.py` 中新增 `test_local_audit_runner.py` 白名单行。
- 修改后 `tests/conftest.py` 的 SHA-256 计算值为:
  `5e4e91f7fbc4b8c4e364197adb831f2b58e14f929f15dc313e17c809dd69efdc`
- `tests/arc_v3/independent/test_import_manifest.py::TestImportManifestParity::test_import_hash_parity_and_documented_adaptations` 会对比 `tools/qa/upstream_obligations.py` 中 `KNOWN_ARC_ADAPTATIONS["tests/conftest.py"]["expected_sha256"]`（原基线值为 `401e455c...`）。
- 参照历史 Commit `8187b0b`（`chore(coord): integrate T38 and T33, update conftest expected hash in QA obligations`）之既定协调流程，`tools/qa/upstream_obligations.py` 属协调总控范畴，非本施工租约允许修改之文件。请 M1 集成主脑在交割合并本工单变更时，同步将 `expected_sha256` 登记更新为 `5e4e91f7fbc4b8c4e364197adb831f2b58e14f929f15dc313e17c809dd69efdc`。更新后 `arc-tests` 即达 341 passed / 0 failed 全绿。
