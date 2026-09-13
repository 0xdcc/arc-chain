# Arc-Chain G3 验收报告: 独立金额三项不变量集成与滑点差异规范备忘 (AMOUNT_INVARIANTS)

- **集成阶段**: G3 精确集成 (M4 审查成果合入)
- **基准工作树**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **审查来源**:
  - `/tmp/arc-fast-track-guard/candidate_amount_invariants.py`
  - `/tmp/arc-g2-amount-review/RESULT.md`
- **落地文件**:
  - 独立测试套件: `tests/arc_v3/independent/test_amount_invariants.py`
  - 安全源清单更新: `scripts/test_safety_source_manifest.json`
  - 本验收备忘: `docs/acceptance/AMOUNT_INVARIANTS.md`

---

## 1. 集成背景与独立架构决策

在 G2 独立验收阶段（`/tmp/arc-g2-amount-review/`），已对 legacy guard 中剥离出的三项核心金额安全不变量完成了断言核验与宿主沙箱实跑。

在 G3 集成落地时，采取**独立测试文件架构**（`tests/arc_v3/independent/test_amount_invariants.py`），而非直接合入 `tests/atomic_execution/test_policy.py`。
**核心工程考量**：
1. **冻结来源与哈希保护**：`tests/atomic_execution/test_policy.py` 为上游复用清单 `docs/reuse/IMPORT_MANIFEST.json` 中受强哈希校验保护的冻结文件。独立文件避免了对冻结源的修改与哈希漂移（保持 `freezeIMPORT` 零改动）。
2. **零生产侵入**：严格不修改 `atomic_execution/`、`arc_*`、CLI 或注册表等生产实现，保持只读与离线研究系统的纯净性。
3. **零外部状态依赖**：金额三项测试纯粹依赖代数计算与生产策略门禁，不混入任何 Tax 目录、未知代币白名单或模拟签名。

---

## 2. 三项独立金额安全不变量规范

合入的 `tests/arc_v3/independent/test_amount_invariants.py` 包含以下三项刚性断言，逐函数 AST dump 与 G2 验收源保持 100% 零漂移（Zero AST Drift）：

### 2.1 Hard Cap 初始化 Fail-Closed 门禁
- **测试用例**: `test_candidate_guard_execution_policy_hard_cap_initialization_fail_closed`
- **生产入口**: `atomic_execution.policy.ExecutionPolicy.__init__`
- **行为规格**:
  - `max_amount_usd = 500.0` 及合法下限（如 `250.0`）正常初始化。
  - 超过 500 USD 硬顶（如 `500.01`、`2000.0`）立即抛出 `ExcessiveAmountError(match=r"exceeds hard limit")`，实施严格 Fail-Closed 防御。
  - 替代了旧系统静默钳位（`guard.max_amount_usd == 500.0`）的弱安全行为。

### 2.2 非正数入参拒绝门禁
- **测试用例**: `test_candidate_guard_evaluate_policy_non_positive_amount_in_rejected`
- **生产入口**: `atomic_execution.policy.evaluate_execution_policy` 与 `atomic_execution.policy.validate_atoms`
- **行为规格**:
  - `amount_in == 0` 或 `amount_in < 0`（如 `-50_000_000`）时，`evaluate_execution_policy` 拒绝通过（`approved=False`，`reason=PolicyRejectionReason.POLICY_VIOLATION`）。
  - 底层原子校验器 `validate_atoms(..., positive=True)` 对 0 或负数严格抛出 `ValueError(match=r"out of bounds [1, 2^256-1]")`。

### 2.3 保底输出非正数与零滑点拒绝门禁
- **测试用例**: `test_candidate_guard_calculate_output_floor_non_positive_inputs_rejected`
- **生产入口**: `atomic_execution.policy.calculate_output_floor`
- **行为规格**:
  - `amount_in == 0`：抛出 `ValueError`。
  - `expected_out == 0`：抛出 `ValueError`。
  - `slippage_bps == 0`：严格抛出 `ValueError(match=r"slippage_bps must be between 1 and 500, got 0")`，捍卫零滑点不可触碰的铁律。

---

## 3. 滑点单位差异与既有行为事实说明

### 3.1 数学换算与“非等价”定性
根据 G2 专项核算：
- **旧版单位 (`tests/test_guard.py`)**: 浮点百分比（`%`），最低阈值 `0.1` 表示 $0.1\% = 0.0010 = \frac{10}{10000}$（即 10 bps）。
- **新版规范 (`atomic_execution.policy`)**: 整型基点（`bps`），最低阈值 `1` 表示 $1\text{ bps} = 0.0001 = 0.01\%$。
- **比值与差异**:
  $$\frac{0.1\%}{1\text{ bps}} = \frac{0.001}{0.0001} = 10$$
  二者具有 **10 倍（10x）** 的数值差异。

### 3.2 既有行为差异与纪律红线
1. **既有行为差异**: 这是新旧两代架构在防夹精度演进中的**既有行为差异**（分辨率从 0.1% 细化至 1 bps，下界由 10 bps 扩展至 1 bps）。
2. **严禁写等价**: **严禁在任何工程记录或验收文档中将 0.1% 与 1 bps 称为“等价承接”或“等价转换”**。
3. **严禁改动阈值**: 本任务与当前各子代理**无权批准或擅自修改任何滑点阈值**。生产规范 `MIN_SLIPPAGE_BPS = 1` 与 `MAX_SLIPPAGE_BPS = 500` 维持完全不变。

---

## 4. 原 Guard 归档状态说明

- **当前状态**: `tests/test_guard.py` **暂不归档（RETAINED / NOT ARCHIVED）**。
- **原因说明**:
  1. 待入库的金额三项已在独立套件中正式落盘受控。
  2. 原 Guard 中涉及的 4 项遗留接口（私钥文件权限校验 `check_private_key_file` 及广播断言 `assert_can_broadcast`）属于纯只读与物理封印规范下的退役接口（`OUT_OF_SCOPE_RETIRED`）。
  3. 按照 G2 决议，在架构总控（M1）正式发布接口退役规格裁定之前，原测试文件保持现状，不进行物理删除或移动归档，确保审计追踪链条完整。

---

## 5. 安全红线与合规自检

- [x] **未修改生产实现**: `atomic_execution/` 及所有生产业务代码保持只读。
- [x] **未修改冻结来源**: `tests/atomic_execution/test_policy.py` 与 `docs/reuse/IMPORT_MANIFEST.json` 保持零变更。
- [x] **未修改滑点策略与阈值**: 保持生产 `1..500` bps 不变。
- [x] **零断言漂移**: 三项金额函数通过纯静态 AST 比对，与 G2 验收源 100% 吻合。
- [x] **清单唯一排序**: `scripts/test_safety_source_manifest.json` 正确收录新路径，保证有序且无重复项。
- [x] **零资金与网络操作**: 本地只读研究环境，无私钥暴露，无资金流动，无网络 push。
