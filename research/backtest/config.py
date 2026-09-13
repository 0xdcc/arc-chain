"""回测全局配置参数 (历史研究兼容).

【历史研究兼容性声明 / Historical Research Compatibility Notice】
本模块配置移植自原回测套件与离线研究上游：
- 上游来源: /root/projects/crypto/v2-modular/dex-sniper-engine-modular/backtest/config.py
  (SHA-256: 779957df663dff55c1798ce0a9f538743126f578b9b8b056157fbe19e2c60a5e)

定义金额档位、延迟档位、摩擦系数、流动性红线以及清洗规则的默认参数。
专供纯离线回测与历史因果复盘使用。
零网络、零 RPC、零私钥、零执行器。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BacktestConfig:
    """回测执行与风控参数配置."""

    # 跟单金额档位 (USD)
    trade_amounts: list[float] = field(default_factory=lambda: [100.0, 300.0, 1000.0])

    # 跟单延迟档位 (秒)
    delays: list[int] = field(default_factory=lambda: [3, 5, 10, 30, 180])

    # 摩擦模型参数 (单边手续费 0.6%，实盘实测口径)
    fee_rate: float = 0.006
    impact_factor: float = 0.5

    # 流动性红线 (USD)
    low_liquidity_threshold: float = 50000.0

    # 数据清洗参数
    min_cost: float = 20.0  # 成本 < $20 剔除 (小额诱饵/退款)
    max_return: float = 20.0  # 单笔收益 > 2000% 剔除 (Relay虚假配对)
    max_price_deviation: float = 5.0  # 成交价 vs 市场价偏离 > 5倍 剔除
    fallback_friction_target: float = -0.017  # fallback 假价格收益特征值
    fallback_friction_tolerance: float = 0.005  # |R - (-0.017)| < 0.005
    min_unit_price: float | None = None  # 单价 < $0.01 剔除 (Meme 币场景默认关闭以防误杀)

    # 目标链 ID (默认 Robinhood = 4663)
    target_chain_id: int = 4663

    # 价格数据源 (False: GeckoTerminal 分钟级 K 线; True: 链上秒级 Tick)
    use_tick: bool = False
