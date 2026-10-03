# Recent social signals as a changing candidate list — version 13

Discovery chooses **which coins to inspect**, not which trades to execute. Each cycle searches again, checks publication age, extracts explicit uppercase cashtags such as `$BTC` or pairs such as `BTCUSDT`, and keeps only matching active Binance and OKX linear USDT perpetuals. It does not infer ambiguous full coin names or map leveraged/1000-prefixed symbols to other coins.

The normal numerical pipeline then decides LONG / SHORT / WAIT. A post saying LONG can result in our system choosing SHORT or WAIT. Post text, claimed returns, direction and target prices never enter the numerical GPT prompt or order sizing. Search snippets are AI-extracted and can be misread; this is not verification of the author's skill or endorsement of the signal.

## Run without orders

Open Terminal in the extracted `crypto_signal` folder:

```bash
bash run.sh --discover-coins --watch --ask-api-key
```

Default: search public Binance Square and X, posts at most **2 hours old**, at most **10 unique coins**, newest first. After all candidates have been handled, wait **900 seconds** before the next fresh search. Omit `--watch` for one cycle. Use `--discovery-hours 4`, `--discovery-limit 20`, or `--interval 600` to adjust. Hours must be 1..24, coins 1..20, interval at least 300 seconds.

The same still-recent post may nominate a coin in multiple cycles. Position, cooldown and candle checks still apply. Stale posts are rechecked before analysis and before execution eligibility. A failed or empty search yields no candidates; the old list is never substituted. `discovery.jsonl` records search outcomes, post URLs, times and rejection counts alongside `signals.jsonl`. Each coin report includes its discovery source.

## Existing automatic-position mode

Use your **existing trading JSON and journal**, adding the discovery flag:

```bash
bash run.sh --coins trading.json --discover-coins --live-trading --ask-binance-key --ask-api-key
```

This enables real-money execution, exactly as `--live-trading` did previously. In discovery mode any `coins` array is ignored; the `trading` settings still control allocation, leverage, total margin, max positions, attempt limit and exits. Keep your actual existing filename in place of `trading.json`.

New examples without any coin array are included:

```bash
bash run.sh --coins trading.discovery.demo.json --discover-coins --demo-trading --ask-binance-demo-key --ask-api-key
bash run.sh --coins trading.discovery.live.json --discover-coins --live-trading --ask-binance-key --ask-api-key
```

The live example preserves the prior 3% allocation, 2-USDT total margin/wallet ceiling, 1x leverage and one entry attempt. Minimum order sizes may prevent entries. This release does not increase those settings. The whole-position +25% profit / -10% loss targets remain percentages of initial margin, with the existing cost and fixed backup-order limitations described in STRATEGY_AND_EXITS.md.

Open positions remain monitored even when absent from the next discovered list. Discovery and analysis run in a worker while the main thread monitors positions. Existing/pending positions are skipped before analysis. When account limits prevent new entries, new discovery is paused and monitoring continues. Keep the same journal across restarts; never run two bots for the same account.

`--check-live` remains a static-list read-only sizing check and cannot combine with `--discover-coins`.

## Source options and coverage

**`--discovery-source web` (default):** OpenAI web search looks across public Binance Square and X. Requires an OpenAI API key even with `--no-ai --no-events`; those flags disable the other GPT tasks only. Only actual search-sourced individual post URLs are accepted. X publication time is decoded from the post ID; Square requires readable explicit publication metadata. Search index delays, inaccessible pages, missing metadata or limited tool calls can yield few or zero coins. This does not scrape private/logged-in feeds or guarantee all recent signals. No alternate access route is used if blocked.

**`--discovery-source x`:** Uses X's official `/2/tweets/search/recent` endpoint with `tweet.fields=created_at`. Set `X_BEARER_TOKEN` locally from your X developer account, with access to recent search. This source searches X only, returns at most 100 recent matching posts per cycle, excludes retweets, and does not paginate. Account access and provider charges may apply. Never paste keys into chat or put them in the JSON. With `--no-ai --no-events` this route does not require OpenAI.

Example after configuring `X_BEARER_TOKEN` locally:

```bash
bash run.sh --discover-coins --discovery-source x --watch --no-ai --no-events
```

API reference: https://docs.x.com/x-api/posts/recent-search
Snowflake timestamp reference: https://github.com/twitter-archive/snowflake/blob/snowflake-2010/src/main/scala/com/twitter/service/snowflake/IdWorker.scala

## Costs and limits

Discovery defaults to **24 attempts per UTC day**, shared across symbols and source types in the same cache, with at least 300 seconds between attempts. Failed attempts count. At 15-minute intervals the cap may be reached in roughly six hours; subsequent cycles report `daily_limit` until the UTC day resets. To permit all-day discovery use `--discovery-daily-cap 96` explicitly. Each web attempt can invoke up to four web-search tool calls plus model tokens; this is an attempt limit, not a dollar spending cap. OpenAI analysis and event-search daily caps remain separate (96 and 12). X requests can have separate provider costs/limits.

Do not delete the cache to bypass limits. No search method establishes predictive accuracy. Offline validation covers local behavior; authenticated web/X discovery and exchange fills have not been verified for your accounts.

## Forward paper comparison

```bash
bash run.sh --discover-coins --paper-compare --ask-api-key
```

Both strategies receive the same discovered candidates. Existing paper positions keep being marked after a candidate leaves the list. Paper mode places no exchange orders. Keep GPT enabled to compare both strategies meaningfully.
