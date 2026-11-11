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

### Timeframe

Strategies trade **4-hour bars** (`--timeframe` on the runner and supervisor; `1h` and `1d` also
work). Kraken charges up to 0.26 % per side, so a round trip costs about 0.6 % with slippage. On
1-hour bars every strategy lost money after those costs and the independent reviewer rejected all
of them. On 4-hour bars some strategies pass the same, unchanged review standards. Passing the
review does not guarantee profits: a paper trade still needs a live signal, both reviews and every
gateway limit.

### Learning over time

- **Candle history.** Kraken returns only the newest 720 candles. The runner merges every
  download into `state/history/<pair>_<timeframe>.csv` (up to 6,000 rows each), so the
  backtests, the ML model, the parameter optimiser and the RL simulator use up to 3,000 candles
  as the history grows. Stored candles before a gap of more than three missing candles are
  dropped, so a computer that was off for a long time never joins two distant stretches.
- **Track record.** Every long signal is written once per agent, pair and candle to
  `state/signal_ledger.json`. Six candles later (one day on 4-hour bars) it is scored against
  the market's move after 0.62 % round-trip costs. Each agent's hit rate, shrunk towards 50 %,
  becomes a vote weight between 0.75 and 1.25 in signal aggregation. A good record can raise a
  vote, but never above the strongest confidence a supporting agent reported. The reviews and
  gateway limits are unchanged. The Performance tab shows the record.

### Running non-stop

`ai_team/system.sh` and `ai_team/system.ps1` start one **supervisor** (`python -m ai_team.supervisor`).
It runs the gateway, the runner and the dashboard as child processes and restarts any of them that
exits, with a back-off from 2 s up to 5 min. Its status is in `ai_team/state/supervisor.json` and its
log in `supervisor.log`. "Stop" creates `ai_team/state/supervisor.stop`, so the children are always
shut down cleanly, including on Windows. Bus messages and coordinator decisions are trimmed to their
newest 20,000 lines, so the disk does not fill up.

### Windows

Double-click these files in the repository root:

| File | What it does |
|------|--------------|
| **`PORNESTE-AI-TELEFON.bat`** | Installs the packages on first run, starts the supervisor with the dashboard open to the local Wi-Fi, and prints the link for the phone |
| **`PORNIRE-AUTOMATA.bat`** | Starts the system automatically at every Windows login (a hidden script in the Startup folder). It offers to turn off sleep while on mains power, then starts the system |
| **`OPRESTE-PORNIREA-AUTOMATA.bat`** | Removes the automatic start |
| **`OPRESTE-AI.bat`** | Stops everything |
| **`CONFIGUREAZA-TELEGRAM.bat`** | Connects the bot to your Telegram once (see below) |

They call `ai_team/system.ps1` (`start|stop|status|telefon|local|autostart|fara-autostart`),
written for Windows PowerShell 5.1. Python 3.11+ must be installed with "Add python.exe to PATH".
The computer must stay on and must not sleep. These scripts have not yet been run on a real
Windows machine.

### Telegram notifications (optional)

The supervisor also runs `ai_team.notifier`. Once Telegram is configured it sends a message for
every paper buy and sell (price, amount, stop-loss / take-profit, profit) and a weekly report
(account value, the week's change, closed trades by strategy, open positions and the agents with
the best signal record). Rejected proposals are not sent.

Set it up once with `CONFIGUREAZA-TELEGRAM.bat` (Windows) or `ai_team/system.sh telegram`:
create a bot with @BotFather, paste its token, send `/start` to your bot and press Enter.
`ai_team/system.sh raport` sends the weekly report immediately.

The token is stored only on that computer, in `~/.ai-trading-desk/telegram.json`, outside the
repository, so downloading a new version keeps it. It is never logged or printed, error
messages replace it with `<token>`, and the agents are denied reads of that file. The
notifier only reads the gateway's decisions and portfolio.

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
| `ai_team.runner` | – | Kraken tickers every 10 s (`state/market.json`). Every 5 min the LangGraph coordinator invokes all 50 agents for each pair on 4-hour bars and submits approved-for-review proposals to the gateway |
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

## Public data feeds

These need no account and no key. Headlines and other feed text are treated as data only.

| Feed | Used by |
|------|---------|
| RSS: CoinDesk, Cointelegraph, Decrypt | news-analyst, then sentiment-analyst and event-driven-specialist |
| CoinGecko `/coins/markets`, `/global` | fundamental-analyst, macro-economic-analyst |
| Coinbase Exchange public tickers | arbitrage-strategist (Kraken vs Coinbase, net of fees) |
| Deribit public option summaries (BTC, ETH) | options-strategist |

## Capability status (verified, not assumed)

`python -m ai_team.cli verify` runs the coordinator over all 50 and compares
**observed** results with the declared status. See `VERIFICATION.md`:

- **41 functional**: real computations on market data and the public feeds.
- **9 partial** (they work, with a stated limitation):
  - sentiment-analyst and event-driven-specialist: lexicon scoring of headlines;
  - macro-economic-analyst: crypto-wide data only, no rates or CPI;
  - scalping-strategist: REST polling;
  - pairs-trading-strategist: long leg only;
  - order-routing-specialist: one paper venue;
  - arbitrage-strategist and options-strategist: analysis only, and options cover BTC/ETH only;
  - reinforcement-learning-researcher: research only. It trains tabular Q-learning in a built-in,
    cost-aware simulator and scores it out of sample against buy-and-hold. There is no deep-RL
    framework, and it emits no trade signals.

On live data, all 50 are online. A partly supported agent counts as online when it succeeded
on at least one pair in the latest cycle.

External frameworks: LangGraph runs the coordinator graph and scikit-learn the ML
researcher.
TradingAgents, FinRobot, RD-Agent, Qlib and NautilusTrader are **not installed**.
`python -m ai_team.cli integrations` shows which agents need each one. None of
them is simulated.
