# RUNTIME-R1/R2 运行时包返修与交付验收报告

- **工单编号**：TASK-RUNTIME-R1 / TASK-RUNTIME-R2
- **状态**：awaiting_review
- **基线 Commit**：`02bb55b1525f4ce57ba6ae88f78d83310cc9fc20`
- **执行角色**：RUNTIME 独占 Writer
- **工作区**：`/root/projects/crypto/arc-chain/.worktrees/repair-runtime-package-20260912`
- **Python 环境**：`/root/projects/crypto/arc-chain/venv/bin/python` (Python 3.12.14)
- **注意**：按照工程规约，本单仅针对 RUNTIME 白名单范围验收，**不标记全仓 PASS**。

---

## 1. 修改文件清单 (Strict Whitelist)

严格遵守 RUNTIME 租约白名单，仅修改以下 8 个文件（7 个代码/测试文件 + 1 个新增验收文档）：

1. `apps/arc_collect.py`
2. `arc_runtime/collect.py`
3. `arc_runtime/history.py`
4. `arc_runtime/shadow.py`
5. `tests/arc_v3/runtime/test_collect_cli.py`
6. `tests/arc_v3/runtime/test_history_cli.py`
7. `tests/arc_v3/runtime/test_shadow_cli.py`
8. `docs/acceptance/RUNTIME_R1.md` (新增)

严禁并实际未触碰任何 workflow、主仓、.coord、公共契约、安全规则、凭据、全局配置。无 push / commit / merge / 部署 / 真实交易操作。

---

## 2. 独立审查缺陷返修与安全加固 (R2 返修收敛)

针对 R1 审查报告（`REVIEW.md`）发现的 3 项阻断缺陷与 1 项严重设计缺陷，本轮实施了最小收敛加固：

1. **Manifest 累加合并与连续性全链核验 (解决 BLOCKER 1)**：
   - 修复前：追加分支仅按当次参数生成 manifest 并原地替换，导致历史块范围凭据丢失。
   - 修复后：连续追加分支严格核验原有 `cursor.json`、`coverage_manifest.json` 与 `raw_envelopes.jsonl` 的全量记录（包括行数、单调连续块号、域身份与游标一致性）。在通过全链校验后，将 `from_block` 保持为初始起始块，`to_block` 更新为最新块，`covered_blocks` 与 `expected_blocks` 保持与磁盘上的底层 envelopes 行数完全对齐。

2. **进程排他锁与随机唯一临时文件 (解决 BLOCKER 2)**：
   - 修复前：连续追加使用无锁 `open(..., "a")` 与固定命名临时文件（`.tmp`），并发竞争导致数据重复落盘与未捕获 `FileNotFoundError`。
   - 修复后：对输出目录 `.lock` 获取 `fcntl.flock(LOCK_EX)`，使“检查状态 -> 读取旧数据 -> 写入数据 -> 原子提交”全链路置于进程排他锁保护下。临时文件采用 `tempfile.NamedTemporaryFile` 生成随机独立文件名，经 `fsync()` 落盘后再原子 `os.replace` 提交，并发追加请求严格串行化，冲突进程安全 fail-closed（退出码 2）。

3. **目录与父层符号链接穿透防御 (解决 BLOCKER 3)**：
   - 修复前：仅使用 `lexists` 检查叶子文件名，未对入参目录及其父路径做符号链接检测。
   - 修复后：引入 `_reject_path_symlinks`，对输出目录及所有祖先路径逐层做符号链接检测（`is_symlink()` / `os.path.islink()`）。一旦检测到路径中包含符号链接立即 fail-closed 抛出 `PermissionError`，同时叶子文件继续使用 `O_NOFOLLOW` 打开，杜绝逃逸写入非受信目录。

