# Arc-Chain 市场 M3/M1 合流集成验收报告：ABI 字符串解码精准沙箱复验与合流 (Pool String Decode Repair)

- **工单编号**: `TASK-ABI-DECODE-STRING-INTEGRATION`
- **集成主脑**: M1 合流集成 / M3 策略与市场研究
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **候选来源**: `/tmp/arc-string-decode-r2` (`pool_reader.py` 与 `test_pool_string_decode.py`)
- **审查与验证沙箱**: Bubblewrap 极窄沙箱（断网、丢特权、清环境、仅精确挂载单一指定 Python 解释器及其必要 alias，零挂载 `.hermes`、零挂载整目录 `/root/.local`、零挂载整目录 `uv/python`）
- **正式写入文件范围**:
  1. `research/market_data/pool_reader.py`
  2. `tests/arc_v3/independent/test_pool_string_decode.py`
  3. `scripts/test_safety_source_manifest.json`
  4. `docs/acceptance/POOL_STRING_DECODE_REPAIR.md`
- **交付状态**: 🟢 COMPLETED (沙箱复验 100% 通过，精准合流完成，31 项旧用例收集错误声明严格保留)

---

## 一、 精确装配与哈希核验一致表

本集成操作严格提取通过 R2 独立审核准入的 2 个交付源文件，SHA-256 哈希与准入要求 100% 精确一致：

| 序号 | 模块角色 | 目标工作区路径 | 大小 (Bytes) | SHA-256 哈希 | 校验结果 |
|---|---|---|---|---|---|
| 1 | **池抓取器核心解码器** | `research/market_data/pool_reader.py` | 32,376 | `97c53656f9643108df958d9fa7a30d929de6b09b7fdbd7ad9027c08e392b2a89` | ✅ 严格一致 (100% MATCH) |
| 2 | **ABI 独立回归测试套件** | `tests/arc_v3/independent/test_pool_string_decode.py` | 6,087 | `8b71a0b2652709b5e3834b2e0fc45a31eea41489aa040e6a2b61492479d179f7` | ✅ 严格一致 (100% MATCH) |

### 1. 基线覆盖前置核对
- 覆盖前工作树 `research/market_data/pool_reader.py` 哈希确认为：`71442dd0d0e842b534ce9df7a92edd0a21fb0f96286be99da9e311a85c4e92ee`。
- `diff -u` 比对确认：变更仅且严格为在 `PoolReader` 类内新增 `@staticmethod def _decode_string(hexdata: str) -> str:` 静态方法，无任何其他方法、字段或逻辑污染。
- AST 分析确认：`pool_reader.py` 未增删任何 `import` 语句（导入节点保持 13 项绝对不变），无环境副作用。

### 2. 清单更新规则核验
- `scripts/test_safety_source_manifest.json` 文件列表由 525 项更新至 526 项（精确 +1 项）。
- 仅且严格新增独立测试文件：`tests/arc_v3/independent/test_pool_string_decode.py`。
- 全清单保持字典序严格排序与唯一性约束（`test_manifest_is_sorted_and_unique` 门禁验证通过）。
- 严守父级契约：不触碰父目录 `__init__.py`、不触碰 `venv` 软链、保持冻结 `docs/reuse/IMPORT_MANIFEST.json`（281 项义务）未受任何影响。

---

## 二、 严格精准解释器沙箱复验凭证

为彻底收口沙箱挂载范围，本次复验放弃宽泛的 `.local` 和 `.hermes` 挂载，实施极窄隔离沙箱：

### 1. 沙箱隔离参数
- **隔离命名空间**: `--unshare-net --unshare-pid --unshare-ipc --unshare-uts`
- **特权管控**: `--clearenv --cap-drop ALL`
- **私有临时卷**: `--tmpfs / --proc /proc --dev /dev --tmpfs /tmp`
- **解释器精确挂载**:
  - `--ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 --ro-bind /bin /bin`
  - `--dir /root/.local/share/uv/python`
  - `--ro-bind /root/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu /root/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu`
  - `--symlink cpython-3.12.14-linux-x86_64-gnu /root/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu`
  - `--ro-bind /root/projects/crypto/arc-chain/venv /root/projects/crypto/arc-chain/venv`
  - 明确排除：不挂载 `/root/.hermes`、不挂载整个 `/root/.local`、不挂载整个 `/root/.local/share/uv/python`。
