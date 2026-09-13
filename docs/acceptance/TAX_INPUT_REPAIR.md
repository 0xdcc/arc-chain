# Arc 门禁修复批次 T6: 税费门禁三文件装配与避让公共清单集成验收报告

- **工单编号**: `TASK-T6-TAX-INPUT-INTEGRATION`
- **集成主脑**: M1 总控与集成 (Master Control & Integration) / T6 集成执行器
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **精确候选源**: `/tmp/arc-t5-input-fix/src` (三文件精确提取)
- **独立审查对照源**: `/tmp/arc-t5-review/stage` (哈希核对 100% 一致)
- **已验测试证据**: `/tmp/arc-t5-review/RESULT.md` (70 pass 全通 + 旧实现 4 fail 反证有效)
- **交付凭据产物**:
  - `docs/acceptance/TAX_INPUT_REPAIR.md` (本集成验收报告)
  - `/tmp/arc-t6-integration/RESULT.md` (集成执行与状态报告)

---

## 一、 精确装配与哈希核对一致表

本集成操作严格按授权范围装配已通过 T5 审查的三文件，SHA-256 哈希比对结果如下：

| 文件相对路径 | 变更属性 | 前态 SHA-256 | 集成后 SHA-256 | 大小 (Bytes) | 哈希核对 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `atomic_execution/inputs.py` | 核心实现 | `7aaa3c4d72c311829e3e2c69fabf152406ead109105068e0fc071a9638b7b0ed` | `4a39cf00e71d18f374cffe0b9224c516ee4ee0c5e38cc862883c31c741ab59a6` | 28,847 | ✅ MATCH (`/tmp/arc-t5-input-fix/src` & `/tmp/arc-t5-review/stage`) |
| `tests/atomic_execution/test_input_tax_admission.py` | 新增门禁测试 | *(不存在)* | `3b871d21b37d8aa643b099af63400dc7655dc2473c99019e90bb130ea9ffc95f` | 22,056 | ✅ MATCH (`/tmp/arc-t5-input-fix/src` & `/tmp/arc-t5-review/stage`) |
| `tests/atomic_execution/test_inputs.py` | 回归测试适配 | `3105db1ba7d54d7f2b51036fddf5104e1517105fc6afcf7679478dd20d693da5` | `f5879a794da03e91616a4c43a68f690b1514073ac7a8b6253070d40b2b02fdb3` | 33,063 | ✅ MATCH (`/tmp/arc-t5-input-fix/src` & `/tmp/arc-t5-review/stage`) |

---

## 二、 语义契约与变更差异分析 (Semantic & Delta Audit)

### 1. `atomic_execution/inputs.py`
- **引入类型**: `from arbitrage_contracts.eligibility import CapabilityStatus, RestrictionStatus`
- **税限制严格判定 (Fail-Closed)**:
  - 提取 `raw_restrictions = getattr(elig, "contract_restrictions", None)`
  - 若 `restrictions_map.get("tax") != RestrictionStatus.VERIFIED_FALSE`，拒绝为 `InputRejectionReason.ASSET_NOT_APPROVED`
  - 若包含 `transfer_tax` 且 `!= RestrictionStatus.VERIFIED_FALSE`，拒绝为 `InputRejectionReason.ASSET_NOT_APPROVED`
  - 彻底防范洗白 (Whitewashing) 漏洞与别名冲突误放
- **池能力状态严格校验**:
  - `cap.can_quote != CapabilityStatus.SUPPORTED` 触发拒绝
  - `cap.can_simulate == CapabilityStatus.UNSUPPORTED` 触发拒绝
  - `cap.can_atomic_execute == CapabilityStatus.UNSUPPORTED` 触发拒绝
  - **保留未知模拟放行 (Unknown Simulate Allowed)**：当 `can_simulate == UNKNOWN` 时保持合法放行，符合系统在报价阶段的设计
  - **保留经济阈值与断言**: 完全保留原经济保底判断 `output_floor >= amount_in + gas_atoms + 1` 与 `delta_atoms` 限制

