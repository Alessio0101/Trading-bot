# AI trading team: 50 specialists, a coordinator and an independent risk gateway

**Paper trading only.** No component in this system can place a live order.

```
                     ┌──────────────────────────────┐
  you / Claude Code ─▶  trading-coordinator         │  .claude/agents/00-trading-coordinator.md
                     │  (assign · parallel · compare · review · log)
                     └──────┬───────────────▲───────┘
                Agent tool  │               │ reports (JSON)
           ┌────────────────▼───────────────┴────────────────┐
           │ 50 specialist subagents  .claude/agents/NN-*.md  │
           │ each runs: python -m ai_team.cli agent <id>      │
           │ → ai_team/specialists/*.py (real computations)   │
           └────────────────┬─────────────────────────────────┘
                            │ proposals only (no orders, no credentials)
                 POST /proposals (HTTP, fail-closed)
           ┌────────────────▼─────────────────────────────────┐
           │ risk_gateway/  separate process, own limits,     │
           │ own price feed, required independent reviews,    │
           │ paper broker. No route can change limits.        │
           └──────────────────────────────────────────────────┘
```

## Layout

| Path | What it is |
|------|------------|
| `.claude/agents/` | 51 Claude Code subagent definitions (coordinator + 50 specialists), generated from `ai_team/specs.py` |
| `.claude/settings.json` | Project permissions: deny edits to `risk_gateway/**` and settings, deny reads of credentials, deny `docker`, `curl`, `wget`, `git push` and starting a gateway |
| `ai_team/specs.py` | Single source of truth: roles, inputs/outputs, tools, integrations, workflows, honest capability status |
| `ai_team/specialists/` | Python implementation behind every specialist |
| `ai_team/coordinator.py` | Coordinator: parallel stages, workflow hand-offs, reviews, gateway submission, decision log, LangGraph graph |
| `ai_team/integrations.py` | Detection adapters for TradingAgents, FinRobot, RD-Agent, Qlib, LangGraph, NautilusTrader, freqtrade |
| `ai_team/dashboard/` | Read-only web dashboard |
| `ai_team/verify.py` | Verification: files, permissions and a real invocation of all 50 |
| `risk_gateway/` | Independent risk gateway and paper broker |

## Running the whole system

```bash
ai_team/system.sh start     # creates .venv, installs ai_team/requirements.txt, starts everything
ai_team/system.sh status    # processes, paper mode, agents online, Kraken feed age, alerts
ai_team/system.sh logs      # follow the runner log
ai_team/system.sh stop      # stops dashboard, runner and gateway
ai_team/system.sh verify    # test suite + 50-agent verification
ai_team/system.sh telefon   # open the dashboard to phones on the same Wi-Fi (no password)
ai_team/system.sh local     # back to this computer only (default)
```

### From your phone

Run `ai_team/system.sh telefon` on the computer. It prints an address such as
`http://192.168.1.15:8800`; open it on a phone connected to the **same Wi-Fi**. There is no
password, so anyone on that Wi-Fi can **see** the dashboard. The dashboard is read-only and
cannot place orders or change anything, and the risk gateway stays on 127.0.0.1. Do not
forward port 8800 on your router. On Windows, allow Python through the firewall for private
networks if the page does not load. `ai_team/system.sh local` closes it again.

Dashboard: **http://127.0.0.1:8800**. It updates live via Server-Sent Events and falls back to
polling every 5 s.

| Process | Port | What it does |
|---------|------|--------------|
| `risk_gateway.server` | 8790 | Independent paper broker. Prices every decision from its own Kraken ticker, fails closed on stale prices, executes stop-loss / take-profit exits every 15 s |
| `ai_team.runner` | – | Kraken tickers every 10 s (`state/market.json`). Every 5 min the LangGraph coordinator invokes all 50 agents for each pair and submits approved-for-review proposals to the gateway |
| `ai_team.dashboard.server` | 8800 | Read-only web UI built from the runner's state files and the gateway API |

Environment overrides: `AI_TEAM_DATA=synthetic` (offline), `AI_TEAM_INTERVAL=300`,
`AI_TEAM_GATEWAY_PORT`, `AI_TEAM_DASHBOARD_PORT`. Everything binds to 127.0.0.1. Kraken's
public API needs no key, and the system holds no credentials.

