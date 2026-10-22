<div align="center">

# Elliott Waves

**Rule-based Elliott Wave analysis and automated BTCUSDT futures trading.**

[Star this repo](https://github.com/alsgyu/ElliotWaves) · [Run locally](#run-locally)

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Binance USDⓈ-M](https://img.shields.io/badge/Market-Binance%20USD%E2%93%88--M-F0B90B?logo=binance&logoColor=111)](https://www.binance.com/)
[![Paper first](https://img.shields.io/badge/Trading-paper--first-16856a)](#run-locally)

</div>

<p align="center"><img src="docs/images/elliott-timeframes.png" alt="Illustrative Elliott Wave impulse and ABC structures across 1W, 1D, 4H, 1H, 30m, and 15m timeframes" width="100%" /></p>
<p align="center"><b>Multi-timeframe map</b><br />Nested 1–5 impulse and A–B–C correction examples from weekly context to 15-minute structure.</p>

<p align="center"><img src="docs/images/elliott-complete-cycle.png" alt="Illustrative BTC-style candlestick chart labeling a complete Elliott Wave 1–5 impulse followed by an ABC correction" width="100%" /></p>
<p align="center"><b>Complete Elliott cycle</b><br />A five-wave motive sequence followed by a three-wave correction. Both images use synthetic candles for illustration, not live BTC data or verified forecasts.</p>

## Core features

1. **Multi-timeframe wave analysis** across 1W, 1D, 4H, 1H, 30m, and 15m, with ranked counts and alternatives.
2. **Automatic Elliott charting** with impulse and correction labels, confirmed pivots, lower-degree structure, Fibonacci references, and invalidation levels.
3. **Automated BTCUSDT trading** with paper-first execution, configurable strategy profiles, and a local decision and trade journal.
4. **Risk controls** for leverage, position sizing, stops, exposure, daily loss limits, and exchange account checks.

## Run locally

Requires Python 3.11+ and internet access for Binance public market data.

```bash
git clone https://github.com/alsgyu/ElliotWaves.git
cd ElliotWaves
./start.sh
```

Open <http://127.0.0.1:8765/>. The app starts in paper mode; press **Start** in the interface to begin simulated trading. No API key is needed for paper mode.

> Elliott counts are interpretations, not guaranteed forecasts. Start in paper mode and verify every live account setting before enabling real orders.
