# Arc 回测批次 B6: 日志读取器与 37 项契约测试装配集成验收报告

- **工单编号**: `TASK-B6-LOG-READER-INTEGRATION`
- **执行角色**: M1 总控与集成 (Master Control & Integration) / B6 集成执行器
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **精确候选源**: `/tmp/arc-b6-r3/src`
- **独立审查对照源**: `/tmp/arc-b6-r3-review/src` (哈希核对 100% 一致)
- **已验测试证据**: `/tmp/arc-b6-r3-review/RESULT.md` (37 pass 全通、2 probe 探针通过、旧 R2 bool 11 fail 真实红、正常 9 pass 真实绿)
- **交付产物**:
  - `research/backtest/log_reader.py` (装配落盘)
  - `tests/arc_v3/independent/test_research_log_reader.py` (新增测试套件)
  - `scripts/test_safety_source_manifest.json` (源码清单仅增 2 项，494 -> 496 排序唯一)
  - `docs/acceptance/LOG_READER_REPAIR.md` (本集成验收报告)
  - `/tmp/arc-b6-integration/RESULT.md` (合流执行与状态凭据)

---

## 一、 精确装配与哈希核对一致表

本集成严格按授权范围装配已通过 B6R3 审查的文件，SHA-256 哈希比对结果如下：

| 文件相对路径 | 变更属性 | 前态 SHA-256 | 集成后 SHA-256 | 大小 (Bytes) | 哈希核对 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `research/backtest/log_reader.py` | 核心实现 | *(不存在)* | `281ed19d909dbf755bc6a7d16dcca97d73e9c70c43a4820ced82ac5b8b1473e3` | 23,310 | ✅ MATCH (`/tmp/arc-b6-r3/src` & `/tmp/arc-b6-r3-review/src`) |
| `tests/arc_v3/independent/test_research_log_reader.py` | 新增契约测试 | *(不存在)* | `16413e7e8926a1fbc6f7965dd1312d3acbd217fa564d89fe98b57e0b0bb0c9a1` | 30,379 | ✅ MATCH (`/tmp/arc-b6-r3/src` & `/tmp/arc-b6-r3-review/src`) |

装配过程不重构、不修剪任何代码，确保字节流与受审版本 100% 吻合。

---

## 二、 语义契约与核心修复分析

### 1. 数量参数防布尔穿透 (Boolean Rejection)
- 在 Python 中 `isinstance(True, int)` 为 `True`，旧实现未拦截布尔类型，存在将布尔值误当作区块号或跨度的风险。
- 新增严格防御：
  - `from_block`, `to_block`: 显式 `isinstance(..., bool)` 检查并拒绝为 `ArcValidationError`。
  - `min_span`, `max_span`, `max_depth`, `limit_threshold`: 显式拦截 `bool`。
  - 响应字段解析：`blockNumber`, `logIndex`, `transactionIndex` 均拦截 `bool` 伪装整型。

### 2. 正常数量与 Removed 字段合法语义放行
- 合法整型 0 / 1 与十六进制数量（`"0x0"`, `"0x1"`, `"0x64"` 等）正常通过校验。
- `removed` 字段本身作为布尔标志（`True` 表示回滚日志，`False` 表示生效日志），在 `_extract_log_payload` 中保持合法布尔语义，未被整数量校验误伤。

### 3. 冲突拒绝与 Fail-Closed 机制
- 同一区块、交易哈希与日志索引的日志，若出现数据或主题冲突，坚决抛出 `ArcValidationError`，杜绝静默吞掉或覆盖。
- 单块超限或不可恢复 RPC 错误时坚决 fail-closed，绝不伪造空结果返回。

---

## 三、 源码清单登记与冷冻边界保持

1. **源码清单登记 (`scripts/test_safety_source_manifest.json`)**:
   - 原清单包含 494 个源文件。
   - 本工单仅追加以下两条路径：
     - `research/backtest/log_reader.py`
     - `tests/arc_v3/independent/test_research_log_reader.py`
   - 更新后条目总数严格为 496，按字典序升序排序，无重复条目。
   - `public_fixtures` 配置完全保持原样。

2. **冷冻边界与零扰动原则**:
   - 绝不动 `docs/reuse/IMPORT_MANIFEST.json` 与 `docs/reuse/CODE_MANIFEST.json`。
   - 绝不动 registry 注册表及任何 pool 目录清单。
   - 绝不动旧测试 `tests/test_tick_cache.py`。
   - 绝不动 CLI、scanner 及其他 research 实现。
   - 工作区原既有脏改动（如其他批次的在制文件）完全保留，未做任何丢弃或清理。

---

## 四、 离线 Handler 注入与 Live 义务边界界定

1. **离线只读 Handler 注入**:
   - `OfflineLogRangeReader` 严格依赖入参注入的 `ReadOnlyRpcTransport`，执行离线回放与历史日志读取。
   - 零新增网络连接，无 socket 监听与远端连接行为。

2. **原 Live 义务未验证明确声明**:
   - 本次集成仅验证了离线环境下 `OfflineLogRangeReader` 的契约测试与回测基础组件。
   - 生产网络环境下的原始 live 义务、实时 websocket/RPC 轮询与在线 tick 恢复机制未在本次合流中进行端到端 live 验证。
   - **明确声明：不能称旧 tick 全部闭合 (Cannot claim old tick obligations are fully closed)**。相关 live 状态有待后续生产集成测试进一步复核。

---

## 五、 静态检查验证结果

依据工单约束，仅执行静态检查，不执行 pytest、业务 import 审计或 bwrap：

1. **AST 语法解析**:
   - `research/backtest/log_reader.py`: ✅ AST 解析通过 (11 顶层语句)
   - `tests/arc_v3/independent/test_research_log_reader.py`: ✅ AST 解析通过 (25 顶层语句)

2. **代码规范检查 (`ruff`)**:
   - 配置：显式指定 `--config pyproject.toml`
   - `research/backtest/log_reader.py`: ✅ `All checks passed!`
   - `tests/arc_v3/independent/test_research_log_reader.py`: 保持受审字节完全一致，未做变动。

3. **类型检查 (`mypy`)**:
   - 配置：显式指定 `--config-file pyproject.toml`
   - `research/backtest/log_reader.py`: ✅ `Success: no issues found in 1 source file` (退出码 0)
   - `tests/arc_v3/independent/test_research_log_reader.py`: 包含故意注入的非 ReadOnlyRpcTransport 运行时拒绝测试，保持受审测试用例原貌。

---

## 六、 资金与操作红线合规声明

1. **主仓零写入 (Zero Writes to Main Repo)**:
   - `/root/projects/crypto/arc-chain` 主仓状态为零写入，未增删改任何文件。
2. **零真实资金与网络操作**:
   - 无真实交易、无转账、无 approve、无合约交互、无网络外联。
3. **零 Git Commit / Push**:
   - 本操作仅完成工作树装配与登记，未执行 `git commit` 或 `git push`。
