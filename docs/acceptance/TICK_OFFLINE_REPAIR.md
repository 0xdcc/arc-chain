# Arc 回测批次 B5: 纯离线 Tick 缓存与解码器隔离装配集成验收报告

- **工单编号**: `TASK-B5-TICK-OFFLINE-INTEGRATION`
- **集成主脑**: M1 总控与集成 (Master Control & Integration) / B5 合流执行器
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **精确候选源**: `/tmp/arc-b5-single-end/src` (三文件只读装配)
- **独立受试对照源**: `/tmp/arc-b5-single-end-final/integrated` (哈希 100% 严格一致)
- **已验测试与反证证据**:
  - `/tmp/arc-b5-single-end/RESULT.md` (单端 inf 对照反证与引擎拦截机理)
  - `/tmp/arc-b5-single-end-final/RESULT.md` (14 pass pytest 全绿 + mypy 0 issues 凭据)
- **交付凭据产物**:
  - `docs/acceptance/TICK_OFFLINE_REPAIR.md` (本集成验收报告)
  - `/tmp/arc-b5-integration/RESULT.md` (集成交付与状态报告)

---

## 一、 精确装配与哈希核对一致表

本集成操作严格按授权范围，自 `/tmp/arc-b5-single-end/src` 与 `/tmp/arc-b5-single-end-final/integrated` 提取已通过独立测试与类型核验的三文件，校验 SHA-256 哈希 100% 一致后装配入工作区：

| 文件角色 | 目标工作区路径 | 大小 (Bytes) | SHA-256 哈希 | 校验结果 |
|---|---|---|---|---|
| **Swap日志离线解码器** | `research/backtest/swap_decoder.py` | 4,362 | `0bd6aa0bcfbd55855ac2044d0b61f543f5c8ba97d8e4e1ee99fb4f8b4606d65e` | ✅ 严格一致 |
| **纯离线Tick价格缓存** | `research/backtest/tick_cache.py` | 9,166 | `592ed61a098542ac4543aa22eebaa5396cc2151868a50e859df241341070867f` | ✅ 严格一致 |
| **纯离线Tick独立测试套件** | `tests/arc_v3/independent/test_research_tick_offline.py` | 28,183 | `0e9c6e05206170aa1682edc19daebc0eebbefb224908a6140833efcc771c0284` | ✅ 严格一致 |

---

## 二、 原测试文件保持与遗留网络义务明确界定

1. **原测试文件完全不动**:
   - `tests/test_tick_cache.py` 严格保持 **0 字符改动**（未添加 `skip`/`xfail`，未改写 import，未迁移归档）。
   - SHA-256 保持原始基准值不变：`9f5230ce674617c32a7071e98da2b0ad9af1129a8093f5a26a7d7db293328449`（13,741 bytes, 441 行）。
2. **明确未迁移之第 7-10 项网络义务**:
   - 原测试中前 6 项基础逻辑已在 `tests/arc_v3/independent/test_research_tick_offline.py` 中完整具备纯离线隔离实现；
   - 原测试中涉及真实 RPC 与网络特性的后 4 项义务明确属于未完成状态，绝不在本次纯离线工单中虚报通过：
     - **第 7 项** (`test_live_robinhood_decode_pons_pool`): Robinhood 链上 RPC 实时日志抓取与验证；
     - **第 8 项** (`test_rpc_adaptive_bisection_on_limit_overflow`): RPC 二分递归请求与超限自适应重试；
     - **第 9 项** (`test_overflow_semantics_timeout_not_overflow`): 区分网络超时与区间超限语义；
     - **第 10 项** (`test_get_logs_skips_bytes32_poolid`): 针对 bytes32 poolId 的日志过滤逻辑。
   - 上述 7-10 项仍保留在原 `tests/test_tick_cache.py` 中，留待后续网络/RPC 专题治理。

---

## 三、 安全源清单同步 (scripts/test_safety_source_manifest.json)

1. **唯一受限新增**:
   - `scripts/test_safety_source_manifest.json` 的 `files` 列表中**仅新增上述三路径**：
     - `research/backtest/swap_decoder.py`
     - `research/backtest/tick_cache.py`
     - `tests/arc_v3/independent/test_research_tick_offline.py`
2. **排序与去重审计**:
   - 清单严格按字典序递增排序，全局去重；
   - 清单实际文件条目数从 **491** 增至 **494**；
   - 未改动 `public_fixtures`，未引入任何非声明文件。

---

## 四、 离线研究兼容性与哨兵防御机理

1. **价格 0 / 负数 / 非有限值哨兵全面防御**:
   - `TickCache.price_at_with_meta` 与 `price_at` 显式校验 `math.isfinite(candidate.price)` 且 `candidate.price > 0`；
   - 价格 `<= 0` 或非有限值（`+inf`, `-inf`, `NaN`）统一返回 `(None, 'no_trade_at_signal')`；
   - `BacktestEngine` 消费时识别该 meta，自动累加 `no_trade_count`，坚决拒绝撮合与虚假收益穿透。
2. **外部缓存安全边界免责**:
   - 本实现仅服务于回测引擎隔离研究与离线回放；
   - **不声称任何外部缓存安全**；不声明对任意未经验证的第三方持久化缓存具备免责信任。
3. **架构与注册表刚性防线**:
   - 保持 `docs/reuse/IMPORT_MANIFEST.json` 与 `docs/reuse/CODE_MANIFEST.json` 严格 Freeze 状态；
   - 保持 `tools/qa/upstream_obligations.py` 的 `LEGACY_TEST_MIGRATION_REGISTRY` 不动（仅维持原有历史归档条目）；
   - 不动 CLI 入口（`apps/cli.py`）、引擎公共接口（`research/backtest/engine.py` 签名）、注册表。

---

## 五、 静态门禁自检与证据复用

1. **静态门禁检验结果 (严格守约无动态业务导入)**:
   - **AST 语法树解析**:
     - `research/backtest/swap_decoder.py`: AST 节点 505，语法结构正确；
     - `research/backtest/tick_cache.py`: AST 节点 1,267，语法结构正确；
     - `tests/arc_v3/independent/test_research_tick_offline.py`: AST 节点 3,686，包含完整 14 项测试用例定义。
   - **Ruff 检查**:
     - `venv/bin/python -m ruff check ...` 执行结果：`All checks passed!`（退出码 0）。
   - **Mypy 类型检查**:
     - `venv/bin/python -m mypy --config-file pyproject.toml ...` 执行结果：`Success: no issues found in 3 source files`（退出码 0）。
2. **红线遵循与动态执行免责**:
   - 严格遵循红线：**不 pytest 业务 import、不触发 audit/bwrap**。
   - 复用上游受审证据 `/tmp/arc-b5-single-end-final/RESULT.md`：
     - 14 项离线测试在沙箱中全绿通过（`14 passed in 0.13s`）；
     - 真实单端 `+inf` 对照反证闭环已确立，旧版本放行异常、新版本拦截有效。

---

## 六、 最终合流结论

1. **装配状态**: **SUCCESS (精确装配完成)**
   - 三文件哈希与候选源/独立受试源 100% 严格一致；
   - 安全源清单仅净增 3 路径（491 -> 494）；
   - 原 `tests/test_tick_cache.py` 零修改完整保留，7-10 项遗留网络义务明确披露；
   - 静态 AST、Ruff、Mypy 全部无瑕疵通过。
2. **工作区安全声明**:
   - 源主仓 `/root/projects/crypto/arc-chain`（main 分支）保持零写入、零网络请求、零资金操作、无 commit / push，保持 dirty。
