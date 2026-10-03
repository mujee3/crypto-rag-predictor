# Crypto AI Analyst

A Python cryptocurrency analysis system that combines historical and live market data, technical indicators, and structured OpenAI analysis. Supports multiple coins, separate major-event research, paper comparison, and optional Binance Futures execution.

> Research software. Signals and AI explanations have no established predictive accuracy or profitability. Analysis-only commands do not place orders.

## Features

- Historical price and volume analysis, with 90 days of hourly candles requested by default.
- Price changes, moving averages, RSI, ATR, volatility, VWAP, and relative volume.
- Order-book depth, walls, aggressive trades, funding, and open-interest observations.
- Conditional BTC context when measured correlations support relevance.
- Descriptive comparisons with similar historical periods.
- Structured GPT LONG / SHORT / WAIT proposals grounded in supplied numerical evidence.
- Separate research into major upcoming economic and coin-specific events.
- Multiple-coin scanning through JSON configuration.
- Optional discovery of candidates from recent social signal posts.
- Paper comparison and opt-in Binance demo/live execution with position monitoring.

## How decisions work

Two strategy modes are available:

| Mode | Proposed direction | GPT role |
|---|---|---|
| `trend` — default | Closed-candle VWAP/EMA rules | Explanatory context |
| `gpt` | Structured GPT assessment of numerical data | Directional proposal, subject to validation and risk checks |

Python fetches data and calculates the metrics before calling GPT. The GPT strategy checks evidence references, data freshness, liquidity, spread, price movement, funding, and cost-adjusted reward/risk. A proposal can become WAIT when required checks fail. Missing AI prevents a GPT-strategy entry; the trend strategy can run without AI.

Major-event research is shown separately. Event warnings do not automatically change or block the numerical direction.

This version has **no custom trained prediction model, embedding index, or vector database**. It uses retrieved numerical context and web-assisted event research rather than the earlier embedding-based RAG architecture.

## Requirements

- Python 3.11 or newer
- Internet access to supported data providers
- An OpenAI API key with credits and compatible model access for GPT analysis/event research
- Binance credentials only for optional account checks and execution

Dependencies: NumPy, pandas, certifi, OpenAI SDK, and Pydantic. See `requirements.txt` for version constraints.

## Quick start

Extract the project and open a terminal in the `crypto_signal` folder:

```bash
bash run.sh SEI --strategy gpt --ask-api-key
```

The launcher creates `.numeric_venv`, installs dependencies, and asks for your OpenAI key using hidden input. The key is not saved to a project file. This command performs one analysis scan and places no orders.

If `OPENAI_API_KEY` is already set in your environment, omit `--ask-api-key`.

For the default trend strategy with GPT context:

```bash
bash run.sh SEI --ask-api-key
```

To run trend analysis without GPT or event research:

```bash
bash run.sh SEI --no-ai --no-events
```

## Multiple coins

Edit `coins.json`:

```json
{
  "coins": ["BTC", "ETH", "SEI", "SOL"]
}
```

Run one batch using GPT proposals:

```bash
bash run.sh --coins coins.json --strategy gpt --ask-api-key
```

Repeat continuously:

```bash
bash run.sh --coins coins.json --strategy gpt --watch --ask-api-key
```

The list accepts 1–20 tickers. Coins are processed sequentially. The default 900-second watch interval starts after a batch finishes; it is not synchronized to exact candle-close times. Stop with Ctrl+C.

Supported instruments are active OKX linear USDT perpetuals with sufficient history and compatible contract specifications. Not every cryptocurrency is supported. Unsupported or unavailable core markets return WAIT with an error.

## Data sources

| Data | Source or calculation |
|---|---|
| Historical candles and short-term indicators | OKX hourly and 15-minute closed candles |
| Order book, trades, funding and open interest | OKX; optional Binance second-venue observations |
| Historical comparisons | Numerical similarity across measured returns, volatility and volume |
| Supply snapshot | CoinPaprika |
| TVL and stablecoin-supply proxies | DefiLlama |
| Optional unlocks | Tokenomist |
| Optional large transfers | Whale Alert |
| Optional liquidations | CoinGlass |
| Major-event outlook | OpenAI web search with source/date checks |

GPT receives calculated summaries, 24 recent hourly candles, selected historical comparisons, and current observations—not every historical row. Fetched hourly history is cached locally. Use `--history-days 30` to change the requested window; supported range is 7–365 days.

