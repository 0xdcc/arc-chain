# Arc 套利研究引擎

面向 Arc 的模块化链上数据采集、机会评估与历史回放项目。当前以**只读研究和离线模拟**为主，公开代码不代表可以直接使用真实资金交易，也不代表策略已经验证盈利。

**作者 X：[@0xdcc](https://x.com/0xdcc)**

- 目标网络：Arc 主网（Chain ID：`5042`）与测试网（`5042002`）。目标配置不代表已完成主网验证。
- 方案编号：`ARC-4B-v3.1-20260911-13817f4`
- 冻结契约摘要：`604333c5d6857baece331bc6c78ec44f3eb6ed19e822b8a535928cf1ea14126e`

## 当前状态

离线样例采集、影子评估、历史扫描和显式输入模拟已有隔离运行验证。`--fixture-mode` 使用样例数据，不连接真实行情；模拟成功不等于交易成功或盈利。

**全库测试尚未通过。** 历史模块仍有导入与运行失败，实时监控和报价入口也在修复中，暂不列为已验收能力。局部测试通过不能替代全库验收；主网运行、签名与广播不在当前可用性承诺内。

## 快速开始

### Python 环境

使用 Python 3.12。以下命令展示维护环境中的路径，不是面向新机器的一键安装脚本；在其他机器使用时，需要按实际仓库位置和解释器位置调整。

```bash
cd /root/projects/crypto/arc-chain
source venv/bin/activate
```

独立 worktree 没有自己的 `venv` 时，可使用主仓解释器：

```bash
source /root/projects/crypto/arc-chain/venv/bin/activate
```

### 隔离运行测试

推荐使用 Bubblewrap（`bwrap`）隔离网络、环境变量和可写目录。下面命令适用于维护环境的 Linux 路径；运行前进入待验收的仓库或 worktree。虚拟环境依赖的 Python 安装目录也必须正确挂载。

`tools/checks/arc_audit_local.py` 只是检查调度器，**不提供操作系统级隔离**。隔离启动失败时不要降级为裸跑。

```bash
bwrap \
  --unshare-net --unshare-pid --unshare-ipc --unshare-uts --clearenv \
  --setenv PATH /root/projects/crypto/arc-chain/venv/bin:/usr/bin:/bin \
  --setenv LANG C.UTF-8 --setenv LC_ALL C.UTF-8 \
  --setenv HOME /tmp --setenv TMPDIR /tmp \
  --setenv PYTHONDONTWRITEBYTECODE 1 --setenv PYTEST_DISABLE_PLUGIN_AUTOLOAD 1 \
  --tmpfs / --proc /proc --dev /dev --tmpfs /tmp \
  --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 --ro-bind /bin /bin \
  --ro-bind /root/.local/share/uv/python /root/.local/share/uv/python \
  --ro-bind /root/projects/crypto/arc-chain/venv /root/projects/crypto/arc-chain/venv \
  --ro-bind $(pwd) /sandbox/src \
  --chdir /sandbox/src \
  -- /root/projects/crypto/arc-chain/venv/bin/python -m pytest tests/arc_v3/ -q -o cache_dir=/tmp/pytest_cache
```

这条命令只运行 `tests/arc_v3/`，不代表全库通过。核查完整测试时，将最后一行的测试路径替换为 `tests/`，保留隔离参数。

### 离线命令示例

下面命令应在上述隔离环境内执行：将示例中的 Python 命令替换到 `bwrap` 的 `--` 后。输出统一写入沙箱内可写的 `/tmp/runtime-data/`；沙箱退出后临时文件会消失，需要保存时应显式挂载专用输出目录，不要放开整个仓库的写权限。

```bash
# 样例数据采集：不连接真实 RPC
/root/projects/crypto/arc-chain/venv/bin/python apps/arc_collect.py --chain-id 5042 --from-block 1000 --to-block 1010 --output-dir /tmp/runtime-data/ingest --fixture-mode

# 样例影子评估：不签名、不广播
/root/projects/crypto/arc-chain/venv/bin/python apps/arc_shadow.py --chain-id 5042 --ledger-dir /tmp/runtime-data/ledgers --fixture-mode

# 样例历史扫描：不代表真实链上历史验证
/root/projects/crypto/arc-chain/venv/bin/python apps/arc_history.py --chain-id 5042 --from-block 1000 --to-block 1005 --output-dir /tmp/runtime-data/history --fixture-mode

# 显式 JSONL 输入的离线模拟；该文件是测试样例
/root/projects/crypto/arc-chain/venv/bin/python apps/cli.py simulate --input tests/fixtures/atomic_execution/v1/e2e-stream.jsonl

# 本地审计调度器：仍须在外部 bwrap 隔离中运行
/root/projects/crypto/arc-chain/venv/bin/python tools/checks/arc_audit_local.py --evidence-dir /tmp/arc-audit-local-evidence
```

审计调度器可能因全库历史错误返回非零退出码，请检查各项日志，不要忽略失败。

## 目录说明

- `arbitrage_contracts/`：Arc 契约类型与扩展定义。
- `arc_readiness/`：网络配置、USDC 双接口记账与准入规则。
- `arc_markets/`：交易场所注册、V3 池发现与 V4 状态解码。
- `arc_ingest/`：采集传输、固定区块采样、持久化游标与原始记录。
- `arc_opportunities/`：报价接入、成本模型、账本与影子评估。
- `arc_execution/`：风险规则、授权隔离与 Nonce 日志状态机；存在此目录不代表开放实盘。
- `arc_research/`：流动性墙、已结算套利事件归因与延迟回放。
- `arc_runtime/`：单实例生命周期、熔断健康状态与报告。
- `research/`：研究模块与历史接口迁移；部分历史测试仍未完成兼容。
- `apps/`：命令行入口与应用装配。
- `docs/acceptance/`：分项验收记录，结论仅适用于各报告所列范围和版本。
- `docs/coordination/`：协作清单、候选版本说明与交付记录。

## 安全边界

- **不默认开放真实交易**：离线模拟、dry-run 和公开发布均不构成签名、授权或广播许可。网络上线日期也不会自动授予实盘权限。
- **禁止伪造盈利**：样例输出、模型估算和实际链上收益必须区分；缺少报价或成本数据时不能用默认值冒充有效结果。
- **滑点与最低输出**：滑点不可为零；执行最低输出必须覆盖本金、向上取整的 gas 成本和至少 1 atom 盈余。普通滑点折扣不能替代这一要求。
- **金额上限**：单笔上限为 500 USD；探路模式上限为 1 USD。规则存在不代表执行已开放。
- **USDC 不重复记账**：区分原生 18 位精度与 ERC-20 6 位精度，避免重复扣费。
- **V4 状态严格解码**：Slot0 按 4 个 word（128 字节）处理，正确解析有符号 tick，截断数据拒绝使用。
- **损坏记录停止追加**：已有清单损坏或会话中断时拒绝继续写入，须先检查恢复，不能覆盖历史证据。
- **文件检查有边界**：软链接检查和叶节点 `O_NOFOLLOW` 不等于消除所有竞态；本地高权限进程并发替换父目录仍属于操作系统层面的风险。

## 许可证

本项目采用 [MIT 许可证](LICENSE)，允许使用、修改、分发和商业使用；分发时须保留版权及许可声明。软件按原样提供，不附带担保。第三方依赖及另有许可声明的内容仍遵循各自许可证。

## 联系作者

项目交流与反馈：**[@0xdcc](https://x.com/0xdcc)**。