### 2. `tests/atomic_execution/test_inputs.py`
- **仅补充合法资产 Tax 规范属性**:
  - 在 `test_c04_approved_assets_pass` 中，仅在 fixture 资产中补充 `contract_restrictions={"tax": RestrictionStatus.VERIFIED_FALSE}`
  - **零原有断言删除**: 既有测试用例与断言完全机械保留，未修改任何其他业务或阈值逻辑

### 3. `tests/atomic_execution/test_input_tax_admission.py`
- **17 个全新门禁测试用例**:
  - 覆盖 Tax 权威验证、缺失拦截、洗白防护、别名冲突、池能力的 quote/simulate/execute 各种组合
  - 确保分类理由严格归属 `ASSET_NOT_APPROVED` / `POOL_CAPABILITY_UNSUPPORTED`

---

## 三、 并行避让公共清单与登记待集中

因 B3 并行工单正在写入 `scripts/test_safety_source_manifest.json` 与 `tools/qa/upstream_obligations.py`，为防止并发冲突与覆写风险：
1. **本单严格不写公共清单**:
   - 未修改 `scripts/test_safety_source_manifest.json`
   - 未修改 `tools/qa/upstream_obligations.py`
   - 未修改 `docs/reuse/IMPORT_MANIFEST.json` 或 `docs/reuse/CODE_MANIFEST.json`
2. **待主脑/集中登记项**:
   - **新增测试用例 (待加入 source manifest)**:
     - `tests/atomic_execution/test_input_tax_admission.py` (SHA-256: `3b871d21b37d8aa643b099af63400dc7655dc2473c99019e90bb130ea9ffc95f`)
   - **受监控受审文件 (待登记 KNOWN_ARC_ADAPTATIONS / 审计更新)**:
     - `atomic_execution/inputs.py` (新 SHA-256: `4a39cf00e71d18f374cffe0b9224c516ee4ee0c5e38cc862883c31c741ab59a6`, 前态: `7aaa3c4d72c311829e3e2c69fabf152406ead109105068e0fc071a9638b7b0ed`)
     - `tests/atomic_execution/test_inputs.py` (新 SHA-256: `f5879a794da03e91616a4c43a68f690b1514073ac7a8b6253070d40b2b02fdb3`, 前态: `3105db1ba7d54d7f2b51036fddf5104e1517105fc6afcf7679478dd20d693da5`)
3. **验收状态明确界定**:
   - **不称全验收完 (NOT FULL ACCEPTANCE COMPLETE)**，留待主脑集中完成公共清单合并后进行全量闭环审计。

---

## 四、 静态检查验证结果

遵循任务要求，仅执行静态检查，不执行 pytest 业务 import / audit / bwrap：
1. **AST 语法解析**:
   - `atomic_execution/inputs.py`: ✅ 解析通过
   - `tests/atomic_execution/test_input_tax_admission.py`: ✅ 解析通过
   - `tests/atomic_execution/test_inputs.py`: ✅ 解析通过
2. **类型检查 (`mypy`)**:
   - 运行 `./venv/bin/python -m mypy atomic_execution/inputs.py tests/atomic_execution/test_input_tax_admission.py tests/atomic_execution/test_inputs.py`
   - 输出: `Success: no issues found in 3 source files` (退出码 0)
3. **代码规范 (`ruff`)**:
   - `atomic_execution/inputs.py`: ✅ `All checks passed!`
   - `tests/atomic_execution/test_inputs.py`: ✅ `All checks passed!`
   - `tests/atomic_execution/test_input_tax_admission.py`: 保持与 T5 审查字节完全一致（仅 `I001` 导入排序提示，为保护冻结哈希未篡改）。

---

## 五、 红线与边界合规声明

1. **零资金与网络操作**: 零真实交易、无 approve、无转账、无网络调用、无 git push、无 commit。
2. **源码边界合规**:
   - 未混入 `arc_planning.py` 或外部候选源。
   - 源 main 分支不动，CLI 入口不动，旧测试未做未授权归档。
3. **工作区整洁性**: 仅变更声明的 3 个代码/测试文件及 1 个文档文件。
