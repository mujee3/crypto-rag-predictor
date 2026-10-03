# Version 12: trend entries, comparison and +25% / −10% margin exits

The new entry strategy uses closed candles: hourly close above 24-hour rolling VWAP plus a green 15-minute candle above EMA9 produces a LONG candidate. The inverse produces a SHORT candidate. All those calculations use confirmed OKX candles and their actual timestamps. An unaligned setup means WAIT. Existing candle validation rejects gaps, malformed values and stale history.

GPT still interprets the broader numerical evidence. Its agreement is not required for the trend strategy. Low recent volume, high funding and partially visible depth are context warnings; partial depth is accepted only if the observed liquidity already meets the configured minimum. Fresh quotes, valid candles, sufficient observed depth, acceptable spread, costs, margin and duplicate-order checks still matter. A current Binance quote is checked again before execution.

## Start with the comparison

```bash
bash run.sh --coins coins.json --paper-compare --ask-api-key
```

This is continuous **paper trading**. It uses no Binance trading credentials, makes no orders and does not change an existing account position. It evaluates the old GPT strategy and the new trend strategy on each shared snapshot. Each strategy has its own paper positions; an open paper position cannot be overwritten by another signal. A fresh candle and a five-minute cooldown are required after closure. The old strategy retains its existing historical-price-level entry eligibility check; both paper ledgers use the same ROI exits to compare the resulting entry policies.

Paper stakes are fixed at 10 virtual USDT initial margin per position. This is an equal-stake comparison, not simulation of your wallet or margin caps. The code polls public prices while the scan runs in a worker thread. It estimates fee/slippage costs and funding using public settled funding rates multiplied by the observed notional at detection. That funding amount is an approximation, not a historical settlement-price calculation. It does not claim actual fills, latency-free exits or complete intrapoll coverage. A public-data failure pauses marking and is printed as BLOCKED; it does not manufacture a closing price. Observation gaps are retained in each trade record.

The files beside your output log are:

- `paper_comparison.sqlite3`: durable positions and decision/rejection counts; preserve on restart.
- `paper_comparison_summary.json`: open/closed counts, estimated P&L, wins and rejection reasons, refreshed approximately once a minute and on clean shutdown.
- `signals.jsonl` (or your `--output` path): both strategy decisions and their reasons for every completed scan.

Do not infer quality from a higher number of signals or a handful of wins. Compare closed outcomes after costs, open losses, observation gaps and a meaningful range of market conditions. This release has not established profitability or predictive accuracy.

## The confirmed exit policy

The new example JSON files contain these fields within `trading`:

```json
"strategy": "trend",
"exit_mode": "margin_roi",
"take_profit_pct": 25,
"stop_loss_pct": 10,
"exit_slippage_bps": 3
```

These percentages refer to **initial margin of the actual filled position**, not coin price changes, wallet profit or partial profit-taking. Initial margin is fixed as `filled quantity × actual average entry price ÷ configured leverage`. Partial fills therefore get their own actual margin basis. The entire remaining position is closed on the exit decision.

For a position using 10 USDT initial margin, the targets are +2.50 USDT and −1 USDT. The margin denominator does not change when the balance, market price or configuration later changes. New settings apply to new entries; old journaled positions keep their existing exit policy. Do not delete a journal to force a policy change.

At 1×, a +25% margin target needs approximately a +25% favourable price movement before costs. At 5× it needs roughly 5%; at 10× roughly 2.5%. The loss movement is approximately 10% divided by leverage. Fees/funding shift the exact price. These are arithmetic examples, not recommendations to increase leverage. Such targets may take much longer than an hour to reach; there is no one-hour forced exit. The old GPT analysis horizon remains one hour.

The monitor calculates:

`estimated net P&L = directional price P&L − recorded entry commissions + signed recorded funding − estimated closing fee/slippage`

`estimated net ROI = estimated net P&L ÷ fixed initial margin × 100`

For live/demo execution, entry commissions come from the bot's own filled order, with filled quantity checked for completeness. Funding comes from that symbol's signed account funding history since entry intent, deduplicated and paginated. Funding is refreshed approximately every minute; account posting delays can still matter. The closing fee uses the taker rate observed before entry and the configurable slippage allowance. The exit monitor uses the available bid for a LONG and ask for a SHORT. Actual closing fees and fills cannot be known in advance.

If estimated net ROI reaches +25% or −10%, it attempts a reduce-only market close of the whole current position. The journal prevents blindly resubmitting uncertain orders. It also places exchange-side close-position TP/SL backup price orders, computed once from the actual fill, entry-time fee estimates and zero future funding. **Those fixed backup prices are not dynamically repriced for later funding, and can trigger before or after the net-ROI threshold.** They remain available if the program disconnects. They are approximate protection, not a guarantee that the realised result will be exactly +25% or −10%. Price gaps, slippage, exchange failure, delayed accounting and emergency protection exits can produce different results.

Non-USDT entry commissions or incomplete fee/funding data are not silently treated as zero. The program reports `roi_problem`, retains the backup exits and blocks new entries until accounting becomes available. The supported funding lookback is limited to 80 days. Existing close/protection uncertainty still needs account inspection. Do not manually alter a bot-owned position; a quantity mismatch requires manual review.

## Reading decisions

- `READY / LONG` or `READY / SHORT`: selected entry strategy and its market checks passed; execution still needs its separate account checks.
- `WAIT`: no directional setup.
- `BLOCKED`: required data or, for the GPT strategy, its API assessment is unavailable.
- `SKIPPED`: spread/liquidity/exit suitability or a subsequent account/order-size check prevents entry.

The legacy `signal` field stays LONG/SHORT/WAIT for compatibility. Read `decision_status` to distinguish a genuine WAIT from a block; execution SKIPPED can accompany a directional candidate. `comparison` retains each strategy's separate status and reason. Numerical GPT daily-limit failures appear as BLOCKED for that strategy and do not veto a valid trend decision. The existing 96/day numerical and 12/day event request caps are unchanged; the comparison is incomplete when the old strategy has no API assessment.

## Commands

One trend scan with GPT commentary and event warnings:

```bash
bash run.sh SEI --strategy trend --ask-api-key
```

Trend-only numerical analysis without OpenAI calls:

```bash
bash run.sh SEI --strategy trend --no-ai --no-events
```

The old selection is available with `--strategy gpt`. For analysis/paper mode, exit assumptions can be changed with `--take-profit-pct 25 --stop-loss-pct 10 --leverage 1`. Default analysis selection is now trend. Telegram add-ons that call `scan` without a strategy still default to the legacy GPT selection; this release does not silently change the separate Telegram add-on.

The execution commands remain:

```bash
bash run.sh --coins trading.example.json --demo-trading --ask-binance-demo-key --ask-api-key
bash run.sh --coins trading.live.example.json --live-trading --ask-binance-key --ask-api-key
```

For execution, the JSON determines strategy, exits and leverage, overriding the analysis-only CLI settings. **Live means real orders.** Run the paper comparison first as agreed; these commands are provided for explicit execution after review. No orders were placed during development.

The live example still has the prior 2-USDT margin cap/wallet ceiling, 3% allocation, 1× leverage and one entry attempt across restarts. It can remain below exchange minimum size. This update does not raise your financial allocation or reset entry counts. The same margin checks, process locks, existing-position checks and journal ownership rules apply. Preserve your live/demo journals on upgrade. Ctrl+C stops monitoring and does not flatten exchange positions.
