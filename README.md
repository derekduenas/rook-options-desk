# Options Desk

A local, read-only-market-data journal for checking option quotes and replaying the expiration payoff of standard U.S. equity call debit spreads. Python 3.11 or newer is sufficient.

## Open the research terminal

Open `data/terminal.html` in the browser for the current offline snapshot. Refresh the file from the journal after adding data or reviews.

```sh
python3 dashboard.py --export data/terminal.html
python3 dashboard.py  # optional live local view at http://127.0.0.1:8765
python3 optionsdesk.py review data/plans/aapl-intelligence.json
```

The terminal shows payoff, decision history, cited reviews, and data provenance. The initial analyst reviews were authored by Codex and imported into the local journal. Each review records its evidence cutoff and its actual journal receipt time. A review can include two probability distributions keyed by the original scenario names under `forecasts.baseline` and `forecasts.enhanced`. Both are evaluated against the same frozen net payoff. Stale quotes or late receipt leave the comparison reference-only. A modeled comparison is hypothetical, and the two initial reviews have no scored forecasts. See the sample review files in `data/plans/` for the input format.

## Look up the recorded data

```sh
python3 optionsdesk.py status
python3 optionsdesk.py inspect EXE270115C00090000
python3 optionsdesk.py inspect AR270115P00033000 --as-of 2026-11-26T18:25:00-08:00
```

`inspect` returns the latest eligible observation for each source and feed. A source observation is eligible only after both its market timestamp and its receipt timestamp. Dated open interest uses the newest eligible session, with later revisions applied within that session. Robinhood's displayed open interest is kept as an undated quote field. Alpaca indicative quotes are reference-only, even when their timestamps are recent.

## Capture and replay

Run this from the project directory. `--db FILE` before the subcommand selects another database.

```sh
python3 optionsdesk.py record-mcp data/captures/20261127T022457Z-alpaca-contracts.json
python3 optionsdesk.py evaluate data/plans/aapl-20261126.json
python3 optionsdesk.py replay 84036535387ecf1601fcf8b52f8613a56bd40bec658f044d2f1c68051c6096cd
python3 -m unittest -v
```

The connector capture is on demand. Use the Robinhood MCP to resolve contracts and retrieve option and equity quotes. Use Alpaca's option snapshots and contract metadata for a cross-check and dated open interest. Save `structuredContent` in a JSON envelope with `provider`, `received_at`, and `payload`. Use receipt time after the response arrives. For `robinhood_options` and `alpaca_snapshots`, also include the corresponding `instruments` array from the contract lookup. The four captured envelopes in `data/captures/` show each shape. Use `record-mcp -` to import an envelope from standard input. `record` also accepts a normalized batch with `source`, `feed`, `received_at`, and `observations`.

The SQLite database and capture files are under the ignored `data/` directory. Identical imports are idempotent. A decision freezes its selected observations and input assumptions, and replay checks the decision hash before recomputing its results. New decisions and catalyst studies use v2 pricing and eligibility rules. Archived v1 records replay under their original rules.

## Decision inputs and limits

- A plan specifies its as-of time, provider and feed, expiry, strikes, quote-age and leg-skew thresholds, thesis, invalidation, optional fees, and terminal stock-price scenarios.
- For each scenario, the output reports payout and P&L per spread. The synthetic debit crosses the individual displayed quotes. Displayed size is an upper bound inferred from those legs. Actual multi-leg fills depend on the complex book and its net price.
- Supply a probability for each scenario to calculate expected P&L and the maximum debit consistent with the required expected profit. Probability inputs are explicit assumptions. The sample plans omit them because these captures supply no validated forecast.
- Optional `portfolio` contains `source`, `as_of`, `horizon`, and `pnl_dollars` keyed by scenario name. It must use the same decision time and expiration horizon. The journal adds the candidate's P&L to those supplied results. Combined risk is unknown when that input is absent. The expiration model assumes standard 100-share deliverables and both spread legs intact. It does not model interim mark-to-market, early assignment, taxes, or commission schedules. The CLI accepts an explicit fee assumption.

