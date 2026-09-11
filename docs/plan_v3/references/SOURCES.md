# 本版证据索引与适用范围

计划：ARC-4B-v3.1-20260911-13817f4。网页端检查日期：2026-09-11。上游固定SHA：`13817f4e027375dd59cc7a202ae641c068525f53`。

## U1：用户规范与历史上下文

`references/USER_CONTEXT_2026-09-09.md` 为用户提供的原始上下文副本。Python 3.12、独立工作区、真实动作单独批准、报价/模拟/成交口径等是业务和工程约束。里面9月9日monolith路径与dirty HEAD75d1a1d只是历史事实，不证明今天modular或arc-chain的本地状态。

## U2：用户本轮追加的本地维护约束

`references/USER_UPDATE_2026-09-11.md`记录2026-09-11T08:05:34Z的补充。本地Robinhood会继续增加V3/V4池，其他代码预期不变；允许目录差异、分开代码锁与数据快照。此为用户需求，不是网页版对服务器实际文件的认证。

## R1：实际读取的上游提交与比较

- 当前固定提交：https://github.com/dc787/dex-sniper-engine-modular/commit/13817f4e027375dd59cc7a202ae641c068525f53
- 与上一版比较：https://github.com/dc787/dex-sniper-engine-modular/compare/a6fc7f75cc475c09a09e384fc86208352b37e223...13817f4e027375dd59cc7a202ae641c068525f53
- 历史连续块扫描提交：https://github.com/dc787/dex-sniper-engine-modular/commit/0d186234ab7535f43a1ed439cdea3d67d26d7663

比较结果为两次新增提交。346池、137 V3、209 V4、5668回路、92 ERC20精度等数字来自作者提交说明；本轮没有在本地重算，也不代表Arc样本、盈利机会或执行能力。

## R2–R7：本轮读取的实际源码（均固定同一SHA）

| ID | 文件 | 用于什么判断 |
|---|---|---|
| R2 | `apps/live_pipeline.py` 全文件 | 混合V3/V4采样、历史入口与迁移陷阱 |
| R3 | `tools/update_v3_catalog.py` | immutable/decimals批读可复用，但依赖旧缓存路径与Robinhood地址 |
| R4 | `tools/export_v4_catalog.py` | 旧manifest输入、native ETH、缺hooks默认值等不可直接搬到Arc |
| R5 | `tests/test_v4_pipeline_integration.py` | 仅目录与初始化检查、固定/tmp路径、样本数字限制 |
| R6 | `AGENTS.md` | Python、测试、资金和协作边界 |
| R7 | `docs/audit/REPAIR_NOTES.md` | F01–F07迁移义务、单段报价、输出未知与账本限制 |

文件永久定位格式：`https://github.com/dc787/dex-sniper-engine-modular/blob/13817f4e027375dd59cc7a202ae641c068525f53/<上表路径>`。

本次没有重跑项目测试，没有查询用户钱包，没有测Arc当前RPC或实际池地址，没有部署或提交代码。其余W0–W7模块的复用候选来自上一版检查与上游修复说明；本地T01/T02/T43必须核对实际依赖和迁移测试，不能将这些候选理解成网页版全库复审通过。

## O1–O4：额外核对的官方资料

- O1 Arc钱包集成：https://docs.arc.io/integrate/wallets 。原生USDC18位与ERC20接口6位共用余额；allowance不限制所有原生支出；普通本地EVM不能认证全部Arc系统行为。该页示例仍使用5042002测试网，不用它认证5042某个合约部署。
- O2 Circle Arc mainnet genesis：https://github.com/circlefin/arc-node/blob/main/assets/mainnet/genesis.json 。本次读取config.chainId为5042；不能单凭一个RPC也返回5042就认定其可信。
- O3 Uniswap StateView源码：https://github.com/Uniswap/v4-periphery/blob/main/src/lens/StateView.sol 。getSlot0返回sqrtPriceX96/tick/protocolFee/lpFee；实际部署ABI/版本/manager绑定仍需核验。
- O4 Git worktree手册：https://git-scm.com/docs/git-worktree 。worktree隔离HEAD/index/工作树，但共享部分仓库状态，不等于OS权限沙箱。

O2/O3的main会变化。本地使用时保存实际commit/内容哈希/读取时间；不把可变URL作为永久部署证据。本版不重新核定9月16日公开开放安排、OTC即时溢价或任何代币价格；旧讨论中的这些数字不进入执行配置。

## 本版新设计，不是源码已有能力

四窗口M1–M4、T01–T48派单、共享.coord、BOOTSTRAP_READY、文件租约、Arc专属薄入口、历史因果标签、V4分层验收等均为本版计划设计；须由本地实现/验证。施工包自身的静态校验不等于目标工程check.sh通过。