4. **去除 Shadow 0 字节悬空预留文件 (解决 MAJOR DEFECT 4)**：
   - 修复前：提前执行 `with open(ledger_file, "x"): pass` 占位，导致异常退出后遗留 0 字节空账本文件，毒化后续任务。
   - 修复后：移除 0 字节占位操作，将账本创建收敛至 `ArcOpportunityLedger` 在成功评估后追加首条记录时创建，配合目录锁防止竞态，执行失败不再遗留假凭据。

---

## 3. 测试与反证矩阵

### 3.1 运行时专项测试 (`tests/arc_v3/runtime/`)
- 测试命令：`/root/projects/crypto/arc-chain/venv/bin/python -m pytest tests/arc_v3/runtime/ -v -o cache_dir=/tmp/pytest_cache`
- **结果：63 passed in 10.49s (100% 通过，退出码 0)**
  - 原 CLI 功能与范围验证：通过
  - 未授权 Live 拒绝零文件系统变异 (Zero-Mutation)：通过
  - 各受保护文件兄弟碰撞场景 (No-Clobber Sibling Scenarios)：通过
  - 悬空软链与目录穿透防御：通过
  - 并发追加互斥与重复 batch 安全拦截：通过
  - 多批次连续抓取 Manifest/Envelopes 累加一致性：通过
  - 毒化/损坏 Manifest 及中断状态 Fail-Closed 保护：通过
  - Shadow 异常退出无 0 字节文件残留：通过
  - 零遗留依赖导入隔离 (Zero Legacy Imports)：通过

### 3.2 独立 E2E 与存储测试 (`tests/arc_v3/independent/`)
- 测试命令：`/root/projects/crypto/arc-chain/venv/bin/python -m pytest tests/arc_v3/independent/test_collect_e2e.py tests/arc_v3/independent/test_storage_chaos.py -v -o cache_dir=/tmp/pytest_cache`
- **结果：8 passed in 0.35s (100% 通过，退出码 0)**
  - `test_cursor_monotonic_continuation` 既有历史测试：通过

### 3.3 Arc 套件整体测试 (`tests/arc_v3/`)
- 测试命令：`/root/projects/crypto/arc-chain/venv/bin/python -m pytest tests/arc_v3/ -q -o cache_dir=/tmp/pytest_cache`
- **结果：359 passed, 2 failed in 11.75s**
- **失败项客观归因**：
  - 仅有的 2 处失败为：
    1. `TestExclusionEnforcement.test_no_sensitive_secrets_or_env_files`
    2. `TestExclusionEnforcement.test_no_automated_remote_push_workflows`
  - 失败根因：仓库中存在独立审计 writer 维护的临时诊断工作流 `.github/workflows/arc-audit.yml`，触发了禁止远程 CI/push workflow 的安全门禁。
  - 本工单无 workflow 修改权限，按照任务约定如实记录，不越权修改 workflow，不以未经验证的预判断言全仓结果。

### 3.4 代码规范与格式
- `ruff check`：Exit code 0 (All checks passed!)
- `ruff format --check`：Exit code 0 (7 files already formatted)

---

## 4. 边界与残余风险说明

1. **TOCTOU 边界明确界定**：
   - 路径防御通过逐级检查与叶子节点 `O_NOFOLLOW` 阻断了绝大部分符号链接穿透攻击。
   - 但若运行环境中存在具有写权限的本地对抗性进程在程序通过目录检查后、文件创建前将父级非叶子目录重命名并替换为软链，此属于 OS 级别的目录树并发竞态（需要 Linux 5.6+ `openat2` 与 `RESOLVE_NO_SYMLINKS` 支持）。在本单现有 Python 标准库抽象下，已明确此防御边界，不夸大为绝对零风险。
2. **多文件中断恢复语义**：
   - 当任务在多文件写入的中途被强行中断（如断电或 SIGKILL）导致文件处于部分生成状态时，系统遵循 fail-closed 原则，拒绝覆写或丢弃已落盘数据，防止数据二次破坏。该状态为安全保护状态，不等于数据彻底丢失，需经由专门的恢复工具或重新指定干净目录。
