# 单个旧测试 tests/test_rpc_policy.py 字节保全归档与安全扩展登记验收报告

- **工单编号**: `TASK-LEGACY-RPC-ARCHIVE-R1`
- **执行角色**: M1 总控与集成 / M4 独立审查
- **授权依据**: `/tmp/arc-rpc-policy-disposition-review/REVIEW.md` 及 `rectified_mapping.json` 独立归档准入裁决，遵循 `USER_APPROVED_LEGACY_P1` 规约
- **准入标的**: 仅针对 `tests/test_rpc_policy.py` 单个旧测试文件，其余 47 个历史旧测试与 CLI 保持严格冻结未动
- **执行方式**: 纯静态施工；源集成只读；在 `/tmp/arc-rpc-archive-r1/src` 纯 bytes 隔离快照中实施
- **安全契约**: 遵循 `AGENTS.md` v3.1，只读研究、零资金、零网络、零合约部署、零外部写、Fail-Closed

---

## 一、 归档迁移背景与核心原则

### 1.1 历史冲突痛点
- 历史旧测试 `tests/test_rpc_policy.py` 存留于 `tests/` 根目录下，调用废弃 Robinhood 交易/广播接口，在 CI 或全量收集时产生环境假红。
- 该文件被记录于 `docs/reuse/IMPORT_MANIFEST.json`（上游冻结清单，共 281 项）。直接删除会导致上游审计工具 `tools/qa/upstream_obligations.py::audit_imported_files` 报 `missing_files` 并阻断门禁。
- 若直接在 `IMPORT_MANIFEST.json` 中修改条目或哈希，则严重违反“上游冻结清单不可篡改”的工程红线。

### 1.2 M1/M4 受控迁移解决方案
1. **冻结清单不可侵犯**: `docs/reuse/IMPORT_MANIFEST.json` 保持 100% 冻结，上游原始 SHA-256 (`20722798985cc87ad9c2b3e656fe14bba5ce66834b5307bb9f54e49366f71ed9`) 保持不动。
2. **纯 Bytes 历史源归档**: 将 `tests/test_rpc_policy.py` 原样迁移至 `docs/legacy_tests/robinhood/test_rpc_policy.py.txt`，采用 `.txt` 后缀彻底脱离 pytest 可执行模块收集范围，同时 100% 保持已审字节。
3. **合法适配链核验**: 归档文件保留已审 bytes (`9540b6388e965768d36cd80098ec8a48987b027aa01a8d0d395c4c8cc92572d5`)，在 `KNOWN_ARC_ADAPTATIONS` 与注册表中双向记录原 hash 与已审 hash，杜绝拿当前 bytes 冒充最初上游 hash；适配项维持 72 项不变。
4. **显式注册表与共用受控解析**: 在 `tools/qa/upstream_obligations.py` 维护 `LEGACY_TEST_MIGRATION_REGISTRY`（精确包含 `tests/test_v4_poolkey.py` 与 `tests/test_rpc_policy.py` 两条），强制全局目标唯一性校验 `validate_legacy_migration_registry`，杜绝多源映射至相同归档目标。
5. **全面安全防线**: 刚性保留原路径穿越、父目录软链接、叶子软链接、硬链接 (`st_nlink > 1`)、双份漂移、篡改检测及未登记不豁免全部安全防御。
6. **受审义务总分母守恒**: 归档后依然严格核验全部 281 项受审义务，绝不依靠减少分母伪造全绿。

---

## 二、 字节保全与哈希核验单 (100% 吻合)

| 核验项 | 迁移前原路径 | 迁移后归档路径 | 核验结果 |
|---|---|---|:---:|
| **文件相对路径** | `tests/test_rpc_policy.py` | `docs/legacy_tests/robinhood/test_rpc_policy.py.txt` | 🟢 **迁出并新增** |
| **磁盘字节大小** | 12,225 bytes | 12,225 bytes | 🟢 **100% 吻合** |
| **文件行数** | 325 lines | 325 lines | 🟢 **100% 吻合** |
| **已审 SHA-256** | `9540b6388e965768d36cd80098ec8a48987b027aa01a8d0d395c4c8cc92572d5` | `9540b6388e965768d36cd80098ec8a48987b027aa01a8d0d395c4c8cc92572d5` | 🟢 **MATCH (100%)** |
| **硬链接数 (`st_nlink`)** | 1 | 1 | 🟢 **独立文件** |
| **软链接检测** | 否 | 否 | 🟢 **常规文件** |
| **上游最初冻结 SHA-256** | `20722798985cc87ad9c2b3e656fe14bba5ce66834b5307bb9f54e49366f71ed9` | `20722798985cc87ad9c2b3e656fe14bba5ce66834b5307bb9f54e49366f71ed9` | 🟢 **沿适配链核验** |

---

## 三、 15 函数逐项审计与承接裁决映射表

依据 `/tmp/arc-rpc-policy-disposition-review/REVIEW.md` 与 `rectified_mapping.json`，15 个原函数的业务与安全断言已全部闭合：

