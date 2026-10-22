<div align="center">

# Elliott Waves

**Rule-based Elliott Wave analysis and automated BTCUSDT futures trading.**

[Star this repo](https://github.com/alsgyu/elliott-wave-auto-trading) · [Run locally](#run-locally)

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Binance USDⓈ-M](https://img.shields.io/badge/Market-Binance%20USD%E2%93%88--M-F0B90B?logo=binance&logoColor=111)](https://www.binance.com/)
[![Paper first](https://img.shields.io/badge/Trading-paper--first-16856a)](#run-locally)

</div>

## Real BTC chart examples

These are real BTC market charts published by third parties on TradingView, not mockups or output from this app. Each image links to its source and retains TradingView attribution. Each count is the author's dated interpretation, not independent proof of accuracy; this project does not claim a win rate.

<p align="center"><a href="https://es.tradingview.com/chart/BTCUSD/mJZYXC4F/"><img src="https://s3.tradingview.com/m/mJZYXC4F_mid.webp" alt="WhiteBIT TradingView BTC/USD chart with Elliott impulse and corrective wave labels across 2025–2026 price action" width="100%" /></a></p>
<p align="center"><b>Impulse and correction in BTC/USD</b><br />WhiteBIT on TradingView · Published Mar 12, 2026 · BTC price history spanning 2025–2026 · <a href="https://es.tradingview.com/chart/BTCUSD/mJZYXC4F/">Original chart and analysis</a></p>

<p align="center"><a href="https://www.tradingview.com/chart/BTCUSD/mO5I6PzE-Bitcoin-Is-the-WXYXZ-Correction-Setting-Up-Wave-C/"><img src="https://s3.tradingview.com/m/mO5I6PzE_mid.webp" alt="TradingView BTC/USD chart showing a conditional WXYXZ correction count and possible upward C wave" width="100%" /></a></p>
<p align="center"><b>Conditional bullish scenario</b><br />KlejdiCuni on TradingView · Published Feb 13, updated Feb 15, 2026 · Possible C-wave advance after a WXYXZ correction · <a href="https://www.tradingview.com/chart/BTCUSD/mO5I6PzE-Bitcoin-Is-the-WXYXZ-Correction-Setting-Up-Wave-C/">Original chart and analysis</a></p>

<p align="center"><a href="https://www.tradingview.com/chart/BTCUSD/6u85i1Lm-No-Clear-Signal-That-Bitcoin-Has-Finished-Dropping/"><img src="https://s3.tradingview.com/6/6u85i1Lm_mid.png" alt="TradingView weekly BTC/USD chart showing a completed five-wave advance and a proposed A-B-C correction" width="100%" /></a></p>
<p align="center"><b>Bearish weekly alternative</b><br />BrightRally Research on TradingView · Weekly BTC/USD · Published Mar 24, updated Apr 27, 2026 · An independent author's ABC correction scenario, not a verified system result · <a href="https://www.tradingview.com/chart/BTCUSD/6u85i1Lm-No-Clear-Signal-That-Bitcoin-Has-Finished-Dropping/">Original chart and analysis</a></p>

## Core features

1. **Multi-timeframe wave analysis** across 1W, 1D, 4H, 1H, 30m, and 15m, with ranked counts and alternatives.
2. **Automatic Elliott charting** with impulse and correction labels, confirmed pivots, lower-degree structure, Fibonacci references, and invalidation levels.
3. **Automated BTCUSDT trading** with paper-first execution, configurable strategy profiles, and a local decision and trade journal.
4. **Risk controls** for leverage, position sizing, stops, exposure, daily loss limits, and exchange account checks.

## Run locally

Requires Python 3.11+ and internet access for Binance public market data.

```bash
git clone https://github.com/alsgyu/elliott-wave-auto-trading.git
cd elliott-wave-auto-trading
./start.sh
```

Open <http://127.0.0.1:8765/>. The app starts in paper mode; press **Start** in the interface to begin simulated trading. No API key is needed for paper mode.

> Elliott counts are interpretations, not guaranteed forecasts. Start in paper mode and verify every live account setting before enabling real orders.
