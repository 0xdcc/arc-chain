# Arc-Chain 市场 M3 合流集成验收报告：已验池抓取器与多池读物套件 (Pool Reader & Multicall Reader Repair)

- **工单编号**: `TASK-M3-POOL-READER-INTEGRATION`
- **集成主脑**: M3 策略/仿真/研究主脑 (Strategy / Simulation / Research) / M1 合流集成
- **基准工作区**: `/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912`
- **候选来源**: `/tmp/arc-m3-r2` (`src/research/market_data/pool_reader.py` 与两套测试)
- **独立审查依据**: `/tmp/arc-m3-r2-review` (`RESULT.md` / `test_run.log` / `fixed_block_probe.log` / `control_falsification.log` / `delta.diff` / `hashes.sha256`)
- **已验测试证据**: 27 项独立测试全数通过 (0.81s)、4/4 项固定块高原子绑定探针全部通过、3/3 项变异反证控制实验全数变红确证、原 121 项断言零删减。
- **交付凭据产物**:
  - `research/market_data/pool_reader.py` (同块多池聚合读取器与 SnapshotCoordinator)
  - `tests/test_market_data_pool_reader.py` (15 项测试用例，78 项断言)
  - `tests/test_multicall_reader.py` (12 项测试用例，43 项断言)
  - `scripts/test_safety_source_manifest.json` (安全源清单仅增 1 项，501 -> 502 项，排序且唯一)
  - `tools/qa/upstream_obligations.py` (仅适配登记两测试旧 hash 为新通过 hash)
  - `docs/acceptance/RESEARCH_POOL_READER_REPAIR.md` (本集成验收报告)
  - `/tmp/arc-m3-integration/RESULT.md` (集成交付与状态报告)

---

## 一、 精确装配与哈希核验一致表

本集成操作严格从 `/tmp/arc-m3-r2` 提取通过独立审查准入的净基线 3 文件，SHA-256 哈希与候选源及审查沙箱保持 100% 精确一致：

| 序号 | 模块角色 | 目标路径 | 大小 (Bytes) | SHA-256 哈希 | 校验结果 |
|---|---|---|---|---|---|
| 1 | **多池聚合抓取器核心** | `research/market_data/pool_reader.py` | 26,398 | `f5b7451e04a450b21592c6d25669fea8ef8a2cd737d607b6bc7a3f9474958b5a` | ✅ 严格一致 (100% MATCH) |
| 2 | **池抓取器测试套件** | `tests/test_market_data_pool_reader.py` | 18,454 | `6a8f28fd16ca6406c22a7692e65b91980ab31b8e504ebe83045a0b53ed23c583` | ✅ 严格一致 (100% MATCH) |
| 3 | **Multicall批量读取测试** | `tests/test_multicall_reader.py` | 14,211 | `2a2a12adb4f5bc78394a061ae8fdfbe5b98030b6b9eed0b69d272efdd4771c3b` | ✅ 严格一致 (100% MATCH) |

---

## 二、 排除父包覆盖与命名空间保护 (Parent Init Exclusion)

候选来源目录 `/tmp/arc-m3-r2` 中包含两个 65 字节的占位 `__init__.py`：
- `src/research/__init__.py` (SHA-256: `103ea31dc622b98a6bff4be31b3d33c7696bae6253f6c0c90b32258e80df9a53`)
- `src/research/market_data/__init__.py` (SHA-256: `103ea31dc622b98a6bff4be31b3d33c7696bae6253f6c0c90b32258e80df9a53`)

**集成保护措施**：
严格禁止覆盖工作区既有父包文件，完好保留既有命名空间导出与精度契约：
1. `research/__init__.py`: 保持 81 字节既有版本 (SHA-256: `0d9b4276824f4b26fd76a6b28fbdae40010a45e9fb56edfa1fca471aad023ece`)。
2. `research/market_data/__init__.py`: 保持 794 字节既有版本 (SHA-256: `e36dbdaf50621fbd18d560c2b2879846496d2da1686c7265fc3df79c5de33142`)，其内含完整的目录与类型导出 (`ROBINHOOD_CHAIN_ID`, `VERIFIED_DEX_FACTORIES`, `MarketSnapshot`, `PoolStateSnapshot` 等)。

---

## 三、 契约与断言完好性 (Assertion Invariants)

- **断言统计**:
  - `tests/test_market_data_pool_reader.py`: 78 个 `assert` 语句
  - `tests/test_multicall_reader.py`: 43 个 `assert` 语句
  - 合计: **121 个 `assert` 语句严格保持**，零删减、零放宽。
- **M2 标准 128 字节 ABI 编码契约**:
  - `tests/test_multicall_reader.py` 中的 `getSlot0` mock fixture 严格遵循 M2 标准 128 字节 ABI：`(uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee)`。

---

## 四、 安全清单与适配登记

1. **安全源清单更新 (`scripts/test_safety_source_manifest.json`)**:
   - `files` 列表自 501 项扩展至 502 项。
   - 仅新增 `research/market_data/pool_reader.py`，保持严格字母序排序与无重复。
   - `public_fixtures` 保持不变。

2. **上游测试义务适配 (`tools/qa/upstream_obligations.py`)**:
   - 仅更新 `KNOWN_ARC_ADAPTATIONS` 中两旧测试的 `expected_sha256`：
     - `tests/test_market_data_pool_reader.py` -> `6a8f28fd16ca6406c22a7692e65b91980ab31b8e504ebe83045a0b53ed23c583`
     - `tests/test_multicall_reader.py` -> `2a2a12adb4f5bc78394a061ae8fdfbe5b98030b6b9eed0b69d272efdd4771c3b`
   - `LEGACY_TEST_MIGRATION_REGISTRY` 保持 2 项不变。
   - `IMPORT_MANIFEST.json` 保持冻结 (281 项)。
   - QA 门禁审计执行 `tools/qa/upstream_obligations.py`：**Overall Status: PASSED [OK] (Exit Code: 0)**。

---

## 五、 门禁与收集验证结果

1. **静态 AST 解析**:
   - 4 个涉入文件全部通过 `ast.parse` 校验，无语法错误。
2. **代码规范 (`ruff check`)**:
   - 基于工作树 `pyproject.toml` 配置，`ruff check` 全量通过 (`All checks passed!`)。
3. **测试收集验证 (`pytest --collect-only`)**:
   - 针对目标模块 `tests/test_market_data_pool_reader.py` 与 `tests/test_multicall_reader.py`：**27 项测试全部成功收集，零错误** (Exit Code: 0)。
   - 全工作树收集对比：收集错误数自 43 降至 41（成功消除目标市场模块中的 2 项 ImportError），已收集测试总数自 1866 增至 1893。
   - 遵循规范：未声称全 44/43 历史收集错误已解决，如实反映本次 M3 合流范围。
4. **安全与隔离承诺**:
   - 全程只读研究，零网络调用，零私钥使用，零真实资金操作。
   - 工作树原有 dirty 文件保持完好，零 `git reset`、零 `git commit`、零 `git push`。