The initial capture contains 101 observations across Robinhood and Alpaca. The AAPL and EXE examples were captured Thursday evening using Wednesday's option quotes, so their replay status is `reference_only`. This tool runs locally and imports read-only market data. It has no broker submission, background collection, or historical tick replay.

## Publish the private terminal

The separate Site checkout is `../optionsdesk-site`. See the [Site architecture](SITE_ARCHITECTURE.md) for the publication boundary. Build a release with `python3 publish_snapshot.py --workspace --out ../optionsdesk-site/dist --decision ID`, repeating `--decision` for each selected record. The exporter reads one journal transaction and uses a field allowlist for the selected decisions and market-only terminal snapshot. The hosted view has searchable market discovery, historical close charts, captured contract summaries, and an expiration payoff builder. Paper plans stay in the browser. Hosted market data updates on redeployment. The local loopback terminal retains the scanner, research engines, and journal.

## Catalyst studies

```sh
python3 optionsdesk.py study-catalyst data/plans/mu-earnings-catalyst.json
python3 optionsdesk.py replay-catalyst 8ef2d5946e5d8f83bf2c1c40c07aa3ccb3a9f5f60d41b63c5d7042d84126341f
python3 dashboard.py --export data/terminal.html
```

The Catalyst studies view compares a single long call or put with a same-expiry debit spread. Each plan freezes a named universe, cited event and availability time, strike pair, assumptions, and expiration scenarios. Ranking uses return on maximum loss at the named favorable scenario under the slippage stress. Both structures use the same journal cutoff and provider feed. Historical, stale, or late-recorded inputs remain reference-only.

Optional account stress reports fractional-exposure loss streaks, repeated simultaneous losses, and how many whole contracts fit a declared illustrative budget at stressed cost. A fully specified probability distribution and named forecast basis enable expected P&L and 2,000 seeded hypothetical equity paths. The paths assume independent batches and a shared outcome for simultaneous positions within each batch. With illustrative equity supplied, v2 paths size in whole contracts and stay in cash when the budget cannot fund a contract. The seeded MU case supplies no probabilities. The sample compares held-to-expiration payoffs under intact legs and configured entry costs. It has no historical fill or exit data and has no observed performance. Catalyst studies stay in the local journal and offline terminal.

## Earnings Lab

The local Earnings Lab shows the fixed universe, registered events, and separate pre- and post-release decisions. The stock, cash, outright option and same-expiry debit spread use the same illustrative dollar budget. Pricing reserves entry and exit fees, respects displayed option size, and uses only observations received by the fixed cutoff. The stock benchmark uses the last sale. Option replay uses displayed leg prices and leaves P&L empty when the modeled exit would require more size than the quote displays.

```sh
python3 optionsdesk.py earnings-register data/plans/earnings-pilot.json
python3 optionsdesk.py earnings-status
python3 dashboard.py --export data/terminal.html
```