Missing provider coverage is reported explicitly. TVL is not network revenue, stablecoin supply is not exchange inflow, and large transfers do not establish buying or selling. Historical similarity is descriptive, not a validated probability.

## Major-event research

The event service looks for major reports and events in the next seven days, including interest-rate decisions, inflation, employment, GDP, and significant coin-specific events. It may also report serious incidents from the previous day.

Event dates, source links and possible effects appear separately from the numerical signal. Coverage can be incomplete, and validated links do not prove that AI interpreted an event correctly. Use `--no-events` to disable this research.

## Configuration and API usage

| Environment variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | GPT and web-search API access |
| `OPENAI_MODEL` | Override the default `gpt-5-mini` with a compatible model |
| `TOKENOMIST_API_KEY` | Optional unlock feed |
| `COINGLASS_API_KEY` | Optional liquidation feed |
| `WHALE_ALERT_API_KEY` | Optional transfer feed |

Provider access and coverage depend on the account and endpoint. OpenAI access does not include third-party subscriptions. Do not commit credentials to GitHub.

Default numerical AI limits are 96 attempts per UTC day across coins, with at least five minutes between attempts per symbol. Event research permits 12 attempts per day and uses cached results. These are request limits, not spending limits. Multi-coin scans increase usage.

## Candidate discovery

To discover recent social-post candidates and analyze them without orders:

```bash
bash run.sh --discover-coins --watch --ask-api-key
```

See `DISCOVERY.md` for sources, time-window checks, optional X access and costs. Social mentions only nominate candidates; they do not establish a valid trade. Discovery can return an empty list.

## Paper comparison and optional execution

Compare trend and GPT approaches in paper ledgers:

```bash
bash run.sh --coins coins.json --paper-compare --ask-api-key
```

This does not place exchange orders. See `STRATEGY_AND_EXITS.md` for strategy and exit behavior.

Binance execution is a separate opt-in feature. Read `LIVE_SETUP.md` before configuring credentials or using `--demo-trading` or `--live-trading`. `--check-live` performs read-only account/sizing checks. Analysis-mode settings do not replace the trading JSON configuration.

Execution includes account checks, allocation limits, position tracking and exit management. Exchange order minimums and execution validation can reject a signal. A printed setup is not a fill confirmation. Preserve trading journals across restarts and never run two managers against the same journal.

## Project structure

| File | Responsibility |
|---|---|
| `run.py`, `run.sh` | CLI, scan orchestration and launcher |
| `market_numbers.py`, `market_helpers.py` | Market collection and numerical features |
| `ai_analysis.py` | Structured GPT assessment and event research |
| `auto_context.py`, `binance_depth.py` | External numerical context and Binance observations |
| `signal_logic.py` | Trend-strategy decisions |
| `coin_discovery.py` | Recent social-post candidate discovery |
| `paper_compare.py`, `evaluate.py` | Paper comparison and research evaluation |
| `demo_broker.py`, `trade_manager.py`, `demo_runner.py` | Optional broker execution and position management |
| `live_check.py`, `roi.py` | Account/sizing checks and ROI helpers |
| `coins.json`, `trading.*.json` | Watchlist and trading configuration examples |
| `test_*.py` | Offline software tests |

## Testing

After installing dependencies:

```bash
.numeric_venv/bin/python -m unittest discover -s . -p 'test_*.py' -v
```

See `VALIDATION.md` for the supplied version's recorded checks and limitations. Software tests do not demonstrate profitable trading. Live provider availability, model access, and exchange behavior require separate verification.

## Troubleshooting

- **WAIT:** read the reasons; missing data, a weak setup, or failed validation can prevent a directional signal.
- **GPT unavailable:** check API credits, model access, connectivity, and request limits.
- **Empty discovery:** no qualifying recent posts were found; use `coins.json` for a fixed list.
- **Unsupported contract:** choose an eligible OKX USDT perpetual.
- **SSL errors:** update certifi and check local certificate configuration; do not disable verification.
- **Trading setup skipped:** inspect execution status separately from the analysis signal.

## Publishing

Before committing, exclude credentials, virtual environments, caches, runtime logs and trading journals. Suggested `.gitignore` entries:

```gitignore
__pycache__/
*.py[cod]
.numeric_venv/
.venv/
.numeric_cache/
.env
.env.*
*.jsonl
*.sqlite3*
*.db
*.lock
*.log
.DS_Store
```

Review your actual trading configurations for private account information before publishing. The project is a research and automation prototype; avoid claims of guaranteed returns or proven predictive accuracy.
