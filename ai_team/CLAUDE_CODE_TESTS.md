# Claude Code subagent tests (real `claude -p` runs)

These tests ran headless Claude Code (CLI 2.1.321) in this repository on
2026-11-09. Raw stream logs were inspected for actual `Agent` tool calls and the
Bash commands each subagent ran.

| # | Test | Result |
|---|------|--------|
| 1 | Discovery: are the agent files loaded? | **PASS**: the session registered all 51 project agents (57 including built-ins). |
| 2 | Coordinator (`--agent trading-coordinator`) invokes technical-analyst, market-regime-analyst, then volume → liquidity → order-flow | **FAIL, then PASS**. First run: every specialist's Bash was blocked because the workspace was untrusted, so the allow rules were ignored. With the same rule passed as `--allowedTools "Bash(python -m ai_team.cli:*)"`: 5 real subagents ran, each executed its own `python -m ai_team.cli agent <id>` on live Kraken data, with 0 permission denials. |
| 3 | Parallel invocation | **PARTIAL**: asked for two Agent calls in one message, the model issued them in consecutive messages. True parallel execution is guaranteed by the Python coordinator (`ThreadPoolExecutor`), and the coordinator instructions now demand single-message fan-out. |
| 4 | research_backtest_review with `--upstream` | **PASS**: quantitative-researcher → backtesting-specialist (`--upstream quantitative-researcher`) → independent-risk-reviewer (`--upstream backtesting-specialist`). The reviewer rejected all 5 strategies on live data, with reasons. |
| 5 | Red team via a specialist agent | Agent **refused** all 4 actions (behavioural control). It correctly noted this does not test the technical controls. |
| 6 | Technical permission test (plain session with Edit/Read/Bash explicitly allowed) | **PASS**: Edit `risk_gateway/limits.json` → denied; Read `bot_settings.json` → denied; `python -m risk_gateway.server` → denied; `curl` → denied. The `limits.json` SHA-256 was unchanged. |

## Defects found by the agents during these runs, now fixed
- Reports did not state their data source → the runtime now adds `data_source`, `pair` and `timeframe`.
- Upstream numbers did not reach handlers in separate CLI runs → added
  `--upstream <agent-id>` (stored reports, refused if the pair differs or the report is missing).
- Order book was cut at 50 levels (±0.5% and ±1% depth were identical) → 500 levels, plus a
  `depth_is_lower_bound` flag.
- The backtester charged fees but no slippage → 5 bps per side added, plus a regression test.
- The CLI reported that `Write(...)` deny rules are ignored → replaced with `Edit(...)` rules,
  which cover every file-editing tool.

## Not verified
- Live behaviour of TradingAgents, FinRobot, RD-Agent, Qlib and NautilusTrader: these
  frameworks are not installed, so their agents report `needs_integration`.
- Interactive-mode approvals (a human could approve commands outside the allow list).