The first registered event is Micron's December 17 earnings call. In Eastern time, the pre cutoff is December 17 at 3:55 p.m., the post cutoff is December 18 at 9:45 a.m., and the shared exit cutoff is December 18 at 3:15 p.m. The call is scheduled for 4:30 p.m. Eastern. The post phase uses the timestamp of the actual published results and the time the desk first saw them. [Micron's earnings announcement](https://investors.micron.com/news/press-release/2026/Micron-Technology-to-Report-Fiscal-First-Quarter-Results-on-December-17-2026/default.aspx)

- For another confirmed event in the universe, run `earnings-schedule FILE` with `experiment_id` and the same event fields as the pilot. Register it before its pre cutoff. The database keeps the original terms when an event is retried.
- Capture the two options and the underlying with `record-mcp` just before each cutoff. The configured pilot uses Robinhood and allows quotes up to 45 seconds old with option legs up to 10 seconds apart. Record the decision with `earnings-entry FILE` as soon as the cutoff passes. The input fields are `experiment_id`, `event_id`, `phase` (`pre` or `post`), `action` (`paper` or `pass`) and `reason`. For the post phase, also include `release` with the actual results `source`, `published_at` and `first_seen_at`. Check the publisher and timestamps against the registered issuer. The journal checks timing and preserves the provider evidence alongside the decision. The pilot's prospective registration window ends 30 seconds after each entry cutoff or at the next phase boundary, whichever comes first.
- Capture again just before the fixed exit. Run `earnings-resolve ENTRY_ID` for each phase after that cutoff. Replay crosses displayed option bid and ask prices and records incomplete candidates, including missing data and positions the budget could not fund. Each phase keeps its original record, including a pass or a missed cutoff.
- `earnings-paper FILE` records an observed paper round trip or unfilled attempt for a prospective paper candidate. Include `entry_id`, `structure`, `status` (`filled`, `partial` or `unfilled`), `source`, `reference` and `attempted_at`. For a round trip include `quantity`, `entry_at`, `exit_at`, `entry_net_price`, `exit_net_price` and total `fees_dollars`. Net prices are per share or per option point. Capture and recording are on demand. The Lab does not submit orders, and its records stay local.

The post-release phase requires market observations at or after the confirmed publication time. Late confirmation can reveal that a pre-release decision or paper fill followed an early release. The scorecard applies that correction from the time the desk received it. A cash-only replay appears separately from a completed replay with market outcomes.

Run `python3 -m unittest -v test_workflow` to rehearse registration, provider import, both cutoffs, replay, paper scoring, forecast eligibility, and dashboard export in a temporary SQLite journal. Its market and paper data are synthetic.

The MU affordability control fixes the December 18 $720 call and $720/$760 vertical under the original $2,500 budget. At the November 25 reference quotes, one lot reserved $1,912 and $714 respectively, including round-trip fee assumptions. The two MU plans share one issuer event. Compare them separately when counting evidence. Salesforce is the first event in the separately registered December cohort. Its $200 call and $200/$210 spread reserved $1,072 and $469 per lot at the reference quotes. [Salesforce earnings announcement](https://investor.salesforce.com/news/news-details/2026/Salesforce-Announces-Date-of-Third-Quarter-Fiscal-2027-Earnings-Release-and-Webcast/)

## Capture and measurement

The local terminal has three research engines that share the journal, limit-price evaluator, and portfolio stress tools:

- **Directional** tests relative strength across ordinary trading sessions and audits options flow against actual stock and option observations.
- **Volatility** compares realized-variance forecasts and uses the volatility surface, higher-order Greeks, and joint repricing tools.
- **Catalyst** researches dated events. Earnings Lab is one workflow inside Catalyst. Set `catalyst.event_type` to `earnings`, `macro`, `corporate`, `regulatory`, or `other` when freezing a new study. Legacy studies stay replayable.

Use `python3 desk_run.py --engine directional` to run one engine and print its result. The other values are `volatility` and `catalyst`. A single-engine run leaves the combined terminal report in place.

**Market intake** gives all three engines a common regime, news, and option-liquidity snapshot. Put the collected input in `data/market-scan-input.json` and rerun `python3 desk_run.py`. Regime uses SPY's last completed session versus its 20- and 50-session averages, realized-volatility rank, and a five-ETF breadth basket. A regime stays unclassified when regular-session breadth is incomplete. The news view deduplicates titles, preserves publication and receipt time, and distinguishes primary releases from lead headlines. The liquidity screen checks the age and sides of the book, spread relative to mid, displayed size, and the supplied broker cutoff. The terminal timestamps the snapshot. Each run reads the supplied snapshot and sets up no background polling.

### Regular-session opportunity scanner

`python3 optionsdesk.py opportunity-scan` evaluates the scanner configuration at the current clock. Add `--watch --every 5` for a continuous JSON stream or use `--as-of TIMESTAMP` for a historical replay. The local dashboard evaluates the same scanner when the **Opportunity scanner** page is open. The standalone HTML export shows an as-of snapshot.

The scanner reads `opportunity-scan.json` beside the journal. It takes explicit `market_sessions` with `open_at`, `close_at`, and `previous_session`, and exact `contracts` with `symbol`, `expiry`, `strike`, `right`, `known_at`, and `last_entry_at`. Configure `option_feed` and `stock_feed` as provider/feed pairs, the quote age and skew limits, two-sided liquidity limits, and the one-lot budget and fee assumptions. The local file keeps a narrower set of verified contracts for quote screening, with a $250 illustrative one-lot budget and a regular-session cutoff at or before the supplied contract deadline. Refresh the contract universe and calendar before those bounds expire. Supply new quotes by journal capture or connector import.

This baseline screens same-session stock direction against a verified previous close, option spread and depth, timing, and approximate one-lot affordability. It ranks surviving liquidity-screen leads by spread percentage, then by movement magnitude. Use the shared expression review, candidate sizing, and portfolio stress tools to assess a proposed trade. The scanner reads recorded quotes and leaves order authority outside the workflow.

The upstream Robinhood discovery board reads `data/robinhood-market-discovery.json`. It takes a `previews` list with each market-wide Robinhood scanner preview's `received_at` and `result`, plus an optional `unpartitioned_preview` with `matches` and `received_at` for the same filter scope. Use stock and ETF filters and split options volume into non-overlapping bands when the result cap is reached. Rook reports provider counts, rows received, distinct usable symbols, and any discrepancy. The board ranks underlyings by options activity and keeps a separate largest-moves list in the JSON output. Preview prices and volume are discovery evidence. Contract screening requires a separate exact-contract lookup and fresh journal quotes.

Run `python3 desk_run.py` to check the local journal, replay all frozen spread decisions, recompute the saved historical benchmarks, and run both pricing rehearsals. The command writes a checksummed `data/research-run.json` beside the journal. The local terminal displays it under **Risk and validation**. Export the updated standalone terminal with `python3 dashboard.py --export data/terminal.html`. The run reads the journal without recording market captures, forecasts, or orders.

```sh
python3 optionsdesk.py capture-status
python3 optionsdesk.py capture-status --symbol CRM
python3 optionsdesk.py scorecard
python3 optionsdesk.py reconcile-due
python3 optionsdesk.py affordability-preflight proposed-case.json
python3 optionsdesk.py archive-fetch historical-case.json > historical-market-records.json
python3 optionsdesk.py archive-replay historical-market-records.json
python3 optionsdesk.py capture-run --watch
```

The local Research Monitor shows cutoff readiness and cohort coverage. `reconcile-due` freezes any recorded entry whose exit has passed and whose replay remains pending. The scorecard reports matched replay comparisons separately from paper fill rates and measured slippage. Missing captures and unmatched comparisons remain visible. Confidence intervals use distinct New York event days and require at least 20 days. The paper statistics use eligible recorded attempts and preserve distinct denominators for attempt coverage, fill rate, and matched slippage.

`capture-status` reports each planned capture window, the registered provider and feed, exact option symbols, and each leg's quote status, market time, and receipt time. A prospective cutoff stays scheduled even when an earlier connectivity check has fresh quotes at the time of that check. After a cutoff, status is computed from observations available by the frozen cutoff.

The paper scorecard measures entry markouts at 1, 5, 10, and 60 seconds and five minutes for recorded option fills that are still open at the horizon. Midpoint-to-midpoint movement measures what happened immediately after the fill. Fill-to-midpoint movement includes the entry price relative to that subsequent mark. Quotes must be available by the applicable cutoff, at most one second old, and newly observed after the fill for each follow-up. A missing capture produces a coverage gap. Spread markouts use the difference between synchronized leg midpoints and are indicative.

Each horizon reports `incomplete_reasons` for eligible fills without both measurements. A `followup_` reason takes precedence when the follow-up quote is unusable. An `entry_` reason can coexist with a measured fill-to-midpoint result.

`affordability-preflight` takes the proposed `protocol` and `event` and reports the fee-reserved one-lot cost and headroom for both option structures. It labels stale inputs as such, even when they can support a planning preview. Run it before registering new pricing terms. Official experiment sizing still uses cutoff-eligible quotes.

The timed worker supports protocols registered with Alpaca `opra` options and `sip` or `iex` equities. Give the worker authorized `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` environment variables. It uses read-only requests and records both successful captures and per-feed failures shortly before each registered cutoff. It defaults to Alpaca's paper host for contract metadata. Set `OPTIONSDESK_ALPACA_CONTRACT_MODE=live` only for a credential pair provisioned on Alpaca's live host. The worker still uses only read requests. Robinhood protocols use the existing connector and `record-mcp` at the cutoff. Alpaca credentials do not change the feed frozen into a Robinhood protocol. [Alpaca latest option quotes](https://docs.alpaca.markets/us/reference/optionlatestquotes) · [Alpaca contract metadata](https://docs.alpaca.markets/us/reference/get-option-contract-symbol_or_id)

The archive commands run separately from the prospective journal. `archive-fetch` takes `protocol` and `event` in the Earnings Lab formats and uses an entitled `MASSIVE_API_KEY` for timestamped historical records at the three cutoffs. `archive-replay` also accepts a normalized file with `archive_source`, `archive_reference`, and `observations` containing the standard option quote and underlying trade fields. This path uses historical market timestamps, with stock last sales as the benchmark. The historical scores describe quote replay. Prospective receipt times and paper fills require their own evidence. [Massive historical option quotes](https://massive.com/docs/rest/options/trades-quotes/quotes)

Include an optional `strike_selection` object with `as_of`, `strike_increment`, and `spread_width` to verify the selected call and wing. The cutoff must be at or before the pre-release entry. Replay checks the selected strikes against the latest fresh stock trade at the selection cutoff and requires option quote timestamps at or after selection. Fetch collects the extra stock observation when needed. Use exchange or SIP timestamps for this check. Provider session-close labels alone cannot establish the sequence.

## Volatility and portfolio risk

```sh
python3 optionsdesk.py iv-surface surface-input.json
python3 optionsdesk.py portfolio-stress book-and-scenarios.json
```

`iv-surface` takes `symbol`, `as_of`, both quote and underlying `source` and `feed` pairs, `max_quote_age_seconds`, `max_quote_skew_seconds`, `rate`, `dividend_yield`, and `contracts` with `expiry`, `strike`, `right`, and optional `expiry_at`. It returns bid, midpoint, and ask IV with market evidence and strike and calendar diagnostics. Rates and IV are decimal fractions. The implied vol convention uses a European pricing approximation. Pass an explicit `expiry_at` for any nonstandard close. [Cboe option valuation methodology](https://cdn.cboe.com/resources/indices/governance/Cboe_American_and_European-Style_Theoretical_Options_Calculation_Methodology.pdf)

`portfolio-stress` takes an `as_of` time, an `underlyings` map with `spot`, `rate` and `dividend_yield`, a list of signed stock or option `positions`, and scenarios with `name`, `at`, `spots`, and optional absolute `iv_shifts`. Each position specifies `kind`, `symbol`, `quantity` and `entry_price`. Options also specify `strike`, `right`, `expiry_at` and `iv`. Include bid, ask, and displayed sizes to estimate execution sensitivity. After an option's expiration, provide explicit `expiry_spots` by symbol and expiration timestamp. The report estimates scenario P&L, closing friction, and gross short-option assignment exposure before offsets. Optional `risk_limits` can cap `max_loss_from_mark_dollars`, `max_gross_assignment_cash_dollars`, and `max_synthetic_round_trip_cost_dollars`. It uses positions supplied in the input file and prices spread legs synthetically. [OIC assignment guidance](https://www.optionseducation.org/referencelibrary/faq/options-assignment) · [Cboe complex orders](https://www.cboe.com/markets/us/options/trading/complex-orders)

Set `advanced_greeks` to `true` in `portfolio-stress` to add signed position and underlying sensitivities. Vega, vanna, volga, and zomma use one percentage point of IV. Theta, charm, and color use one calendar day. Gamma and speed use a $1 stock move. The quadratic gamma estimate for a 1% move is a local sensitivity. Scenario P&L uses full repricing. Use `iv_shifts_by_position` with one-based option position numbers to apply strike-specific shifts in addition to the underlying's parallel IV shock. Exact model attribution averages the six orders of applying spot, IV, and time changes so mixed effects are shared. Attribution reports pre-cost mark changes and skips scenarios that reach any option's expiration. Run `python3 optionsdesk.py portfolio-stress examples/advanced-risk.json` for a synthetic example.

On `iv-surface`, the optional `event_variance` object takes `event_at`, `before`, `after`, `farther`, `max_log_moneyness_gap`, and `max_quote_skew_seconds`. The three node selectors are OCC symbols from the same surface and option right. Their expirations must surround one event, with one before and two after. The estimator uses the slope between the two later expiries as the background variance rate. It reports the event variance residual and bid/ask bounds. A band that includes zero leaves the estimate inconclusive. The estimate reflects market pricing under the model assumptions. [Scheduled earnings jumps](https://arxiv.org/abs/1412.8414)

`python3 optionsdesk.py hedge-replay examples/hedge-replay.json` compares unhedged, delta rebalancing, and a fixed no-trade band on the same supplied path. The model uses a single European cash-settled option with a 100-unit multiplier and a share hedge on the same price scale. It assumes the opening option fill at the supplied premium before the first sample. Every hedge requires an observable book after the decision and configured latency. Fills cross the spread, cap quantity at displayed size, and cancel any remainder. Unexecuted rebalances expire at settlement. If the terminal book cannot unwind the position in full, settlement P&L stays unavailable. The report includes modeled P&L, friction, turnover, and drawdown sampled at supplied quotes. The Risk view shows the bundled synthetic replay. Funding, stock borrow, hedge dividends, and market impact are outside the model.

## Research evidence and forecast validation

The [research program](RESEARCH.md) covers execution, earnings jumps, gamma, and model validation. The `price-limits` command evaluates a supplied limit grid against joint fill and liquidation scenarios. The bundled `examples/price-limits.json` is synthetic and can be run without connecting to the journal or a broker.

For contracts expiring on the decision date, supply the exact contract cutoff as `contract.expiry_at`. The command rejects decisions at or after that time, including on exchange early-close days.

`python3 optionsdesk.py candidate-review examples/candidate-review.json` runs the combined research gate. It matches the selected tested limit to the candidate's exact legs, quantity, cutoff, and fees, then adds the candidate to the supplied portfolio stress test. Supply an available loss budget after existing commitments and explicit loss and assignment limits. The gate includes any entry cost above the theoretical mark in its stress comparison. Model and scenario coverage determine the result. The bundled case is synthetic.

Use `candidate-review FILE --require-operational-context` for a strict rehearsal. The command exits nonzero if the review calls for passing on the candidate. Supply `operational_context.account_state` in the broker reconciliation format, `operational_context.market_intake` in the market scan format, and `operational_context.entry_cutoffs` keyed by exact option symbol with `known_at` and `last_entry_at`. Both account snapshots must be at most 30 seconds old at the candidate cutoff, or younger if the input specifies a tighter limit. Reconciled holdings must match the existing portfolio under stress. Working orders require review because their potential fills are outside the supplied stress book. The regular-session regime must be classified with complete fresh breadth. The contract entry cutoffs must have been available by the decision time. These checks operate on supplied evidence. Broker authentication, margin policy, corporate actions, and post-entry lifecycle remain separate controls.

`python3 optionsdesk.py candidate-size examples/candidate-sizing.json` compares whole-lot sizes of the same long option or debit vertical at one tested limit. Supply a `one_lot_candidate` in candidate-review format, a `sizes` list with `quantity` and explicit fill and exit `models` for each size, `net_liquidation_value_dollars`, `fractional_risk_scale`, `max_order_loss_fraction`, and `max_combined_stress_fraction`. For each lot count the desk calculates the worst expected log wealth change across the supplied models, after fees and modeled fills and exits. Other book P&L is held fixed for that objective. A separate check reprices the combined portfolio under the supplied shocks. The reference size maximizes the objective among the tested sizes that pass these gates. The fractional risk scale shrinks that reference size's modeled loss budget. If no passing integer size fits the scaled budget, the result is cash. The command also supports `--require-operational-context`. NLV and execution distributions are supplied research inputs. The example in the Risk view uses synthetic data. [Expected log growth](https://doi.org/10.1002/j.1538-7305.1956.tb03809.x)

### Expression and exit research

`python3 optionsdesk.py expression-review examples/expression-review.json` compares named candidates with cash. It holds the underlying, decision time, exit horizon, portfolio, risk budget, and scenario family names constant. Each candidate supplies exact legs and price, with a separate fill and exit distribution per tested lot count. The command ranks passing expressions by the worst expected log growth at the scaled whole-lot size. It accepts the same strict operational-context flag as candidate sizing. The model distributions and package books are supplied evidence and assumptions. Compare matched physical scenarios and real package observations when using the tool for prospective research.

`python3 optionsdesk.py exit-replay examples/exit-replay.json` compares a fixed exit time with a profit target, loss threshold, and the same scheduled fallback. Supply an exact net entry debit, spread width, size, fees, timestamps, and a path of executable-side package quotes for a single spread. The trigger uses a fresh bid and full displayed size. The modeled liquidation requires a distinct later observation inside the execution window and after the stated latency. A missing or thin book leaves the exit unresolved if no eligible later quote arrives. The replay uses displayed bid prices and models full-size crossings. Queue position, partial fills, and actual broker liquidation remain outside this model.

### Overnight variance challenger

`python3 optionsdesk.py overnight-variance sessions.json` compares a ridge-regularized HAR-style fit on total realized variance, a fit separating overnight and intraday inputs, and a 22-session mean. Each ordered session supplies `open_at`, `close_at`, `received_at`, `previous_session`, and `five_minute_prices` starting at the opening price and ending at the final five-minute close. Supply complete regular sessions with an exchange-verified calendar and consistently adjusted prices across corporate actions. Set `horizon_sessions`, `min_training_windows`, and `ridge_penalty` before evaluation. Both fits train on completed earlier windows whose labels were available before the forecast decision, and score the same non-overlapping targets using QLIKE. Save a usable file as `data/overnight-variance-input.json` to include the result in the Volatility view. The current repository has synthetic test coverage for this input format. [HAR realized volatility](https://statmath.wu.ac.at/~hauser/LVs/FinEtricsQF/References/Corsi2009JFinEtricsQF_LMmodelRealizedVola.pdf)

An optional `archive` object supplies the `source`, actual `downloaded_at` and assumed `availability_lag_seconds`. In archive mode omit `received_at` from sessions. The report labels availability as assumed from each market close. An optional `incumbent_power_law` object supplies `lookback_sessions` and `exponent` to benchmark fixed weights on the same realized-variance target. The local SPY input uses complete regular five-minute bars with full calendar alignment, excludes the interpolated prefix, and starts with the first contiguous complete segment. The [archived benchmark report](data/overnight-variance-report.json) contains the methods, provenance, paired sensitivity and per-window results.

`python3 optionsdesk.py regime-diagnostics regime.json` compares a fixed-parameter exponential Hawkes first-jump probability with a Poisson count baseline over the same forecast horizon. Provide `window_start`, `as_of`, `forecast_seconds`, `decay_seconds`, `branching_ratio`, `background_jumps_per_second`, and `jumps` with market and receipt timestamps. The window provides the available jump history. Supply `assets` and `aligned_returns` rows with `observed_at`, `received_at`, and a `returns` map over the same aligned return intervals. The correlation report shows average pairwise correlation, the largest eigenvalue's share, and normalized entropy of the correlation spectrum. Save a usable input as `data/jump-regime-input.json` to show the result in Volatility. These diagnostics use supplied parameters and remain research features until a forward comparison establishes their value. [Hawkes processes](https://doi.org/10.1093/biomet/58.1.83)

`python3 optionsdesk.py broker-reconcile state.json` compares full, normalized broker and local snapshots at an explicit `as_of`. Set `max_age_seconds` and `max_observation_skew_seconds`. The `broker` and `ledger` snapshots each contain `account`, `observed_at`, `received_at`, `cash` keyed by currency and balance type, `positions` keyed by exact instrument, and `working_orders` keyed by canonical order ID. Single-instrument orders carry `instrument`, `side`, `remaining_quantity`, `type`, `time_in_force`, and any applicable `limit` and `stop` prices. Normalize identifiers and ensure both snapshots cover the same cash buckets, instruments, and active orders before comparing. A mismatch or unusable snapshot returns review required and a nonzero exit code. The command reads supplied files only.

```sh
python3 optionsdesk.py graph-import evidence.json
python3 optionsdesk.py graph-query evidence-cutoff.json
python3 optionsdesk.py forecast-validate --phase pre --target debit_spread_profitable
```

`graph-import` accepts a `document` with `source_url`, `title`, `text`, `published_at`, and `first_seen_at`, and a `claims` list. Each claim includes typed `subject` and `object` (`type` and `key`), `predicate`, source-text `span` (`start`, `end`, and `text`), and `origin` and `extractor`. Provide `provider`, `model`, and `prompt_id` in the `extractor` object for model proposals. A claim may specify `valid_from`, `valid_to`, and the ID it `supersedes`. A model proposal cannot supersede human evidence. Record only the excerpt required for the claim when storing third-party text. For date-only announcements, use a conservative known-by bound and preserve actual receipt and ingest times. [W3C provenance ontology](https://www.w3.org/TR/prov-o/)

`graph-query` takes `as_of` and optional `effective_at`, `subject`, and `include_model`. This preserves the distinction between when a relationship applied and what the desk actually knew at the decision time. The local Research Monitor displays recent excerpts and identifies model proposals. Importing an extraction verifies exact citation spans and lineage. Semantic review remains a separate step.

The forecast benchmark compares a smoothed historical base rate with ridge-regularized logistic regression. It uses contemporaneous option and stock features and scores stock direction or either option structure's positive replay P&L. Pre- and post-release phases are evaluated separately. Walk-forward training uses only earlier event days and labels available before the test cutoff. Brier score, log loss, and calibration bins stay unavailable until the cohort has enough dated outcomes for the 20-day training and 10-day test minimums. These are research thresholds. Paper performance and execution remain in the scorecard. [Proper scoring rules](https://doi.org/10.1198/016214506000001437)

### Cited post-release forecasts

```sh
python3 optionsdesk.py event-quantile-register recipe.json
python3 optionsdesk.py graph-import cited-release.json
python3 optionsdesk.py event-quantile-seal forecast-pair.json
python3 optionsdesk.py event-quantile-report
```

The recipe file specifies `experiment_id` and a `recipes` object with versioned identifiers `numerical` and `with_claims`. Register the recipes before the issuer publishes. Use the same numerical model and inputs across the two arms and vary only the cited features. The desk records the supplied recipe identifiers and forecast outputs. Model fitting and claim extraction happen upstream.

In the graph import, attach each claim to the registered event using a subject with type `event` and its `event_id` as key, a `reports` predicate, and a `metric` object. Use the issuer release's exact HTTPS URL and publication timestamp with the supporting text span. The forecast file supplies `event_id`, `claim_ids`, and a `forecasts` object keyed by the two arm names. Each arm supplies ordered return quantiles in basis points with keys `0.10`, `0.25`, `0.50`, `0.75`, and `0.90`. Seal both by the registered post cutoff, then record the regular post phase and resolve its exit. The post phase must confirm the same release URL and publication timestamp. The desk retains one immutable pair per event and reports missing or mismatched evidence explicitly.

The target is the percentage change in the eligible underlying trade prices from the post cutoff to the fixed exit, expressed in basis points. The report gives both arms the same event outcomes, weights decision dates equally, and shows mean pinball loss and observed coverage per quantile. The Research Monitor summarizes the comparison. Twenty-five scored dates trigger a research review. Option P&L, slippage, and fill probability need their own evidence. [Quantile scoring rules](https://doi.org/10.1198/016214506000001437)

The 2026 directional study compares each stock's return over 20 completed sessions with QQQ's and scores the sign of the stock's next closing move around earnings. The run archives its input hashes, per-event endpoints, accuracy against constant-direction baselines, and an optional option comparison using the earlier closing-quote replay. The directional outcome uses stock bars and can be scored where option liquidity is missing. Results are retrospective.

```sh
python3 earnings_direction.py data/plans/earnings-relative-strength-2026.json \
  data/earnings-relative-strength-2026-input.json \
  --option-replay data/eight-name-earnings-eod-cohort.json
```
