# Arc-Chain: HTTP Batch 修复与独立契约测试集成验收报告

- **工单编号**: TASK-HTTP-BATCH-INTEGRATION
- **集成主脑**: M1 总控与集成 (Master Control & Integration)
- **工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **准入候选源**: `/tmp/arc-http-batch-fix-r2/src` (纯只读源)
- **独立审查凭据**: `/tmp/arc-http-batch-review-r2/RESULT.md` 与 24 项契约用例全绿实跑记录 (`candidate_batch_contract.log`)
- **执行方式**: 本地静态装配与验证 (零网络、零真实资金、禁止 commit/push/merge main/服务重启)
- **交付产物**:
  - `docs/acceptance/HTTP_BATCH_REPAIR.md` (本验收报告)
  - `/tmp/arc-http-batch-integration/RESULT.md` (总控交付审查凭据)

---

## 1. 修复背景与技术实现说明

### 1.1 阻断项修复
1. **阻断点 A (`test_batch_out_of_order_restored_to_request_order`)**:
   - 原测试第 232 行使用了未在只读白名单中的 `eth_blockNumber`，导致批次前置白名单校验直接拦截。
   - 修正为既有只读白名单合法方法 `eth_getBlockByNumber` 并携带参数 `["latest", False]`。
   - `eth_getBlockByNumber` 原本即在 `ALLOWED_READONLY_METHODS` 白名单中，**绝无扩大白名单**。乱序返回项成功按请求 ID 精确恢复为原始序列 `["result_first", "result_second", "result_third"]`。
2. **阻断点 B (`test_batch_circuit_breaker_trips_after_consecutive_failures`)**:
   - 生产代码 `HttpReadOnlyRpcTransport.request_batch` 在达到连续 3 次失败熔断时，异常文案对齐单请求契约添加了 `" Circuit Breaker TRIPPED. Halting repeated requests."` 后缀。
   - 正则断言成功匹配 `Circuit Breaker TRIPPED`，熔断跳闸状态保持 `self._is_tripped = True`，连续失败计数阈值严格维持为 3 次，**绝无变更阈值**。
3. **凭据脱敏安全 (`HttpReadOnlyRpcTransport._sanitize_error`)**:
   - 新增 `_sanitize_error` 辅助方法，对 RPC endpoint URL 中包含的用户名和密码在报错文案中替换为 `<REDACTED>`，确保网络异常信息不会泄露节点凭证。

---

## 2. 精确装配清册与 SHA-256 哈希核验单

本次集成严格限制于受审候选文件及必要的清单/文档增补，主仓与存量工作树保持不动：

| 序号 | 文件路径 | 变更类型 | 受审目标 SHA-256 | 落盘实际 SHA-256 | 一致性裁决 |
|:---:|:---|:---:|:---|:---|:---:|
| 1 | `arc_readiness/http_readonly.py` | MODIFIED | `08cc3af52947728287d27bafb14e8789a536a77598615be902e1c59120850232` | `08cc3af52947728287d27bafb14e8789a536a77598615be902e1c59120850232` | **MATCH (100%)** |
| 2 | `tests/arc_v3/independent/test_http_batch_contract.py` | CREATED | `3ee1f267972ecb348a98213360cc2a5f75d5d0047282bf1992edb6d6d96460a5` | `3ee1f267972ecb348a98213360cc2a5f75d5d0047282bf1992edb6d6d96460a5` | **MATCH (100%)** |
| 3 | `scripts/test_safety_source_manifest.json` | MODIFIED | *(精确增补 test_http_batch_contract.py)* | *(473项，严格字母序)* | **MATCH (100%)** |
| 4 | `docs/acceptance/HTTP_BATCH_REPAIR.md` | CREATED | *(本报告)* | — | **MATCH (100%)** |

---

## 3. 冻结导入清单 (IMPORT_MANIFEST) 与适配登记审查

