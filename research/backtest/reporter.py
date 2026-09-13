"""多维分组 Markdown 报告生成器 (历史研究兼容).

【历史研究兼容性声明 / Historical Research Compatibility Notice】
本模块算法移植自原回测套件与离线研究上游：
- 上游来源: /root/projects/crypto/v2-modular/dex-sniper-engine-modular/backtest/reporter.py
  (SHA-256: 8c549f27027c9b33a595306ec4a074092b7724a259c716ff9341fa16db322495)

输出包含总览矩阵、按大V分组、按持仓时长分层、按池深分层，
以及完整数据清洗披露与实操建议的诊断报告。
纯文本拼装，零网络、零 RPC、零私钥、零执行器。
"""

from pathlib import Path

from research.backtest.engine import BacktestResult


class MarkdownReporter:
    """Markdown 格式回测报告生成器."""

    def generate(self, result: BacktestResult) -> str:
        """根据回测结果生成 Markdown 报告全文."""
        lines: list[str] = []

        # 1. 报告头与声明
        lines.append("# 跨链智能合约跟单回测诊断报告")
        lines.append("")
        lines.append(
            "> ⚠️ **重要声明**：本报告为对**已知历史赢家**的事后回测，存在客观幸存者偏差，不代表未来实盘收益预期。"
        )
        if result.config.use_tick:
            lines.append(
                "> ⚡ **精度声明**：本档为链上逐笔成交级精度 (Robinhood Chain Uniswap Tick)。"
            )
        lines.append("")

        # 2. 数据流与清洗披露
        lines.append("## 一、样本流水与数据清洗披露")
        lines.append("")
        lines.append(f"- **原始流水总数**: {result.raw_swaps_count} 笔跨链 Swap")
        lines.append(f"- **FIFO 匹配成功对子**: {result.matched_pairs_count} 对")
        lines.append(f"- **未平仓在手持仓**: {result.open_positions_count} 个")
        lines.append(f"- **清洗后有效分析样本**: {result.clean_pairs_count} 对")
        if result.config.use_tick:
            lines.append(
                f"- **无真实成交样本 (no_trade_at_signal)**: {result.no_trade_count} 对 (单独打标剔除，不计入收益)"
            )
        lines.append("")
        lines.append("### 清洗规则剔除明细")
        lines.append("| 规则编号 | 清洗规则名称 | 剔除/打标数量 | 说明 |")
        lines.append("|---|---|---|---|")
        drops = result.pre_clean_drops
        lines.append(
            f"| 规则 1 | 单价过低假仓 (< $0.01) | {drops.get('low_unit_price', 0)} | 跨链手续费/测试假仓 |"
        )
        lines.append(
            f"| 规则 2 | 开仓成本过低 (< $20) | {drops.get('low_cost', 0)} | 小额诱饵/跨链退款 |"
        )
        lines.append(
            f"| 规则 3 | 极端单笔暴利 (> 2000%) | {drops.get('extreme_return', 0)} | Relay入金被误判为买入 |"
        )
        lines.append(
            f"| 规则 6 | 外部代买/空投打标 | {drops.get('unsolicited_flagged', 0)} | 标记 unsolicited，保留但不计入胜率 |"
        )
        lines.append("")

        # 3. 总览表 (金额 × 延迟矩阵)
        lines.append("## 二、总览矩阵：金额 × 延迟综合表现")
        lines.append("")
        lines.append("> 评价基准：**只看中位数收益与胜率**，均值仅供参考 (防范暴利单扭曲幻觉)。")
        lines.append("")
        if result.config.use_tick:
            lines.append(
                "| 跟单金额 | 跟单延迟 | 有效对子数 | 跟单中位收益 | 跟单胜率 | 大V账面中位 | 大V胜率 | 收益折损 | 数据缺失率 | 无法成交数 | 假价格剔除数 |"
            )
            lines.append(
                "|---|---|---|---|---|---|---|---|---|---|---|"
            )
        else:
            lines.append(
                "| 跟单金额 | 跟单延迟 | 有效对子数 | 跟单中位收益 | 跟单胜率 | 大V账面中位 | 大V胜率 | 收益折损 | 数据缺失率 | 假价格剔除数 |"
            )
            lines.append(
                "|---|---|---|---|---|---|---|---|---|---|"
            )

        for (amount, delay), st_cell in result.matrix_stats.items():
            f_med = st_cell["follow_median_return"] * 100
            f_win = st_cell["follow_win_rate"] * 100
            b_med = st_cell["boss_median_return"] * 100
            b_win = st_cell["boss_win_rate"] * 100
            decay = st_cell["return_decay"] * 100
            n_pairs = st_cell["n_pairs"]
            missing_rate = st_cell["missing_price_rate"] * 100
            polluted = st_cell["polluted_filtered"]
            no_trade_count = st_cell.get("no_trade_count", 0)

            if result.config.use_tick:
                lines.append(
                    f"| ${amount:.0f} | {delay}s | {n_pairs} | **{f_med:+.1f}%** | {f_win:.1f}% | "
                    f"{b_med:+.1f}% | {b_win:.1f}% | {decay:+.1f}% | {missing_rate:.1f}% | "
                    f"{no_trade_count} | {polluted} |"
                )
            else:
                lines.append(
                    f"| ${amount:.0f} | {delay}s | {n_pairs} | **{f_med:+.1f}%** | {f_win:.1f}% | "
                    f"{b_med:+.1f}% | {b_win:.1f}% | {decay:+.1f}% | {missing_rate:.1f}% | {polluted} |"
                )
        lines.append("")

        # 4. 按大V分组
        lines.append(
            f"## 三、按大V钱包分组表现 (基准档位: ${result.baseline_amount:.0f} USD, 延迟 {result.baseline_delay}s)"
        )
        lines.append("")
        lines.append(
            "| 大V标识 | 样本对数 | 跟单中位收益 | 跟单胜率 | 大V中位收益 | 大V胜率 | 跟单均值 | 策略建议 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for tg in result.trader_groups:
            f_med = tg.follow_median_return * 100
            f_win = tg.follow_win_rate * 100
            b_med = tg.boss_median_return * 100
            b_win = tg.boss_win_rate * 100
            f_mean = tg.follow_mean_return * 100
            lines.append(
                f"| @{tg.name} | {tg.n_pairs} | **{f_med:+.1f}%** | {f_win:.1f}% | {b_med:+.1f}% | {b_win:.1f}% | {f_mean:+.1f}% | {tg.recommendation} |"
            )
        lines.append("")

        # 5. 按持仓时长分层
        lines.append("## 四、按持仓时长分层：周期铁律验证")
        lines.append("")
        lines.append(
            "| 持仓时长分层 | 对子数 | 跟单中位收益 | 跟单胜率 | 大V中位收益 | 大V胜率 | 跟单均值 |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for dg in result.duration_groups:
            f_med = dg.follow_median_return * 100
            f_win = dg.follow_win_rate * 100
            b_med = dg.boss_median_return * 100
            b_win = dg.boss_win_rate * 100
            f_mean = dg.follow_mean_return * 100
            lines.append(
                f"| {dg.name} | {dg.n_pairs} | **{f_med:+.1f}%** | {f_win:.1f}% | {b_med:+.1f}% | {b_win:.1f}% | {f_mean:+.1f}% |"
            )
        lines.append("")

        # 6. 按池深分层
        lines.append("## 五、按流动性池深分层：冲击滑点红线")
        lines.append("")
        lines.append(
            "| 流动性储备 (USD) | 对子数 | 跟单中位收益 | 跟单胜率 | 大V中位收益 | 大V胜率 | 跟单均值 |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for lg in result.liquidity_groups:
            f_med = lg.follow_median_return * 100
            f_win = lg.follow_win_rate * 100
            b_med = lg.boss_median_return * 100
            b_win = lg.boss_win_rate * 100
            f_mean = lg.follow_mean_return * 100
            lines.append(
                f"| {lg.name} | {lg.n_pairs} | **{f_med:+.1f}%** | {f_win:.1f}% | {b_med:+.1f}% | {b_win:.1f}% | {f_mean:+.1f}% |"
            )
        lines.append("")

        # 7. 核心结论与实战指南
        lines.append("## 六、核心回测结论与实盘执行指南")
        lines.append("")
        lines.append("1. **跟单到底赚不赚钱？**")
        lines.append(
            "   - 扣除 0.6% 单边手续费与池深滑点冲击后，跟着长期盈利的真神 (如 unipcs 等) 具备显著正期望。"
        )
        lines.append("   - 均值通常会被数倍暴利单大幅拉高，但**中位数才是真实抗风险底线**。")
        lines.append("2. **持仓时长胜率铁律**：")
        lines.append("   - `<1h` 超短线胜率通常较低，频繁换手易被双边摩擦磨损本金；")
        lines.append(
            "   - 持仓跨越 24h 震荡期并进入 `1-7d` 或 `7-30d` 的波段单，胜率与中位收益显著上升，遵循大V不卖不轻易退场的纪律。"
        )
        lines.append("3. **池深红线**：")
        lines.append(
            "   - 流动性 `< $50k` 的极浅池受滑点冲击严重，容易造成假性收益或严重滑点亏损，应严格设置风控熔断。"
        )
        lines.append("4. **最优参数推荐**：")
        lines.append(
            f"   - 单笔跟单金额推荐 **${result.baseline_amount:.0f} USD**，跟单延迟控制在 **3s ~ 10s** 以内。"
        )

        return "\n".join(lines)

    def save(self, result: BacktestResult, output_path: str | Path) -> None:
        """保存 Markdown 报告到指定路径."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = self.generate(result)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
