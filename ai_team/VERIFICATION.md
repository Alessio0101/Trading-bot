# AI trading team - verification report

Overall: **PASS**

## 1. Agent files
- Present: 51/51 (50 specialists + coordinator)
- Definition/permission problems: 0
- Duplicate names: none
- Settings deny rules complete: True

## 2. Coordinator invocation (real run, synthetic data, temporary paper gateway)
- Specialists invoked: 50/50
- Observed: ok=44, needs_integration=6, error=0, not_invoked=0
- Gateway submissions: 3 (approved 3)
- Bypass attempt (non-coordinator, no reviews, override field): rejected - ["unknown fields rejected: ['override_limits']"]
- Decisions logged: 24, bus messages: 366

## 3. Workflows
- news_sentiment_event: hand-offs OK; statuses {'news-analyst': 'ok', 'sentiment-analyst': 'ok', 'event-driven-specialist': 'ok'}
- volume_liquidity_orderflow: hand-offs OK; statuses {'volume-analyst': 'ok', 'liquidity-analyst': 'ok', 'order-flow-analyst': 'ok'}
- volatility_sizing_portfolio: hand-offs OK; statuses {'volatility-analyst': 'ok', 'position-sizing-specialist': 'ok', 'portfolio-risk-analyst': 'ok'}
- research_backtest_review: hand-offs OK; statuses {'quantitative-researcher': 'ok', 'backtesting-specialist': 'ok', 'independent-risk-reviewer': 'ok'}
- data_features_ml: hand-offs OK; statuses {'data-engineer': 'ok', 'feature-engineer': 'ok', 'machine-learning-researcher': 'needs_integration'}
- liquidity_slippage_cost: hand-offs OK; statuses {'liquidity-analyst': 'ok', 'slippage-analyst': 'ok', 'transaction-cost-analyst': 'ok'}

## 4. Per-agent result