The dashboard has these tabs:
- **Overview:** KPIs, alerts, consensus signals, live prices and recent decisions.
- **Agents:** all 50, with status, task, analysis, signals, errors and performance. Click an agent for its full report.
- **Signals:** the BUY / SELL / HOLD / N/A board.
- **Market:** Kraken last price, bid, ask, spread, daily change, 24 h range, volume and trend.
- **Portfolio & Trades:** equity curve, positions, fills and closed trades.
- **Risk Gateway:** health, limits, exposure, every decision and the blocked trades.
- **Coordinator:** workflows with hand-off counts, decisions and hand-offs.
- **Performance:** system, strategy review and agent statistics.
- **Logs & Health:** runner status, integrations, errors and the log tail.

## Single commands

```bash
python -m ai_team.cli --data kraken cycle --langgraph    # one coordinator cycle
python -m ai_team.cli agent liquidity-analyst --upstream volume-analyst
python -m ai_team.cli verify                             # verification report
```

With Claude Code, open this folder, accept the workspace trust prompt once (so the
allow rules in `.claude/settings.json` apply), and then either:

```bash
claude --agent trading-coordinator      # the coordinator is the main agent
```

or ask Claude Code to "use the volume-analyst subagent". Single specialist:
`python -m ai_team.cli agent liquidity-analyst --upstream volume-analyst`.

Use `--data synthetic` to work offline. Kraken's public API needs no key.

## Collaboration workflows

| Workflow | Chain |
|----------|-------|
| news_sentiment_event | news-analyst → sentiment-analyst → event-driven-specialist |
| volume_liquidity_orderflow | volume-analyst → liquidity-analyst → order-flow-analyst |
| volatility_sizing_portfolio | volatility-analyst → position-sizing-specialist → portfolio-risk-analyst |
| research_backtest_review | quantitative-researcher → backtesting-specialist → independent-risk-reviewer |
| data_features_ml | data-engineer → feature-engineer → machine-learning-researcher |
| liquidity_slippage_cost | liquidity-analyst → slippage-analyst → transaction-cost-analyst |

Each hand-off is a message on the bus (`ai_team/state/messages.jsonl`) and the
upstream report is fed into the downstream calculation. Separate CLI runs do the
same through `--upstream <agent-id>`.

## Safety model

1. **Gateway is independent:** it runs as its own process, reads `limits.json` once at
   start (SHA-256 recorded on every decision), and prices orders from its own feed.
   There is no route to change limits, and `mode` must be `paper` or it refuses to start.
2. **No bypass:** a proposal is approved only if `submitted_by` is the coordinator, both
   `backtesting-specialist` and `independent-risk-reviewer` approved it, neither of
   them is among its supporters, it contains no unknown fields, and it satisfies every
   limit (confidence, stake, exposure, open positions, stop distance, price deviation,
   daily loss, drawdown, allowed pairs).
3. **Fail closed:** if the gateway is unreachable, every proposal counts as rejected,
   and the LangGraph graph switches to research-only mode.
4. **No credentials for agents:** the paper broker needs none, and reads of
   `bot_settings.json`, `.env` and secret files are denied to every agent.
5. **Least privilege:** research, strategy, risk and execution agents get only Read, Grep,
   Glob and Bash for the CLI. Only the 2 engineering agents can edit (strategy code,
   never the gateway). Only the coordinator has the Agent tool. No agent has GitHub
   merge, push or delete tools.

Known limit: Bash is allowed for `python -m ai_team.cli`. A different command needs
approval, which is denied in headless mode and prompted in interactive mode. If you
approve arbitrary commands, an agent could write files. The gateway still reads its
limits only at start-up, records their hash on every decision, and the verifier and
tests detect any change.

## Capability status (verified, not assumed)

`python -m ai_team.cli verify` runs the coordinator over all 50 and compares
**observed** results with the declared status. See `VERIFICATION.md`:

- **38 functional**: real computations on market data.
- **6 partial** (they work, with a stated limitation): news-analyst and
  sentiment-analyst (headlines only, lexicon scoring), event-driven-specialist,
  scalping-strategist (REST polling only), pairs-trading-strategist (long leg only),
  order-routing-specialist (single paper venue).
- **6 need an integration** (they report this instead of inventing output):
  macro-economic-analyst, fundamental-analyst, machine-learning-researcher,
  arbitrage-strategist, options-strategist, reinforcement-learning-researcher.

External frameworks: LangGraph is used when installed (the coordinator graph).
TradingAgents, FinRobot, RD-Agent, Qlib and NautilusTrader are **not installed**.
`python -m ai_team.cli integrations` shows which agents need each one. None of
them is simulated.