- **环境安全变量**: `PYTHONDONTWRITEBYTECODE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `HOME=/tmp`, `TMPDIR=/tmp`。

### 2. 红证证据复用 (RED Proof)
在 R2 阶段已产出物理红证并归档，本次不再宽挂载旧缺陷源码，直接复用既有有效凭据：
- **用例**: `TestPoolStringDecodeBoundaryAndFailClosed::test_preserves_ascii_leading_char_when_length_matches_long_raw`
- **反证确证**: 旧启发式逻辑 `candidate[0] == len(candidate) - 1` 在遇到以 ASCII `'A'` (65) 开头的 66 字节长 raw 字符串时触发误剪，产生 `assert 'XXX...' == 'AXXX...'` 失败（Exit code: 1）。
- **收敛规则**: 限制首字节长度兼容仅在 `len(raw) == 32 and raw.startswith(b"\x00") and 1 <= candidate[0] <= 31 and candidate[0] == len(candidate) - 1 and raw == b"\x00" * (32 - len(candidate)) + candidate` 联合刚性条件下触发，彻底阻断长 raw 字符串首字符误剪。

### 3. 复验结果明细 (GREEN & Static Gates)

#### (1) 14 项独立测试沙箱执行
- **命令**: `pytest tests/arc_v3/independent/test_pool_string_decode.py -v`
- **退出码**: `0`
- **结果**: `14 passed, 1 warning in 0.83s`
- **覆盖清单**:
  - `TestPoolStringDecode::test_decodes_bytes32_layout`: PASSED
  - `TestPoolStringDecode::test_decodes_abi_string_layout`: PASSED
  - `TestPoolStringDecode::test_strips_null_bytes` (历史 fixture 兼容验证): PASSED
  - `TestPoolStringDecodeBoundaryAndFailClosed::test_decodes_padded_standard_abi_string`: PASSED
  - `TestPoolStringDecodeBoundaryAndFailClosed::test_decodes_right_aligned_bytes32_without_length_prefix`: PASSED
  - `TestPoolStringDecodeBoundaryAndFailClosed::test_empty_and_zero_values_return_empty_string`: PASSED
  - `TestPoolStringDecodeBoundaryAndFailClosed::test_abi_dynamic_string_zero_length`: PASSED
  - `TestPoolStringDecodeBoundaryAndFailClosed::test_rejects_truncated_abi_dynamic_string`: PASSED
  - `TestPoolStringDecodeBoundaryAndFailClosed::test_rejects_invalid_hex_format`: PASSED
  - `TestPoolStringDecodeBoundaryAndFailClosed::test_rejects_non_string_input`: PASSED
  - `TestPoolStringDecodeBoundaryAndFailClosed::test_rejects_invalid_utf8`: PASSED
  - `TestPoolStringDecodeBoundaryAndFailClosed::test_no_fake_symbol_fallback`: PASSED
  - `TestPoolStringDecodeBoundaryAndFailClosed::test_preserves_ascii_leading_char_when_length_matches_long_raw`: PASSED
  - `TestPoolStringDecodeBoundaryAndFailClosed::test_standard_abi_dynamic_string_preserves_leading_control_byte`: PASSED

#### (2) 并发高负载重入压测
- **命令**: 多线程池并发解码压力测试（32 线程，每线程 100 轮，共计 25,600 次解码）
- **退出码**: `0`
- **结果**: `32 threads, 3200 iterations, 25600 decodes OK! 无状态污染、无竞争异常`。

#### (3) 旧读池关联小集回归
- **命令**: `pytest tests/test_market_data_pool_reader.py -v`
- **退出码**: `0`
- **结果**: `15 passed, 1 warning in 0.87s`（原有快照读取器逻辑 100% 保持完好）。

#### (4) 静态代码质量与类型检查门禁
- **Ruff**: `ruff check --no-cache --config pyproject.toml research/market_data/pool_reader.py tests/arc_v3/independent/test_pool_string_decode.py`
  - 结果: `All checks passed!` (Exit code: 0)
- **Mypy**: `mypy --cache-dir /tmp/.mypy_cache --config-file pyproject.toml research/market_data/pool_reader.py tests/arc_v3/independent/test_pool_string_decode.py`
  - 结果: `Success: no issues found in 2 source files` (Exit code: 0)

#### (5) Audit 281 与清单覆盖沙箱测试
- **命令**: `pytest tests/arc_v3/independent/test_import_manifest.py tests/arc_v3/independent/test_legacy_archive_registry.py tests/contracts/test_manifest_coverage.py -v`
- **退出码**: `0`
- **结果**: `33 passed in 0.52s`（281 项上游审计义务全部就绪，清单排序与单调递增性 100% 确认）。

---

## 三、 31 项遗留收集错误未解决严正声明 (Unresolved Declarations)

依据工程治理与工单约束，严正声明如下：

1. **未迁移义务表原测试不触碰**:
   - 本次工单范围仅为 `PoolReader._decode_string` 解码逻辑及其对应独立测试用例合流。
   - `tests/test_spread_monitor.py` 等旧价差监控与套利执行测试未在此前实施迁移，其引用未导入的旧模块（如 `arbitrage`）问题依然客观存在。
   - 未擅自修改 `tools/qa/upstream_obligations.py`，未从义务表中删减原测试登记。

2. **31 项收集错误数量保持不变**:
   - 运行全库收集诊断：`pytest tests/ --collect-only -q`。
   - 收集计数：测试用例由 2177 项增至 2191 项（+14 项），**收集错误数依然精确保持 31 项（Interrupted: 31 errors during collection）**。
   - 绝不虚假宣称本次修复消除了全库 31 项收集错误，绝不以新增独立测试用例冒充旧整模块修复。

---

## 四、 安全红线合规核查

1. **零资金与网络操作**: 全程未发起任何 RPC 请求，未连接任何主网/测试网，未操作私钥与资金。
2. **零 Git 远端推送**: 严格本地操作，无 `git push`、无修改远端分支。
3. **断言与契约完整性**: 零删减既有断言，未扩大启发式兼容范围，严格执行 fail-closed 策略。
4. **日志与证据即时落盘**: 全量执行日志落盘于 `/tmp/arc-string-decode-integrate/logs/`，执行完毕立即汇报。
