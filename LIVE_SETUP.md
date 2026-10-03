# Version 12 update

The current example adds `strategy: trend`, `exit_mode: margin_roi`, +25% take-profit and −10% stop-loss on initial filled margin. GPT is supporting context for trend entries. See STRATEGY_AND_EXITS.md before execution: it explains paper comparison and why net ROI monitoring plus fixed exchange backup prices cannot guarantee exact realised percentages. Existing budget/attempt limits below still apply.

# Live Binance futures setup — version 11

This release contains real-money Binance USDⓈ-M futures execution. It is opt-in. Analysis-only commands and demo mode still work. No authenticated exchange tests or orders were performed during development; offline tests are not proof of profitability or production readiness.

## 1. Understand the 2-USDT example

The live example keeps your requested **3% margin allocation**. With 2 USDT in the futures wallet, this is **0.06 USDT margin per entry**. At 1× leverage it is also approximately 0.06 USDT of position notional before rounding. Exchange minimum quantity/notional can prevent this from being an admissible order. The script reads current symbol filters and prints the reason. It does not increase size or leverage to make a trade happen. Do not assume a 2-USDT deposit will let you test an actual fill.

`trading.live.example.json` starts with SEI and:

| Setting | Value | Meaning |
|---|---:|---|
| mode | live | Must match the live CLI flag |
| margin_per_trade_pct | 3 | Percentage of actual USDT futures wallet balance |
| max_total_margin_usdt | 2 | Admission cap, counting existing/pending account collateral |
| max_wallet_balance_usdt | 2 | Refuse new entries if this wallet contains more than 2 USDT |
| cash_reserve_usdt | 0.25 | Keep this amount unallocated |
| cost_buffer_pct | 5 | Reserve an additional 5% of the proposed margin for the admission check |
| leverage | 1 | No automatic leverage increase |
| max_positions | 1 | One account position/pending entry slot at a time |
| max_entry_attempts | 1 | One durable entry intent in this environment's journal, including rejected/unfilled/uncertain attempts; survives restart |

This is a limited execution test configuration. The single-attempt limit deliberately prevents repeated trading of the same small balance. Raising that limit is a configuration change you must choose yourself; do not delete the journal to reset it. If the balance, minimum order rules, data or signal checks do not allow a trade, the bot will not open one. Changing the code to bypass these checks is not necessary for connecting your API key.

A margin cap is not a guaranteed maximum loss. Fees, funding, slippage, liquidation and exchange/network failures still matter. OpenAI API charges are separate from your Binance balance and from this trading cap.

## 2. Check account eligibility before depositing

In your own Binance account, confirm that **USDⓈ-M Futures and futures API trading are available for your verified country/account**. Availability must not be inferred from being able to browse the public website. If Futures or its API permission is unavailable, stop here; this script cannot enable eligibility and does not offer alternate-region endpoints. Check your account or Binance support before funding it.

If eligible, activate the Futures account **before creating the API key**. This script supports one-way position mode and single-asset margin mode, not Hedge Mode or Portfolio Margin. It refuses incompatible modes. Before an eligible entry it sets the symbol to isolated margin and the configured leverage, then checks that automatic margin addition is off. It does not automatically change global account modes.

The bot uses USDT in the **USDⓈ-M Futures wallet**. A Spot or Funding-wallet balance alone is not available to it. Any transfer to Futures must be made by you in Binance. The script has no deposit, transfer or withdrawal function.

## 3. Create the key

Official instructions: https://www.binance.com/en/support/faq/detail/360002502072

1. Sign in to Binance yourself. Open **Profile → Account → API Management → Create API** (labels may differ by interface).
2. Select **System-generated / HMAC**. This implementation does not accept RSA or Ed25519 private keys.
3. Give it a label such as `crypto-signal-test` and complete the requested 2FA/security checks. Binance's guide lists identity verification, 2FA and an initial Spot-wallet deposit as prerequisites.
4. In the key restrictions, use **Reading** and **Futures** trading permission. Keep withdrawal, transfer, Spot/Margin and unrelated permissions disabled.
5. Restrict the key to the public IPv4 address of the machine/network that will run the bot. Use your current public internet IP, not your Mac's local `192.168...` address. Binance's HMAC security controls require an IP restriction for permissions beyond reading. If your ISP changes the address, update the restriction before running again. Do not disable the default security controls to avoid this requirement.
6. Keep the API key and secret private. The script needs both. Never send them in chat, paste them into source code, put them in the coins JSON or commit them to Git.

If Futures permission cannot be enabled, Binance's guide notes that keys created before activating Futures and Portfolio Margin accounts are possible causes. Regional/account restrictions may also apply. Resolve the cause through your Binance account/support; do not assume adding funds will resolve it.

## 4. Install and run a read-only check

Extract the new ZIP into a new folder. Open Terminal in its `crypto_signal` directory. Python 3.11+ is required. The launcher creates its virtual environment and installs dependencies automatically.

