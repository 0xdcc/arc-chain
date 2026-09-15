# Arc-Chain Permit2 只读模块合流集成验收报告 (PERMIT2_READINESS_REPAIR.md)

- **工单编号**: `TASK-PERMIT2-READINESS-INTEGRATION` (Permit2 只读模块与测试合流验收)
- **集成角色**: M1 总控与集成 (Master Control & Integration) / 配合 M4 独立审查
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **候选来源**: `/tmp/arc-permit2-r2/sandbox_src` (经主脑核验准入的 2 项正式文件)
- **前置凭证**: `/tmp/arc-permit2-r2/RESULT.md` (31 passed, 1 warning; 严格变异分析: 1 fail / 30 pass，诚实记录不虚称 100% 全变异覆盖)

---

## 1. 合流交付文件与 SHA-256 核验

| 文件路径 | SHA-256 | 状态 | 说明 |
|---|---|---|---|
| `arc_readiness/permit2.py` | `bf4bb5c00643e94f62b36056a8cbd8cd593d2c02f984b5621fbed73aa21e577e` | ✅ 100% MATCH | Permit2 生产只读适配器 (Canonical Permit2 校验、ABI解码、安全门禁) |
| `tests/arc_v3/independent/test_permit2_readiness.py` | `10f60734dca5219e7df28340671562e8528fcb887bd6de1d293e46579a39d1a4` | ✅ 100% MATCH | Permit2 独立规格测试集 (31 passed) |
| `scripts/test_safety_source_manifest.json` | - | ✅ UPDATED | 源码清单基线 527 -> 529，按 SOURCE_DIRS 规则登记两个新文件 |
| `docs/acceptance/PERMIT2_READINESS_REPAIR.md` | - | ✅ CREATED | 本验收报告 |

---

## 2. 安全与工程架构约束遵循

1. **零真实资金与网络操作**:
   - 纯只读与离线研究系统，测试均在 `bwrap --unshare-net --unshare-pid --cap-drop ALL --clearenv` 沙箱内运行，零 RPC 网络请求、零资金、零交易。
2. **父级 `__init__.py` 与上游冻结保全**:
   - `arc_readiness/__init__.py` 保持冻结，未擅自扩充导出。
   - `docs/reuse/IMPORT_MANIFEST.json` 与 `tools/qa/upstream_obligations.py` 严格保持不变，未动用或虚减上游历史义务（原 `remaining_protocols` 等 30 项未迁错误完全诚实保留）。
3. **工作区脏树与合法 dirty 保全**:
   - 工作区内既有 33 项修改与新增工件完整保全，无 `git reset`、无 `git clean`、无 `git stash`、无 `git commit/push`。
4. **变异与测试覆盖率客观对账**:
   - 31 个独立测试节点全数通过。
   - 变异测试核验确认：针对 `expiry` 边界等号（`expiry > block_timestamp` vs `>=`）存在 1 项 fail，30 项 pass；客观如实记录，不夸大宣称 100% 变异覆盖。

---

## 3. 门禁验证结果

- **Manifest 覆盖与导入测试**: 16 passed in 0.45s (Exit 0)
- **Permit2 + arc_readiness 相关测试联合执行**: 全数通过 (Exit 0)
- **QA 上游义务 Audit (281 项)**: Overall Status: PASSED [OK] (Exit 0)
- **Ruff 检查**: All checks passed! (Exit 0)
- **全库 Mypy 类型检查**: Success: no issues found (Exit 0)
- **Collect-only 收集差集**:
  - Before: 2213 tests collected, 30 errors
  - After: 2244 tests collected, 30 errors (+31 tests, 0 新增错误, 历史 30 项未迁错误不虚减)