| # | Agent | Declared | Observed | Consistent | Latest output / gap |
|---|-------|----------|----------|------------|---------------------|
| 1 | technical-analyst | functional | ok | yes | SOL/USDT trend up (bias +1.00), RSI 88.5 overbought |
| 2 | chart-pattern-analyst | functional | ok | yes | Support 2396.55, resistance 2733.51, structure higher-highs/higher-lows |
| 3 | news-analyst | partial | ok | yes | Classified 2 headlines |
| 4 | sentiment-analyst | partial | ok | yes | Sentiment +1.00 over 2 headlines |
| 5 | macro-economic-analyst | needs_integration | needs_integration | yes | No macro data source connected (needs FinRobot data tools or a FRED key). |
| 6 | fundamental-analyst | needs_integration | needs_integration | yes | No on-chain data provider connected. |
| 7 | volume-analyst | functional | ok | yes | Relative volume 1.02x, z +0.05, OBV rising |
| 8 | liquidity-analyst | functional | ok | yes | Spread 2.00 bps, depth ±1% 329,408,739 quote, score 0.98 |
| 9 | order-flow-analyst | functional | ok | yes | Trade imbalance +0.07, book imbalance +0.47, flow bias +0.23 |
| 10 | market-regime-analyst | functional | ok | yes | Regime trending/calm (efficiency 0.61, vol pct 26%) |
| 11 | quantitative-researcher | functional | ok | yes | Best hypothesis momentum_24 (IC +0.069); 4/5 strategies have positive-IC support: ['moment |
| 12 | statistical-arbitrage-researcher | functional | ok | yes | BTC/USDT~ETH/USDT: beta 1.079, z -0.02, half-life 18.6 bars |
| 13 | factor-researcher | functional | ok | yes | Top factor rank: SOL/USDT |
| 14 | machine-learning-researcher | needs_integration | needs_integration | yes | Model training is delegated to Qlib/RD-Agent, which are not installed. |
| 15 | backtesting-specialist | functional | ok | yes | Backtested 5 strategies on 720 bars; best trend_following (Sharpe 15.15) |
| 16 | data-engineer | functional | ok | yes | 500 rows from synthetic, quality OK, gaps 0 |
| 17 | feature-engineer | functional | ok | yes | Built 6 features x 451 rows |
| 18 | correlation-analyst | functional | ok | yes | Average correlation 0.70; 0 highly correlated pairs |
| 19 | trend-following-strategist | functional | ok | yes | trend_following: long; 1 proposal(s) |
| 20 | mean-reversion-strategist | functional | ok | yes | mean_reversion: flat; 0 proposal(s) |
| 21 | momentum-strategist | functional | ok | yes | momentum: long; 1 proposal(s) |
| 22 | breakout-strategist | functional | ok | yes | breakout: long; 1 proposal(s) |
| 23 | event-driven-specialist | partial | ok | yes | Event view: sentiment +1.00, tags ['adoption', 'listing', 'regulation'], veto=False |
| 24 | swing-trading-strategist | functional | ok | yes | swing: flat; 0 proposal(s) |
| 25 | scalping-strategist | partial | ok | yes | scalping (1m, polling only): flat |
| 26 | arbitrage-strategist | needs_integration | needs_integration | yes | Only one venue (Kraken public) is reachable; needs a second venue adapter. |
| 27 | options-strategist | needs_integration | needs_integration | yes | No options venue/data connected (e.g. Deribit) and paper broker is spot-only. |
| 28 | pairs-trading-strategist | partial | ok | yes | Pair BTC/USDT/ETH/USDT z=-0.02; 0 proposal(s) |
| 29 | execution-trader | functional | ok | yes | Plan: buy 0.366228 SOL/USDT @ 2728.84 IOC (paper) |
| 30 | order-routing-specialist | partial | ok | yes | Routed to 'paper' (only venue available; live venues are disabled) |
| 31 | slippage-analyst | functional | ok | yes | Expected slippage 0.00 bps for 100.00 quote |
| 32 | transaction-cost-analyst | functional | ok | yes | Round-trip cost 54.0 bps vs edge 150 bps |
| 33 | market-microstructure-analyst | functional | ok | yes | Microprice -0.43 bps from mid |
| 34 | algorithmic-execution-specialist | functional | ok | yes | VWAP schedule: 6 child orders over 6h |
| 35 | trade-reconciliation-specialist | functional | ok | yes | Reconciled 0 approvals vs 3 fills: MISMATCH |
| 36 | volatility-analyst | functional | ok | yes | Annualised vol 67%, ATR 1.07%, vol percentile 5% |
| 37 | position-sizing-specialist | functional | ok | yes | Stake 999.07 (risk 1.0% of 9991, stop 2.15%) |
| 38 | portfolio-risk-analyst | functional | ok | yes | Gross exposure 40% of equity, 1-bar VaR95 41.78, within advisory limits |
| 39 | drawdown-monitor | functional | ok | yes | Drawdown now -0.09%, worst -0.09%; pause=False |
| 40 | stress-testing-specialist | functional | ok | yes | Worst scenario loss -698.07 on gross 1994.49 |
| 41 | independent-risk-reviewer | functional | ok | yes | Approved 4/5 strategies: ['momentum', 'breakout', 'trend_following', 'swing'] |
| 42 | compliance-officer | functional | ok | yes | Compliance PASS (5/5) |
| 43 | algorithmic-trading-engineer | functional | ok | yes | 1 freqtrade strategies found, all valid=True |
| 44 | strategy-deployment-engineer | functional | ok | yes | Deployment checks 4/4 passed |
| 45 | signal-aggregation-specialist | functional | ok | yes | 1 consensus proposal(s) from 4 signals |
| 46 | parameter-optimisation-specialist | functional | ok | yes | Best EMA 8/80: IS Sharpe 12.21, OOS 22.85 (gap -10.64) |
| 47 | model-monitoring-specialist | functional | ok | yes | KS returns 0.105, volume 0.060 (crit 0.120); drift=False |
| 48 | performance-attribution-analyst | functional | ok | yes | Attributed -1.49 P&L across 1 strategies |
| 49 | system-health-monitor | functional | ok | yes | Health OK: {'gateway_ok': True, 'data_fresh': True, 'no_failing_agents': True} |
| 50 | reinforcement-learning-researcher | needs_integration | needs_integration | yes | No RL framework/simulator installed (freqtrade freqai-rl, Qlib RL or NautilusTrader). |

## 5. External integrations

- TradingAgents: NOT installed - used by 5 agent(s)
- FinRobot: NOT installed - used by 5 agent(s)
- RD-Agent: NOT installed - used by 5 agent(s)
- Qlib: NOT installed - used by 6 agent(s)
- LangGraph: installed unknown - used by 1 agent(s)
- NautilusTrader: NOT installed - used by 8 agent(s)
- freqtrade (paper bot): installed 2026.11-dev-61c789f - used by 2 agent(s)
