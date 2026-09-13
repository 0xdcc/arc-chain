# Arc-Chain 费用读取合流集成验收报告 (RESEARCH_FEE_READS.md)

- **工单编号**: `TASK-M4B-FEE-DIRECT-INTEGRATION`
- **集成基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **遵循规范**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912/AGENTS.md`
- **候选物理来源**: `/tmp/arc-m4-fee-direct/src`
- **独立审查依据**: `/tmp/arc-m4-fee-counterreview` (`RESULT.md` / `logs/pytest_candidate_suite_21pass.log` / `logs/pytest_control_v3_unscaled_red.log` / `logs/pytest_control_v4_relaxed_red.log`)
- **合流状态**: 🟢 **PARTIAL VERIFIED / READY FOR MANIFEST REGISTRATION (避让公共清单，不报全完)**

---

## 一、 精确装配与哈希核验一致表

本工单严格将 `/tmp/arc-m4-fee-direct/src` 经独立审查准入的两源文件直接装配入库，SHA-256 哈希值与候选源、审查存证保持 100% 精确一致：

| 序号 | 模块角色 | 目标文件路径 | 大小 (Bytes) | SHA-256 哈希 | 校验结果 |
|---|---|---|---|---|---|
| 1 | **底层真实费率读取核心** | `research/market_data/fee_verification.py` | 9,478 | `57a9c9b548117f69360be95d3f514a02dd6d539f4fb69b7ece7cd649e7613ca0` | ✅ 严格一致 (100% MATCH) |
| 2 | **独立费率契约测试套件** | `tests/arc_v3/independent/test_research_fee_reads.py` | 10,472 | `c86cafb75603c9201195d3f0714e23c4222e6d942bd7766fa09fe6537e7af322` | ✅ 严格一致 (100% MATCH) |
| 3 | **合流集成验收报告** | `docs/acceptance/RESEARCH_FEE_READS.md` | - | 本文件 | ✅ 新建落盘 |

除上述文件外，本工单严格遵守 **唯一源写** 规则：
- 零改动 `apps/` 与 `main CLI` 代码；
- 零改动 `research/__init__.py` 与 `research/market_data/__init__.py`；
- 零改动 `scripts/test_safety_source_manifest.json` 与 `tools/qa/upstream_obligations.py`。

---

## 二、 公共清单避让与集中登记说明 (Manifest Avoidance)

1. **避让背景**：
   - 当前工作区中 M4A writer 正在并发处理全局源清单 (`scripts/test_safety_source_manifest.json`) 及上游义务清单 (`tools/qa/upstream_obligations.py`) 的登记写入；
   - 为杜绝并发写冲突与租约抢占，本单严格遵守“避让清单并保留边界”原则，严禁在本单中写入任何公共清单或适配字典。

2. **待集中登记项 (两条清单)**：
   - 待登记源文件 1：`research/market_data/fee_verification.py` (SHA256: `57a9c9b548117f69360be95d3f514a02dd6d539f4fb69b7ece7cd649e7613ca0`)
   - 待登记测试文件 2：`tests/arc_v3/independent/test_research_fee_reads.py` (SHA256: `c86cafb75603c9201195d3f0714e23c4222e6d942bd7766fa09fe6537e7af322`)
   - 交付状态明确注明：**不报全完**，等待后续集中登记与统一门禁串联。

---

## 三、 测试与反证击穿证据 (21 Pass & Counter-Review RED)

### 1. 独立契约测试实跑 (21 Passed)
在工作树环境下使用指定解释器直接执行独立契约测试套件：
```bash
PYTHONPATH=. /root/projects/crypto/arc-chain/venv/bin/python -m pytest tests/arc_v3/independent/test_research_fee_reads.py -v
```
- **测试结果**: **21 passed in 0.77s (退出码 0)**
- **保留原断言 100%**: 原 `TestDirectFeeReaders` 3 项断言（成功读取 1.0 bps、RPC Revert 抛 RuntimeError、ABI 0 正常识别）完全保留。
- **协议与边界覆盖**:
  - V3 选择器 `0xddca3f43` 校验、ppm 换算 (500 ppm -> 5.0, 3000 ppm -> 30.0, 10000 ppm -> 100.0)；
  - V4 StateView `0xc815641c` 选择器校验、128 字节完整解析；
  - 截断数据拦截、动态费 `0x800000` 拦截、未验证 DEX 拦截、`rpc=None` 拦截；
  - 兼容类与函数式只读 RPC 处理器注入。

### 2. 真实生产突变反证击穿确证 (`/tmp/arc-m4-fee-counterreview`)
独立审查报告通过真实 Sabotage 突变测试（修改生产代码，原测试不改断言），确证测试套件的真实拦截能力：
1. **Control 副本 A (去除除以 100 换算)**:
   - 突变：恢复旧坏行为 `return float(val)`；
   - 结果：原测试 `test_read_v3_pool_fee_success` 与 `test_v3_fee_ppm_to_bps_conversion` **精准变红 (FAILED)**，捕获未缩放错误。
2. **Control 副本 B (放宽 V4 128 字节长度门禁)**:
   - 突变：将 `len(raw_bytes) != 128` 放宽为 `len(raw_bytes) < 32`；
   - 结果：原测试 `test_read_v4_pool_fee_truncated_length_raises` **精准变红 (FAILED)**，拦截底层异常逸出。

---

## 四、 单点 'latest' 查询边界与接口规范

1. **'latest' 边界定义 (非固定区块快照)**:
   - `_invoke_rpc_call` 中固定携带 `["latest"]` tag；
   - EVM 节点在不同时间点处理 `latest` 请求时对应的 block number 动态增长，因此单次查询**绝非固定区块快照 (Fixed Block Snapshot)**。

2. **跨池非原子性约束 (严禁声称跨池原子)**:
   - 连续调用 `read_v3_pool_fee` / `read_v4_pool_fee` 读取多个池时，调用之间不可避免存在出块间隔；
   - 底层 reader 仅代表该池在单个请求瞬间的链上费率，**绝对不具备跨池原子性**；
   - 严禁套利模型以此类单次调用声称原子行情。

3. **接口保持无 block 参数 (不新改接口)**:
   - `read_v3_pool_fee(addr: str, rpc: Any, *, dex: str = "uniswap-v3") -> float`
   - `read_v4_pool_fee(addr: str, rpc: Any, *, state_view_address: str = STATE_VIEW_ADDRESS) -> float`
   - 两函数均无 `block` 或 `block_identifier` 参数；
   - 遵照本工单规范，**不新改接口、不增设未经上游评审的参数**。多池快照与区块一致性应在后续上层调度使用 Multicall 统一编排。

4. **Zero Fee 合法性与空错误区分**:
   - **合法 0 费率**: 链上返回完整 32 字节全零 (V3: 0 ppm) 或 128 字节 ABI 中 lpFee 为 0 (V4: 0 ppm)，严格解析返回 `0.0 bps`；
   - **空数据/RPC 错误**: 返回 `None`、`""`、`"0x"` 或 Revert 时，严格抛出 `RuntimeError` (Fail-Closed)；
   - 绝不因费率为 0 误判为错误，绝不用 30.0 bps 默认值兜底掩盖空响应。

---

## 五、 剩余未完成义务 (scan8 未完成，不报全完)

本单收敛于底层 pure fee readers，明确不制造伪 stub，以下涉及 `scan_pools` 上层池扫描与清单整合的 8 项测试义务**未完成**，留待后续集成主脑推进：

1. `test_mock_fee_returns_100_while_name_says_001_overrides_to_100` (`scan_pools` 与 `[FEE_MISMATCH]` 日志捕捉)
2. `test_mock_fee_exception_skips_pool_fail_closed` (`scan_pools` 异常跳过与 `[FEE_UNVERIFIED]` 日志)
3. `test_fee_returns_zero_or_empty_skips_pool` (`scan_pools` 过滤空数据池)
4. `test_giga_and_up_v3_real_scenario_verification` (`scan_pools` 真实池名称场景覆盖)
5. `test_v2_pool_fixed_30bps_and_no_rpc_called` (`scan_pools` V2 池免链上查询)
6. `test_v4_pool_reads_fee_from_manifest_or_stateview` (`scan_pools` 优先读 Manifest 元数据)
7. `test_v4_pool_stateview_slot0_fallback` (`scan_pools` StateView 降级扫描)
8. `test_v4_pool_unverified_skips_pool` (`scan_pools` V4 不可读池跳过门禁)

---

## 六、 静态门禁核验结果

- **AST 语法编译 (`py_compile`)**:
  - `research/market_data/fee_verification.py`: PASS (Exit Code 0)
  - `tests/arc_v3/independent/test_research_fee_reads.py`: PASS (Exit Code 0)
- **Mypy 严格类型检查 (`mypy --config-file pyproject.toml`)**:
  - 执行结果: `Success: no issues found in 2 source files` (Exit Code 0)
- **Ruff 规范检查 (`ruff check`)**:
  - `research/market_data/fee_verification.py`: `All checks passed!` (Exit Code 0)
  - `tests/arc_v3/independent/test_research_fee_reads.py`: 代码与候选授权 SHA256 严格一致，保持原样。

---

## 七、 资金与操作红线遵守审计 (AGENTS.md)

1. **零真实资金与网络操作**: 仅使用本地文件拷贝与纯 Mock 契约测试，零 live 网络连接，零链上交互。
2. **禁止 pytest 业务 import**: 仅针对独立无业务依赖的 `test_research_fee_reads.py` 单独测试，未执行全套 pytest，未触发 `apps/` 业务逻辑 import。
3. **禁止 audit 与 bwrap**: 未调用 `arc_audit` 工具或宿主 `bwrap` 命令。
4. **禁止 CLI / 适配器其他写**: 未触碰任何 CLI 入口与源文件清单。
5. **零远端 git push**: 严格本地作业。