If you have active demo trades from version 10, stop its process and preserve `demo_trades.sqlite3` when upgrading; reconcile it before running multiple copies. Version 11 creates a separate `live_trades.sqlite3` for live execution. Preserve live journals on every subsequent upgrade.

Run:

```bash
bash run.sh --coins trading.live.example.json --check-live --ask-binance-key
```

Two hidden prompts appear:

```text
BINANCE_API_KEY (hidden, not saved):
BINANCE_API_SECRET (hidden, not saved):
```

Paste the corresponding value at each prompt and press Enter. Terminal will not display the characters. That is expected. These values are held in process memory and are not saved. **Do not add the keys to any project file.**

This command only makes GET requests: time, account modes, balances, open orders and market filters/quotes. It places no orders, changes no leverage/account settings, creates no trading journal and does not call GPT. It prints the margin allocation, current minimum notional, estimated minimum margin and any sizing refusal for each coin. `size_eligible` means only that the observed sizing check passed; it does not prove trade permission, a LONG/SHORT signal, liquidity, protection support or future fills. Its estimates can change with prices.

If it shows `3% allocation below current minimum quantity/notional`, no live entry will be attempted under those settings. With a 2-USDT wallet this is a likely result. You can still verify authenticated connectivity without increasing your deposit or changing leverage.

## 5. Run real-money mode

Only when you intend the program to be able to submit real orders, run:

```bash
bash run.sh --coins trading.live.example.json --live-trading --ask-binance-key --ask-api-key
```

It prompts for the real Binance API key/secret and your OpenAI API key. The OpenAI key is separate from Binance and pays for the numerical analysis/event research calls. On startup the console explicitly says `LIVE: REAL MONEY ORDERS ENABLED.` The command continuously monitors; `--watch` is not needed. A fresh final LONG/SHORT is still required—this is not a command to force a test order.

For multiple coins edit only the JSON array, for example:

```json
"coins": ["BTC", "ETH", "SEI"]
```

The included one-attempt limit applies across the whole list, not once per coin. Account margin and position limits also apply across the whole list. A symbol must have supported OKX market data and an exact Binance USDT perpetual contract; unsupported symbols are skipped.

If you already manage secrets using local environment variables, the exact names are `BINANCE_API_KEY`, `BINANCE_API_SECRET`, and `OPENAI_API_KEY`; then omit the corresponding prompt flags. There is no automatic `.env` loading. Demo credentials use different names (`BINANCE_DEMO_API_KEY` and `BINANCE_DEMO_API_SECRET`) and never act as live fallbacks.

## 6. What happens after entry

Before entry, the bot rechecks balance/positions/orders, lot and price filters, a fresh Binance quote, cross-exchange price differences and cost-adjusted reward/risk. It places a bounded IOC limit order and journals its intent before sending. The exchange may partially fill or reject it. A partial fill is protected for the resulting position. An uncertain submission is queried rather than repeated.

A confirmed fill is followed by exchange-side stop-loss and take-profit conditional market exits. Entry and exits are separate requests, so there is an unprotected interval. If the bot cannot confirm the exits or the fill violates its limits, it attempts a reduce-only market close. Network or exchange failures can prevent that close; check any `MANUAL_REVIEW`, `EXIT_PENDING`, unhealthy-account or protection warning yourself. TP/SL does not guarantee fill prices. Never manually trade or modify a bot-owned position while it is running.

After the one permitted entry attempt, the program continues monitoring instead of submitting more entries. When a position closes, it cancels its leftover exit orders. With a larger explicitly configured attempt limit, it requires cleanup, cooldown and a later signal candle before reentry. When funds/limits are exhausted it pauses new scans, not position monitoring.

**Ctrl+C stops monitoring; it does not close positions.** Verified exchange-side exit orders remain. Check Binance yourself when stopping, especially if protection was incomplete. Restart with the same keys/config/journal to resume. Keep the computer awake while monitoring is needed. An already running analysis thread may finish before Python exits; it does not independently submit orders.

The live journal is bound to the environment and a hash of the API key. This prevents accidentally applying another key/account to old position records. Rotating a key requires deliberate journal migration after reconciling positions; deleting the journal can discard pending-order ownership and is not a recovery procedure. A process lock protects one journal, not separate copies on different computers: run only one instance for this account.

## 7. Interpreting the output

- `WAIT`: numerical analysis did not pass the signal checks, or evidence/API output is unavailable. Read the reported reasons.
- `SIZE SKIP`: the configured budget cannot meet sizing rules. No GPT analysis is requested for that attempt.
- `Wallet exceeds configured ceiling`: funds are above the example's 2-USDT ceiling. New entries are blocked; monitoring continues.
- `Entry-attempt limit reached`: the journal already contains the configured number of entry intents, even if an order was unfilled or uncertain.
- `Position already open` / `Order already pending`: duplicate entry is blocked.
- `Account reconciliation unavailable`: new entries are blocked until the exchange state can be read reliably.

No tests in this project establish useful predictive accuracy. Real trading and protective execution remain unverified against an authenticated Binance account. Review `VALIDATION.md` for the exact test scope.

Official endpoint references: https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info and https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade
