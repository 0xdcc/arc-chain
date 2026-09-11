# ARC 四主脑 v3.1 阶段性验收交付报告（G2_RELEASE）

- **计划编号**：`ARC-4B-v3.1-20260911-13817f4`
- **目标网络**：Arc Mainnet (Chain ID: `5042`)
- **交付主脑**：`M1`（总控与集成）
- **基线版本**：`13817f4e027375dd59cc7a202ae641c068525f53`
- **交付候选SHA**：`516be28757d3fb45e624bc3adccb557b669896fd`
- **公共契约摘要**：`604333c5d6857baece331bc6c78ec44f3eb6ed19e822b8a535928cf1ea14126e`

---

## 1. 四维门禁判定（Gate Verdicts）

| 维度 | 状态判定 | 说明与依据 |
| :--- | :--- | :--- |
| **G2_CODE** | **PASS** | 45 项核心与审查工单全部合入主线，321 项自动化测试 100% 通过（耗时约 10s）。 |
| **G2_LIVE** | **PENDING_AUTHORIZATION** | 等待用户明确指定并获准的实网只读 RPC 端点与调用配额，当前保持离线 Mock/Fixture 安全态。 |
| **ALPHA_EVIDENCE** | **INSUFFICIENT_DATA** | 投研模块基于本地离线合成数据与局部历史回放，尚未连接实网长期探测，不虚假声称实盘已盈利。 |
| **EXECUTION_AUTH** | **DISABLED** | 零资金、零私钥、零广播、零合约部署；资金与执行能力严格处于代码级物理隔离封印态。 |

---

## 2. 各主脑分工与交付总览

| 主脑 | 负责领域 | 已交付工单 | 核心交付成果 |
| :--- | :--- | :--- | :--- |
| **M1** | 总控、协调、基础设施、运行集成 | T01–T06, T37–T42 (12单) | 基础仓库隔离、契约冻结、多租约互斥调度引擎、Outbox 单 Writer 归并器、采集/影子/历史 CLI 入口、进程单实例锁、环境规约与主网检查表。 |
| **M2** | 数据、采集、市场与 V3/V4 发现 | T07–T11, T13–T18 (11单) | 网络 Profile 隔离、固定区块采样、原始 Ingest 记录器、USDC 18d/6d 双接口解耦、历史区块连续扫描与游标断点恢复、V3 事件解码、V4 StateView 4-word 精确解析、报价目录与快照持久化、Tick 覆盖加载器、Launchpad 适配器。 |
| **M3** | 策略仿真、研究与执行风控隔离 | T19–T29, T31–T36 (17单) | 离散报价桥接、经济学成本单扣与净利保底、多 Tick 穿越、账本分段管理、影子服务、只读模拟传输层、Universal Router Calldata 编码、输出凭证证明、执行风控物理隔离、本地 Nonce 状态机日志、场外流动性墙深度定价、Arc 真实成交归因、因果时序回放引擎、Alpha 投研报告。 |
| **M4** | 独立红方审查与对抗性反证 | T43–T48 (6单) | 6 份独立对抗性审计报告全部签署 **PASS**，含 07 规则对账、只读网络边界、F01–F07 变异反证、USDC 双接口防双计、多 Writer 租约混沌与存储断裂自愈、端到端全量门禁复核。 |

> **工单完成统计**：已完成并归并 **45 单**；**T12** 因缺少实网 RPC 端点授权进入延期清单（`DEFERRED_SCOPE.md`）；**T30** 作为 G3 预研冲正模块由 M3 在隔离分支进行收尾。

---

## 3. 核心架构与安全硬核成果

1. **07 规则数据与代码物理隔离**：
   - 严格区分代码与数据，上游本地 346+ 个池目录分类为 `DATA_ONLY_DIFFERENCE`，严禁直接全量拷贝或把 Robinhood 目录池地址引入 Arc 注册表。
2. **Uniswap V4 StateView 4-word 真实解码**：
   - 精确解码 `(sqrtPriceX96, tick, protocolFee, lpFee)`，以补码严格还原负数 tick；数据不足 128 字节 fail-closed 拦截，杜绝切片假降级为 V3。
3. **USDC 双接口与单扣记账防双计**：
   - Arc 原生 18 位本地记账与 6 位 ERC20 资产对账严格解耦，单扣 Gas 费，执行 $\text{output\_floor} = \max(\text{min\_out}, \text{in} + \lceil\text{gas}\rceil + 1\text{ atom})$ 绝对盈利保底。
4. **多 Writer 租约白名单互斥（Multi-Writer Lease Isolation）**：
   - 协调调度器 `validate_dispatch.py` 与单 Writer 归并器 `reduce_outbox.py` 严格防冲突，全生命周期内 43 次批量文件交付零覆盖、零代码丢失。
5. **单实例锁与三熔断保底机制**：
   - `arc_runtime.lifecycle` 提供 PID 自动回收实例锁；只读客户端与 systemd 服务模板内置非谈判 3 次失败断路器，杜绝无限死循环重启。

---

## 4. 真实 CLI 验证指令（可在本地复验）

所有命令行工具均 100% 本地自包含，基于独立虚拟环境运行：

```bash
# 1. 运行全量 321 项测试套件
./venv/bin/python -m pytest tests/arc_v3/ -q -o cache_dir=/tmp/pytest_cache
# 输出：321 passed, 1 warning in 10.06s (100% PASS)

# 2. 离线只读采集 CLI 验证
./venv/bin/python apps/arc_collect.py --chain-id 5042 --from-block 1000 --to-block 1010 --output-dir runtime-data/ingest --fixture-mode
# 输出：{"status": "SUCCESS", "records_written": 11, "manifest_entries": 11}

# 3. 离线只读影子评估 CLI 验证
./venv/bin/python apps/arc_shadow.py --chain-id 5042 --ledger-dir runtime-data/ledgers --fixture-mode
# 输出：{"status": "SUCCESS", "total_evaluated": 1, "profitable_candidates": 1}

# 4. 历史连续区块扫描回放 CLI 验证
./venv/bin/python apps/arc_history.py --chain-id 5042 --from-block 1000 --to-block 1005 --output-dir runtime-data/history --fixture-mode
# 输出：{"status": "SUCCESS", "scanned_blocks": 6, "hypotheses_count": 3}
```

---

## 5. 残余风险与未运行项揭示

1. **未连接实网真实 RPC 节点**：当前所有实网网络调用均处于 Mock/Fixture 保护态，避免未授权流量或 IP 污染。
2. **9月16日公网开放日期到达绝对不赋予自动实盘交易权限**：公网启动日到达后，系统依然保持 `ARMED = False`。
3. **资金安全物理封印**：无私钥签名器，未配置真实钱包，真实交易广播方法物理拦截。

---

## 6. 后续推进建议（待用户审批决定）

1. **只读实网数据源决定**：是否提供或批准一个只读 Arc 主网 RPC 端点（如官方免费 RPC 或专用节点），以便在零资金风险下开启实网区块的被动只读监听与影子评估？
2. **探路者实盘沙盒授权决定**：未来进入 G3 阶段时，是否批准激活极小额度（首笔 $\le 1.0\text{ USD}$、单笔上限 $\le 500.0\text{ USD}$）单发探路者交易？（当前保持完全关闭）
