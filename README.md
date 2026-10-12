<div align="center">

# Elliott BTC

**Rule-based Elliott Wave analysis for BTCUSDT perpetuals.**
<br />Multi-timeframe charts, explicit invalidation levels, risk controls, and a local-first trading workstation.

[English](README.md) · [한국어](docs/README.ko.md)

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Binance USDⓈ-M](https://img.shields.io/badge/Market-Binance%20USD%E2%93%88--M-F0B90B?logo=binance&logoColor=111)](https://www.binance.com/)
[![Local first](https://img.shields.io/badge/Mode-local--first-16856a)](#privacy-and-security)

</div>

<p align="center">
  <img src="docs/images/analysis-sample.png" alt="Elliott BTC analysis dashboard with multi-timeframe candidates and a labeled wave chart" width="100%" />
</p>
<p align="center"><sub>Desktop interface · synthetic sample market data · no exchange account connected</sub></p>

> Elliott counts are interpretations, not probabilities. The fit score ranks rule-compatible structures; it is not a win rate. This project makes no claim of high predictive accuracy or guaranteed returns.

## What it does

- Reads BTCUSDT perpetual candles across **1W, 1D, 4H, 1H, 30m, and 15m**. The selected strategy determines which intervals may initiate a trade; smaller intervals are not automatically treated as better signals.
- Builds deterministic Elliott-wave candidates for impulse waves and zigzag corrections. It records the observed wave direction separately from the candidate's projected direction.
- Shows confirmed pivots, lower-degree subdivision evidence, alternative counts, invalidation, candidate stop and target levels, and Fibonacci references on a scrollable chart.
- Applies explicit gates for structure, higher-timeframe context, breakout confirmation, fees, slippage, spread, minimum order size, and account risk before considering an entry.
- Records decisions and execution events in a local SQLite database, with Markdown trade records for entry, exit, and recovery events.
- Starts in **paper mode**. Starting the local server does not start trading; the operator must press **Start** in the interface.

## Screenshots

The screenshots below come from the running application. The 4H and 30m views are paper-mode captures; the manual chart and settings views use local sample data.

<table>
  <tr>
    <td width="50%"><a href="docs/images/waves-4h-paper.png"><img src="docs/images/waves-4h-chart.png" alt="Four-hour Elliott wave chart with confirmed pivots and alternative counts" /></a><br /><sub>4H · swing structure and alternative count</sub></td>
    <td width="50%"><a href="docs/images/waves-30m-paper.png"><img src="docs/images/waves-30m-chart.png" alt="Thirty-minute Elliott wave chart with an ABC correction count and Fibonacci levels" /></a><br /><sub>30m · ABC candidate and Fibonacci references</sub></td>
  </tr>
  <tr>
    <td><img src="docs/images/manual-levels.png" alt="Manual chart review with entry, stop, invalidation, and Fibonacci target lines" /><br /><sub>Manual review · hypothetical entry, stop, invalidation, and target references</sub></td>
    <td><img src="docs/images/risk-settings-sample.png" alt="Risk settings for capital, leverage, trade risk, and daily limits" /><br /><sub>Risk controls · capital, leverage, and loss limits</sub></td>
  </tr>
</table>

## Timeframe logic

The default strict profile is intentionally selective:

| Interval | Role |
| --- | --- |
| 1W | Higher-timeframe opposition veto |
| 1D | Directional context |
| 4H | Primary 3rd/5th-wave candidate |
| 1H | Lower-degree structure and closing-breakout confirmation |
| 30m | Context or confirmation in selected profiles; not a standalone signal by default |
| 15m | Internal confirmation for the optional short-term profile; no independent entry |

Optional multi-candidate and short-term profiles compare more structures, but the project has not demonstrated that they improve returns. A strict rule order resolves conflicts; an unclear or incomplete count can correctly result in `WAIT`.

## Research, not a performance claim

The exploratory 30-day replay documented in [`docs/ELLIOTT_RESEARCH_VERIFICATION.md`](docs/ELLIOTT_RESEARCH_VERIFICATION.md) produced **11 closed trades**, of which **4 were winners**. Their aggregate net P&L was **+0.2211 USDT** across three separately reset 10-day windows starting at 1,000 USDT. Repricing those same trades at twice the assumed fees and adverse slippage produced **−4.1335 USDT**.

This was a small, development-time sample reused while building the strategy, not an untouched holdout. One additional position remained open at a fold boundary. These results do not establish a durable edge, a reliable win rate, or high accuracy. The fit score is not statistically calibrated.

The underlying market-data snapshot and fold-level replay files are local research artifacts and are not included in this repository. The figures above are a documented summary, not a clean-clone reproduction.

## Run locally

Requirements: macOS or Linux, Python 3.11+, and an internet connection for the first-run certificate dependency and public Binance market data.

```bash
git clone https://github.com/alsgyu/Elliot.git
cd Elliot
./start.sh
```

Open <http://127.0.0.1:8765/>. The launcher creates a local virtual environment, installs the pinned certificate package if needed, and starts the app in paper mode. Press **Start** in the UI only when you want paper monitoring and simulated execution. Stop the server with `Ctrl+C` in its terminal.

To run an isolated synthetic sample without exchange market data:

```bash
./start.sh --sample --port 8766 --data data/elliott-sample
```

The sample mode is clearly labeled and cannot be switched to demo or live trading. Each runtime mode uses separate local state.

## Exchange access

Public market data works without API credentials. Binance Demo and live execution require a separately enabled USDⓈ-M Futures account and API key. For a live key, grant only **Reading + Futures**, restrict access to the trusted public IP of the machine running the app, and leave withdrawals and transfers disabled. The account checks expect supported USDⓈ-M, single-asset, one-way, isolated-margin settings.

The app has a live order path, but this repository's documented replay and UI checks are not proof of exchange fills or profitability. Check account eligibility and the exchange's current API rules before use. Never paste an API secret into an issue, README, or source file.

## Risk and operating limits

- Leverage is configurable from 1× to 10×; the default is 1×. Position size is constrained by configured risk, notional limits, exchange filters, and available account state.
- Daily and total loss limits, entry caps, cooldowns, loss-streak pauses, and duplicate-entry checks are enforced by the engine.
- Entries use market orders after signal confirmation. Stop and take-profit protection are submitted to the exchange for supported live positions. A stop is not a guaranteed fill price or a maximum-loss guarantee.
- The chart's candidate stop and invalidation are different levels. Manual review lines are estimates and do not create or manage orders.
- Local monitoring stops when the computer sleeps, the process exits, or connectivity fails. Exchange-side protection may remain on an open live position; check the exchange directly.

## Privacy and security

The server binds to `127.0.0.1` by default. Credentials and journals stay in the selected local data directory; credential files use owner-only permissions but are **not encrypted by the application**. Protect the operating-system account and disk. Runtime databases, logs, virtual environments, and generated artifacts are excluded by `.gitignore`.

## Project map

| Path | Responsibility |
| --- | --- |
| `elliottbot/waves.py` | Pivot and wave candidate construction |
| `elliottbot/strategy.py` | Timeframe hierarchy, ranking, and entry gates |
| `elliottbot/risk.py` | Position sizing and risk limits |
| `elliottbot/execution.py` | Order sizing, protection, and exchange execution |
| `elliottbot/journal.py` | Local event journal and trade records |
| `elliottbot/chart.py` | Chart data and Elliott annotations |
| `elliottbot/static/` | Local web interface |
| `docs/ELLIOTT_RULES.md` | Detailed rule set and calculation policies |
| `docs/ELLIOTT_VERIFICATION.md` | Runtime and implementation verification notes |

The latest verification report records 194 passing unit, regression, and HTTP tests as of 2026-10-05. It does not include authenticated end-to-end exchange execution or a profitability certification.

## Contributing

Bug reports and focused pull requests are welcome. Please include a reproducible case and distinguish a software defect from a disputed Elliott count. Do not attach API credentials, account identifiers, or private journal files.

Contributions and reproducible issue reports are welcome. Community interest is not evidence that a strategy works.

## License

This repository does not include a `LICENSE` file yet.
