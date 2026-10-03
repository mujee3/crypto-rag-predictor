# Version 13 social candidate discovery

89 offline tests pass. New tests cover individual-post URL restrictions, publication age and timezone checks, X ID timestamps, explicit ticker extraction, Binance/OKX contract intersection, deduplication, expired/future posts, fresh-cycle scheduling without stale fallback, daily caps, secret-safe failures, retrieved-source enforcement, structured X API results, post expiry after analysis, repeated batch refresh, and continued live/demo and paper monitoring during discovery. All 75 prior tests still pass. CLI help, Python compilation and launcher syntax checked.

No authenticated OpenAI/X discovery requests or real/demo orders were made. Search coverage, AI excerpt fidelity, website publication metadata and account API entitlements remain unverified live. Tests establish local software behavior, not signal quality or profitability. Discovery is opt-in; existing account limits and ROI exits remain unchanged.

# Version 12 strategy and ROI update

75 offline tests passed, including 11 new tests covering both ROI directions and leverage arithmetic, the 10-USDT +2.50/−1 example, invalid accounting, closed-candle trend selection without GPT, context warnings without veto, WAIT/BLOCKED/SKIPPED statuses, actual-fill margin and whole-position exit, missing-fee protection retention, complete USDT fee matching, paper duplicate/candle protection and funding deduplication. Prior 64 regression tests remain passing. No authenticated exchange/OpenAI calls or real/paper market run were made for validation. No accuracy or profitability claim is established. Paper funding is estimated using observed notional at detection; live funding uses reported account history, while closing costs remain estimates. Fixed native backup exit prices do not dynamically follow funding.

# Version 11 explicit live mode

64 offline tests passed. Thirteen new tests check separate live/demo hosts and credential names; no demo-to-live credential fallback; read-only mutation rejection; signed host/request construction and secret-redacted errors; 2-USDT/3% minimum-size refusal; explicit required live limits; CLI mode conflicts; read-only CLI with no GPT or order mutations; demo-config/live-command mismatch; journal environment/key binding; wallet-ceiling blocks with continued monitoring; a persistent one-entry limit with protective orders retained; and refusal of automatic margin top-up. Existing 51 tests still pass.

The live example uses a 2-USDT wallet ceiling and margin admission cap, 3% allocation, 0.25-USDT reserve, 1× leverage, one account position and one journal entry attempt. This does not guarantee a 2-USDT maximum loss, profitability or a fill. Current exchange minimums can make every entry ineligible. The read-only diagnostic reports the observed minimum and estimated minimum margin without raising allocation or leverage.

No real keys were used, authenticated live/demo requests made, trades executed or funds transferred during this update. All broker tests use local fixtures or mocked HTTP transport. Live API support, account permissions, jurisdictional eligibility, exchange execution and exit behaviour still require user-side verification. No changes were made to signal thresholds to manufacture LONG/SHORT results. OpenAI charges are separate from the futures wallet.

Version 10's demo-only restrictions below describe the preceding release; version 11 adds a distinct explicit live mode. Local command-line help, launcher shell syntax, Python compilation and ZIP integrity were also checked.

# Version 10 demo margin manager

51 offline tests pass. New deterministic tests cover the 50-USDT budget, pending reservations, minimum-notional refusals, long/short sizing, stale signals/quotes, existing positions/orders, duplicate prevention, uncertain entry acknowledgements, partial fills, failed or mismatched protection, reduce-only closure, post-close cleanup/cooldown, reused candles, externally changed positions, restart reconciliation, journal process locking, production-mode rejection, close-position API parameters, and scanning paused when margin is exhausted. Prior 34 numerical/GPT/multi-coin tests remain passing.

No authenticated Binance demo requests, real-money orders or authenticated GPT calls were made during this update. Exchange compatibility, fills and account configuration still require user-side demo verification. Tests validate local control behavior, not investment performance. Execution is hardcoded to the demo host; no production execution setting is provided.

# Version 9.2 multi-coin update

34 offline tests passed. Four new tests cover both JSON formats, normalization/deduplication, malformed/conflicting configuration, per-coin failure isolation and separate logs, and repeating complete batches in watch mode. Authenticated live multi-coin GPT calls remain untested here.

# Version 9.1 diagnostic update

30 offline tests passed. Additional checks verify valid per-factor reference suggestions and specific safe error output for an invalid reference. No live key was available; the user’s original generic error cannot be attributed to one exact cause retroactively.

# Version 9 validation

29 offline tests passed on 2026-10-02. Checks cover:

- BTC/SEI complete market collection with deterministic exchange/provider fixtures, no trained-model load or outputs, and no BTC self-correlation.
- Historical return calculations, partial history, stale history rejection and cache use.
- Past-only historical comparisons, actual subsequent return computation and 24-hour separation of selected examples.
- Numerical-only external provider requests, missing paid feeds, symbol normalization and invalid symbol rejection.
- Structured GPT factor coverage, evidence paths, conditional BTC exclusion, missing key, stale core, request cooldown and secret-safe errors.
- Both LONG and SHORT risk plans, stale quotes, shallow books and inadequate cost-adjusted reward/risk.
- Event date/time and retrieved-URL filtering, actual-search requirement and event caching.
- Real OpenAI SDK numerical structured parsing and web-search request serialization through a local mocked HTTP transport.
- Existing Binance book/trade normalization and confirmation tests and verified TLS setup tests.

Launcher shell syntax and command-line help were checked. No training dependencies or model artifacts are included.

Live BTC smoke run attempted actual OKX data and returned WAIT with a clear provider error: OKX returned non-JSON data in this environment. No alternative domain/region was used. Core live numerical success could not be verified here.

No OpenAI API key was configured in this environment. Therefore real authenticated GPT calls, web-search plus structured output compatibility for the user's account/model, response quality, and live event accuracy remain unverified. Mock HTTP tests check local request/response handling, not service availability.

These tests establish software behavior only. There is no demonstrated predictive accuracy, profitability or validated advantage of the new GPT policy. Endpoint evaluation is not a fill-aware execution backtest.