| # | 原测试函数名 | 裁决分类 | 承接测试路径与断言说明 | 架构依据与设计判定 |
|:---:|:---|:---:|:---|:---|
| 1 | `test_1_readonly_policy_allows_view_calls` | **covered_verified** | `tests/arc_v3/independent/test_readonly_boundary.py` | 白名单内只读方法放行得到实测覆盖。 |
| 2 | `test_2_mutating_methods_blocked_default` | **covered_verified** | `tests/arc_v3/independent/test_readonly_boundary.py` | 状态变更方法默认拦截得到实测覆盖。 |
| 3 | `test_3_wildcard_methods_blocked` | **covered_verified** | `tests/arc_v3/independent/test_readonly_boundary.py` | 通配符与未知方法严格拒绝。 |
| 4 | `test_4_debug_trace_namespace_blocked` | **covered_verified** | `tests/arc_v3/independent/test_readonly_boundary.py` | debug/trace 命名空间刚性封禁。 |
| 5 | `test_5_state_override_blocked` | **covered_verified** | `tests/arc_v3/independent/test_readonly_boundary.py` | 状态重写方法刚性封禁。 |
| 6 | `test_6_local_sensitive_tier_toggle` | **composite** (默认拒绝 verified / 开关放行 spec_excluded) | `tests/arc_v3/independent/test_readonly_boundary.py` | 默认拒绝受测覆盖；放行签名开关依据 `AGENTS.md` 红线 1 与红线 2 规范废除（坚决不提供签名特权旁路）。 |
| 7 | `test_7_rate_limit_counter_tracks_calls` | **covered_verified** | `tests/arc_v3/ingest/test_profile_transport.py` | 传输层请求计数与速率追踪实测覆盖。 |
| 8 | `test_8_rate_limit_triggers_backoff` | **covered_verified** | `tests/arc_v3/ingest/test_profile_transport.py` | 速率限制退避机制实测覆盖。 |
| 9 | `test_9_policy_manifest_file_loading` | **spec_excluded** | `arc_readiness/rpc_readonly.py` (`ALLOWED_READONLY_METHODS`) | 废除运行时动态反序列化 JSON 规则，改用代码内不可变白名单 frozenset，消除动态篡改与软链投毒攻击面。 |
| 10 | `test_10_invalid_endpoint_url_rejected` | **covered_verified** | `tests/arc_v3/independent/test_http_batch_contract.py` | 无效端点 URL 刚性校验与异常拦截。 |
| 11 | `test_11_http_timeout_and_retry_config` | **covered_verified** | `tests/arc_v3/independent/test_http_batch_contract.py` | 超时配置与重试边界实测覆盖。 |
| 12 | `test_12_confirmation_token_not_leaked_in_exception` | **composite** (广播 token 废除 / 凭据脱敏 verified) | `tests/arc_v3/independent/test_http_batch_contract.py` | 旧交易广播 confirmation_token 随广播机制彻底废除；敏感凭据（URL 密码/环境凭据）脱敏替换由契约测试实测覆盖。 |
| 13 | `test_13_batch_request_size_limit` | **covered_verified** | `tests/arc_v3/independent/test_http_batch_contract.py` | 批处理请求上限 fail-closed 拦截。 |
| 14 | `test_14_batch_heterogeneous_policy_enforcement` | **covered_verified** | `tests/arc_v3/independent/test_http_batch_contract.py` | 批请求异构策略强校验实测覆盖。 |
| 15 | `test_15_legacy_config_backward_compat` | **spec_excluded** | `docs/plan_v3/01_四主脑总计划.md` | 规范废弃旧 JSON 兼容层，解耦历史配置。 |

---

## 四、 跨调用点精确修改范围 (Exact Write Scope)

本次收尾严格限制于 7 项限定工件：
1. `tests/test_rpc_policy.py`: 迁出移除（脱敏）。
2. `docs/legacy_tests/robinhood/test_rpc_policy.py.txt`: 原样纯 bytes 保全归档（12,225 bytes, SHA `9540b638...`）。
3. `docs/acceptance/LEGACY_RPC_ARCHIVE.md`: 本验收文档。
4. `tools/qa/upstream_obligations.py`:
   - `LEGACY_TEST_MIGRATION_REGISTRY` 注册表增加 `tests/test_rpc_policy.py` 条目。
   - `KNOWN_ARC_ADAPTATIONS` 更新说明并保留 `expected_sha256`。
   - 增加 `validate_legacy_migration_registry()` 强制目标唯一性校验并在 `resolve_imported_file_path` 中调用。
5. `scripts/test_safety_stage.py`: `PUBLIC_FILES` 精确加入 `docs/legacy_tests/robinhood/test_rpc_policy.py.txt`。
6. `scripts/test_safety_source_manifest.json`: 移出原路径，加入归档路径，保持字典序与全量对齐。
7. `tests/arc_v3/independent/test_legacy_archive_registry.py`:
   - 扩展注册表测试以核验 v4 与 rpc 两条记录。
   - 增加目标唯一性碰撞负例测试 `test_duplicate_archived_destination_rejected_fail_closed`。
   - 保留父目录软链接、叶子软链接、硬链接等全部原安全断言。

---

## 五、 门禁对账与测试影响说明

1. **Pytest 收集影响说明**:
   - `tests/` 根目录遗留测试数量变动，预期使 pytest collection 错误数由 48 降为 47。
   - **注意**: 48 -> 47 系测试收集阶段的错误/问题用例数下降，而非根目录测试文件总数。此项指标属于预期行为，必须由后续独立阶段实测核定，本阶段绝不提前虚假宣称通过。
2. **上游审计工具对账 (Upstream Obligations Audit)**:
   - 受审文件总数: 281 项严格守恒，绝无分母减少。
   - 合法适配条目: 维持 72 项不变。
3. **安全契约一致性**:
   - 零网络、零真实资金、零合约部署、只读离线研究契约 100% 遵守。