依据 `ai-coding-governance` 专项门禁 Gate 125, 126, 131 规范：
1. **冻结清单 Membership 检索**:
   - 检索 `docs/reuse/IMPORT_MANIFEST.json`（281 项上游冻结历史导入文件）。
   - `arc_readiness/http_readonly.py` 与 `tests/arc_v3/independent/test_http_batch_contract.py` 均不在 `IMPORT_MANIFEST.json` 中，二者均属 Arc 独立自研只读模块与独立契约测试。
2. **冻结清单守卫**:
   - `docs/reuse/IMPORT_MANIFEST.json` 保持 100% 原始快照不变，零修改。
3. **适配映射表 (`KNOWN_ARC_ADAPTATIONS`)**:
   - 因两受审文件均未被 `IMPORT_MANIFEST.json` 监控，故无需在 `tools/qa/upstream_obligations.py` 的 `KNOWN_ARC_ADAPTATIONS` 中做多余登记，`tools/qa/upstream_obligations.py` 保持现有状态不动。

---

## 4. 源码清单 1:1 双向覆盖度核验

- **清单路径**: `scripts/test_safety_source_manifest.json`
- **变更明细**: 在 `files` 列表中字母序（`test_history_claims.py` 与 `test_import_manifest.py` 之间）精确插入 `"tests/arc_v3/independent/test_http_batch_contract.py"`。
- **总受控文件数**: 由 472 项安全扩充至 473 项。
- **双向覆盖验证**:
  - `disk_files - manifest_files == ∅`（新增测试文件已精确入册，无未审漏测文件）。
  - `manifest_files - disk_files == ∅`（无幽灵缺失文件）。

---

## 5. 工作区与分支行为隔离

1. **存量 Dirty 状态保全**:
   - 集成前工作树已存在的 186 项变更文件完全保持未变。
   - 绝对禁止执行 `git reset --hard`、`git clean -fd` 或 `git checkout -- .`。
2. **CLI 变更严格隔离**:
   - 外部未审或暂缓合入的 CLI 变更（包括 `/tmp/arc-cli-readonly-*`）严格排除在本次集成之外，保持 CLI 行为变更未合入。
3. **主仓零写与无网络/资金操作**:
   - 主仓 `/root/projects/crypto/arc-chain` 保持干净工作树，零字节变动。
   - 全程零网络连接、零 RPC 实盘调用、零资金与私钥操作。
   - 严格禁止 `git commit`、`git push` 或 `git merge main`。

---

## 6. 静态验证门禁执行结果

按照规范要求，本轮验证严格限制为静态验证，不执行 `pytest`、不直接 import 业务模块、不运行 `arc_audit_local.py`、不调用 `bwrap`：

1. **AST 抽象语法树校验**:
   - `arc_readiness/http_readonly.py`: `ast.parse` 校验通过（12 个顶级节点，语法结构完整）。
   - `tests/arc_v3/independent/test_http_batch_contract.py`: `ast.parse` 校验通过（14 个顶级节点，语法结构完整）。
2. **代码风格检查 (Ruff)**:
   - 执行命令: `/root/projects/crypto/arc-chain/venv/bin/ruff check arc_readiness/http_readonly.py tests/arc_v3/independent/test_http_batch_contract.py`
   - 退出码: **0**
   - 检查结果: `All checks passed!`
3. **类型检查 (Mypy)**:
   - 执行命令: `/root/projects/crypto/arc-chain/venv/bin/mypy arc_readiness/http_readonly.py tests/arc_v3/independent/test_http_batch_contract.py --config-file pyproject.toml`
   - 退出码: **1** (子报退出码为 1，精确申报，不可泛称 2)
   - 结果说明: `arc_readiness/http_readonly.py` 零类型告警；`test_http_batch_contract.py` 存在 8 处关于局部 `calls` 列表推导式的标注提示（`Need type annotation for "calls"` 及 `Statement is unreachable`），严格维持受审候选版本原貌，不擅自重新设计代码。
