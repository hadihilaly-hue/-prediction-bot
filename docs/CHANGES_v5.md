# v5 changes (2026-11-03)

v5 builds on v4 (`docs/CHANGES_v4.md`) and the credit cadence work (`docs/CHANGES_cadence.md`, which also calls itself "v5"; this file covers the v5 brief's phases). At the taker ask, after the 0.07·P·(1−P) fee, Kalshi game markets track the devigged sharp consensus to within the fee in every window the bot has sampled. So the bot "barely trades" because its rules are working, and the realized P&L is variance on an edge that is roughly zero or slightly negative. v5 sets out to:
- measure against the right benchmarks;
- spend odds credits where repricing happens;
- simulate maker execution properly;
- stop buying the price bands that are reliably negative.

It is still paper-only: no live-order path, no credential changes, and every ledger change is additive.

**Status.** All phases are merged: Phases 0–2 ([#60](https://github.com/hadihilaly-hue/-prediction-bot/pull/60), [#61](https://github.com/hadihilaly-hue/-prediction-bot/pull/61), [#62](https://github.com/hadihilaly-hue/-prediction-bot/pull/62)), Phase 3 ([#63](https://github.com/hadihilaly-hue/-prediction-bot/pull/63)), Phase 4 ([#65](https://github.com/hadihilaly-hue/-prediction-bot/pull/65), re-opened against `main` after #64 merged into its stacked base), Phase 5 ([#66](https://github.com/hadihilaly-hue/-prediction-bot/pull/66)), Phase 6 ([#67](https://github.com/hadihilaly-hue/-prediction-bot/pull/67)), Phase 7 ([#68](https://github.com/hadihilaly-hue/-prediction-bot/pull/68)) and Phase 8 ([#69](https://github.com/hadihilaly-hue/-prediction-bot/pull/69)).

## What changed, by phase

| Phase | PR | What |
|---|---|---|
| 0: measurement | [#60](https://github.com/hadihilaly-hue/-prediction-bot/pull/60) | `clv_sharp_close` / `clv_sharp_source`, per-book odds and the Kalshi event ticker on `odds_snapshots`, `pbot clv-report` (text and `--json`), dashboard panel, `docs/evidence/2026-11-03.json` |
| 1: fee model per series | [#61](https://github.com/hadihilaly-hue/-prediction-bot/pull/61), stacked on #60 | One fee helper `fees.fee_for_ticker`; the v1 path now uses each series' fee type and multiplier; `fee_model_version='kalshi-series-v2'` |
| 2: entry rules | [#62](https://github.com/hadihilaly-hue/-prediction-bot/pull/62) | 25c price floor and a doubled threshold below 40c; soft reference off in the workflow; entry window as config (72–168h); shadow-only NHL/NBA/NCAAB measurement pulls (≤2 credits/day); price-floor and thin-market rows kept as near misses with depth and open interest |
| 3: pull plan | [#63](https://github.com/hadihilaly-hue/-prediction-bot/pull/63) | Per-game T−24h/T−6h/T−90m/T−10m targets (`pull_plan`) sharing each day's credits with the 72h+ rotation; pulls on any 30-min slot; shadow-only sharp-move detector (`sharp_move`, `sharp_move_follow`); `odds_budget.py` pull-plan section |
| 4: maker queue | [#65](https://github.com/hadihilaly-hue/-prediction-bot/pull/65) (first [#64](https://github.com/hadihilaly-hue/-prediction-bot/pull/64)) | `/markets/trades` and depth-20 `orderbook_snapshots`; maker shadows record their queue and price rule (bid+1c, or at the bid on a 1c spread); queue-model fills from the trade tape beside the candle rules; maker gate now decides on queue fills with clean CLV |
| 5: Polymarket reference | [#66](https://github.com/hadihilaly-hue/-prediction-bot/pull/66) | Read-only Polymarket client; tracked Kalshi events matched by team aliases and kickoff; `reference_quotes` mids on scanner runs (≤200 requests); `clv_polymarket` in every CLV table and `clv-report`; `pbot reference-gaps` |
| 6: scheduler reliability | [#67](https://github.com/hadihilaly-hue/-prediction-bot/pull/67) | Cloudflare Worker cron (`infra/cloudflare-trigger`) dispatching `paper.yml` with a `slot` input; `pbot slot-guard` against double runs; `pbot health` and an hourly `health.yml` that opens a "paper bot missed runs" issue |
| 7: audit and sizing | [#68](https://github.com/hadihilaly-hue/-prediction-bot/pull/68) | Stale-book check fails closed; Kalshi timestamps always aware; order-book parser tolerant of key and unit variants; `PBOT_SIZING_MODE` (`flat` default, `kelly` capped by supported cohort CLV) |
| 8: dashboard and docs | [#69](https://github.com/hadihilaly-hue/-prediction-bot/pull/69) | Dashboard "Measurement health" section (CLV by cohort with CIs, share beating the close, realized vs model-expected P&L, maker fill rate and drift, feed freshness, runs per day, rejections per day); README "Current rules (v5)" and "How to read the evidence" |

## Phase 0: measurement fixes

### What each CLV column measures

All values are in cents per contract and are positive when the price moved our way.

| Column | Benchmark | Where it is set |
|---|---|---|
| `clv_kalshi` | Kalshi **mid** for the side at T−5 min, minus entry | `clv.record_clv` (unchanged) |
| `clv_kalshi_bid` | Kalshi **side bid** at T−5 min (the price you could sell at), minus entry | `clv.record_clv` (unchanged; same value as `clv_exit`) |
| `clv_gameday` | Kalshi mid at 10:00 ET on game day, minus entry | `clv.record_clv` (unchanged) |
| `clv_sharp_close` (new) | Devigged sharp-book fair for the side at the close, minus entry | `clv.record_sharp_clv` (new) |
| `clv_sharp_source` (new) | `pinnacle`, `circa`, `betonlineag` or `consensus` | `clv.record_sharp_clv` |

The two new columns are on `fills`, `shadow_decisions` and `maker_shadow`. The names `clv_kalshi` and `clv_kalshi_bid` stay as they are, so the dashboard and older queries keep working.

### How `clv_sharp_close` is computed

1. **The close.** It is the latest quote from each sharp book whose own `last_update` is between kickoff − 20 min and kickoff, in a snapshot taken before kickoff. A quote with a missing or unreadable `last_update` never counts.
2. **The book.**
   - Pinnacle, whenever it qualifies.
   - Otherwise Circa or BetOnline (`betonlineag`), whichever one qualifies.
   - If both qualify without Pinnacle, their `BOOK_WEIGHTS` mean (3:2), recorded as `consensus`.
   - DraftKings and FanDuel are never used.
3. **Devig.** Power devig (the v3 default) on that book's own prices for every outcome.
4. **The side.** For a YES on the leg, the fair is P(leg); for a NO, it is 1 − P(leg). The leg comes from the ticker suffix (a team code, or `TIE`/`DRAW` as the draw).
   - In 3-way soccer a NO on team A is therefore 1 − P(A), which equals P(B) + P(draw), not P(B).
   - This uses the ticker, not `position_key`: v2/v3 keys map a 2-way NO to the other team and a 3-way NO to `not:<team>`, and v1 rows have no key at all.
5. **When it is scored.** Each paper cycle, after `record_clv`. It runs only local SQL (no API calls) and only for rows whose game started within the last 7 days. Rows with no qualifying quote stay NULL.

### Ledger additions
- `odds_snapshots.book_odds_json`: every book's raw decimal odds and `last_update`, for example `{"pinnacle": {"odds": {"Arizona Cardinals": "2.10", ...}, "last_update": "..."}}`.
- `odds_snapshots.event_ticker`: the Kalshi event the v2/v3 strategy matched this Odds API game to (`SportsbookV2Strategy.seen_event_links`, passed by `Engine` to `Ledger.record_consensus`). Partial index `odds_snapshots_event_ticker`.
- `clv_sharp_close` and `clv_sharp_source` on `fills`, `shadow_decisions` and `maker_shadow`.

### `pbot clv-report`

`pbot clv-report [--since 2026-10-24] [--json]` prints one block per cohort:
- **fills**: all `fills` rows;
- **v3_net_edge**: v3 near misses blocked by `net_edge`;
- **v3_entry_window**: v3 near misses blocked by `entry_window`;
- **line_move_follower**: v2 `line_move_follower` shadows.

Only `clv_status='ok'` rows count. Each block covers `clv_kalshi`, `clv_kalshi_bid` and `clv_sharp_close`, with n, mean, sd, se, normal 95% CI, the share of rows beating the close (CLV > 0) and a verdict. Each is shown overall and broken out:
- by window (`72h+`, `24-72h`, `2-24h`, `<2h`);
- by price band (<0.25, 0.25–0.40, 0.40–0.60, 0.60–0.80, ≥0.80);
- by sport (series prefix);
- by net_edge bucket.

For fills, which have no stored window, the window is derived from `created_at` and kickoff with the same `decision_window` helper the shadows use. The dashboard has the same table under Details → "Closing-line evidence by cohort", with a cohort picker.

Verdicts follow the v4 rule:
- **supported**: n ≥ 50 and the 95% CI lower bound is above 0;
- **rejected**: n ≥ 50 and the upper bound is below 0;
- **open**: otherwise.

### Baseline (`docs/evidence/2026-11-03.json`)

Generated with `pbot clv-report --since 2026-10-24 --json` against `paper.sqlite` at paper-ledger commit `8d50f46` ("paper cycle 2026-11-03T16:19:51Z"). Later PRs compare against this file.

| Cohort | n | vs T−5 mid | vs T−5 bid | 95% CI (bid) | Beat close (bid) | Verdict (bid) |
|---|---:|---:|---:|---|---:|---|
| v3 `net_edge` near misses (all 72h+) | 336 | −0.51c | −1.01c | −1.50 to −0.53 | 28% | rejected |
| v3 `entry_window` near misses | 576 | −0.51c | −1.01c | −1.26 to −0.75 | 26% | rejected |
| … 24–72h | 383 | −0.53c | −1.03c | −1.36 to −0.69 | 25% | rejected |
| … 2–24h | 193 | −0.46c | −0.96c | −1.36 to −0.56 | 26% | rejected |
| v2 `line_move_follower` | 28 | +0.93c | +0.43c | −1.14 to +2.00 | 50% | open |
| fills | 1 | +7.50c | +7.00c | – | – | open |

`net_edge` near misses (bid) by price band:

| Price | n | Mean | Verdict |
|---|---:|---:|---|
| <0.25 | 60 | −1.73c | rejected |
| 0.25–0.40 | 70 | −2.06c | rejected |
| 0.40–0.60 | 80 | −1.05c | rejected |
| 0.60–0.80 | 95 | −0.04c | open |
| ≥0.80 | 31 | −0.13c | open |

`net_edge` near misses by net_edge bucket:

| Bucket | n | Mean (bid) | Mean (mid) |
|---|---:|---:|---:|
| <−5c | 3 | −4.00c | −3.50c |
| −5..−3c | 41 | −2.24c | −1.74c |
| −3..−2c | 120 | −1.19c | −0.69c |
| −2..−1c | 126 | −0.69c | −0.19c |
| −1..0c | 41 | −0.37c | +0.13c |
| 0..+1c | 4 | +1.75c | +2.25c |
| ≥+1c | 1 | +2.00c | +2.50c |

Notes on these numbers:
- CLV rises with net_edge in both cohorts. The `entry_window` cohort's 0..+1c bucket is +2.50c vs bid on n=10 (CI +0.89 to +4.11). That is interesting, but it is far below 50, so the verdict is open.
- In all 1,852 scored `shadow_decisions` rows the mid is exactly 0.5c above the bid: every T−5 spread was 1c, so the two Kalshi measures differ by a constant here.
- `clv_sharp_close` has n=0 everywhere, as expected. Before this PR, snapshots kept only the blended probability (no per-book prices, no Kalshi event), and only 69 of 10,245 snapshots since Oct 24 had any book updated within 20 minutes of kickoff. The column starts filling for games whose feed is pulled close to kickoff, which on the current 2-hour grid is rare. Phase 3's T−10m pull is what makes it routine.
- Only one fill since Oct 24 has a clean close (fill 87, +7.0c vs bid). The 86 v1-era fills are excluded as `in_game` or `no_commence`, as in v4.

### Acceptance

`pbot clv-report` reproduces Ground truth item 3:
- `net_edge` near misses: n=336, `clv_kalshi_bid` −1.01, `clv_kalshi` −0.51, sd 4.53, se 0.25 (the brief quotes sd 4.52; the value is 4.527).
- By window: −0.46 (2–24h, n=193), −0.53 (24–72h, n=383), −0.51 (72h+, n=336).
- Bucket means vs bid: −4.0, −2.2, −1.2, −0.7, −0.4, +1.8.
- `line_move_follower`: n=28, +0.43 vs bid, +0.93 vs mid.

### Deviations from the brief
- **The Appendix A bucket query is wrong as written.** `net_edge` is a TEXT column, so `net_edge < -0.03` compares strings (`'-0.026' < '-0.03'`) and returns 1246/0/0/297/30/4. With `CAST(net_edge AS REAL)` it returns the brief's 297/540/546/160/30/4. `clv_report` parses every value in Python and has a test for this.
- **No historical sharp close.** The brief's source for `clv_sharp_close` was the existing `odds_snapshots`. Those rows had no per-book prices and no Kalshi event, so this PR adds `book_odds_json` and `event_ticker`, and rows from before this PR stay NULL (agreed with Hadi). History is not backfilled from the blended probability, because that blend includes DraftKings and FanDuel.
- **Freshness uses the chosen book's own update time**, not the snapshot's `max_book_update`. `max_book_update` is the newest update across all books, so a fresh DraftKings line would otherwise vouch for a stale Pinnacle one.
- **Seven net_edge buckets, not six.** A ≥+1c bucket is added so no row drops out of the table.
- **`rejected` also needs n ≥ 50**, matching the v4 evidence rule.

### Not verified here (network)

This session's network policy blocks `kalshi.com`, `api.elections.kalshi.com` and the Polymarket APIs. Phase 0 needs none of them. Phase 1.2 (fee PDF), 4.1 (`/markets/trades`), 5 (Polymarket) and 7.1a (`liquidity_dollars`) do; see the decisions below.

## Phase 1: fee model per series

### One helper

`fees.fee_for_ticker(ticker, price, contracts, style, *, schedule, rounding)` is now the only fee formula:

coefficient · `fee_multiplier` · C · P · (1 − P)

- The coefficient is 0.07 for takers.
- For makers it is 0.0175 on `quadratic_with_maker_fees` series and 0 on `quadratic` series.
- `schedule` resolves the ticker's series to `(fee_type, fee_multiplier)`. It is either the run's `SeriesFeeCache`, or `FixedFee(...)` built from the schedule a signal already carries.
- `rounding` is `cent` or `centicent` (rounded up), or None for the unrounded per-contract cost used in net-edge maths.
- An unknown series schedule or fee type raises, so every caller fails closed. Taker fees on an unknown fee type now fail closed too; before, only maker fees did.

Replaced call sites:
- **Fee functions:** `taker_fee`, `maker_fee`, `taker_fee_rate`, `models.fee_for` and `Engine._fee_multiplier` are removed.
- **Call sites rewritten:** every use in `sportsbook_v2.py` (net edge, maker price, sizing, maker shadows, reserved cost), `engine.py` (affordability, paper fills, paper orders), `strategies/base.py`, `book_scanner.py` and `scripts/replay.py`.
- **Settings:** `taker_fee_multiplier` and `maker_fee_multiplier` (0.07 / 0.0175, which were really coefficients) are removed. Settings ignore unknown env keys, so a leftover env var is harmless.

### What changes in behavior
- **v1 strategies** (`book_scanner`, `sportsbook_arb`, `underdog_value`) used a flat schedule: 0.07 taker and 0.0175 maker on every series.
  - The CLI now gives every strategy in a run one shared `SeriesFeeCache`.
  - MLB game markets therefore pay half the fee, and MLS and UFC maker orders pay none.
  - `make_signal` stamps the series `fee_type` and `fee_multiplier` on the signal, so the engine charges what the strategy priced.
  - Strategies built without a Kalshi client and ledger (offline `pbot scan`, unit tests) keep Kalshi's general schedule (`DEFAULT_FEE_SCHEDULE`, multiplier 1 with maker fees).
- **v2/v3** already read per-series schedules; their numbers are unchanged.
- **Version tag.** Every new fill, including filled paper orders, records `fee_model_version='kalshi-series-v2'`. Historical rows keep what they have: 86 v1-era fills have none, and fill 87 has `kalshi-series-v1`.

### Series schedules in the ledger (`series_fees`, from Kalshi's public `/series`)

| Series | `fee_type` | Multiplier |
|---|---|---:|
| NFL, NCAAF, NBA, NHL, EPL, UCL | `quadratic_with_maker_fees` | 1 |
| MLB game | `quadratic_with_maker_fees` | 0.5 |
| MLS, UFC | `quadratic` (no maker fee) | 1 |

`tests/test_fees.py` holds the acceptance table: hand-computed (series, price, count, style, rounding) → fee rows for NFL, MLB game, MLS and UFC, resolved through `SeriesFeeCache`.

### Effect on the 87 historical fills (recomputed, not rewritten)

| | Fees |
|---|---:|
| Booked at the time | $65.66 |
| Per-series schedule, cent rounding | $63.26 |
| Per-series schedule, centicent rounding | $62.80 |

- The $2.40 between the first two lines is all MLB: 5 fills paid the full fee instead of the 0.5 multiplier.
- Centicent rounding would have saved another $0.46 over 87 fills and 6,072 contracts. That is about 0.5c per order, or 0.76c per 100 contracts, because the rounding loss is at most 1c per order whatever its size.

### 1.2 Rounding: RESERVED

The brief asks for the current rounding rule to be checked against Kalshi's fee schedule PDF (dated 7 July 2026). This session's network policy blocks `kalshi.com`, so I could not read it. I'm also not certain from memory which rule it states, so I have not guessed.

`PBOT_FEE_ROUNDING` already exists and defaults to `cent` (each fee rounded up to the cent). `centicent` rounds up to $0.0001. The workflow is unchanged. See decision 2 below.

### Acceptance
- The `tests/test_fees.py` table covers:
  - NFL: multiplier 1, maker fees;
  - MLB game: multiplier 0.5;
  - MLS and UFC: `quadratic`, no maker fee.
- It includes centicent rows and unrounded per-contract rows.
- Other new tests cover:
  - the v1 `make_signal` maker edge on a `quadratic` series;
  - the book scanner and engine booking 18c MLB legs, where the general schedule booked 35c, with `kalshi-series-v2`;
  - one shared cache across the CLI's strategies;
  - failing closed on an unknown schedule, fee type or rounding.

## Phase 2: entry rules

### 2.1 Threshold in the open
- The effective v2/v3 threshold is `PBOT_V2_MIN_NET_EDGE`, which now appears in `paper.yml` (repo Variable, default 0.02).
- `PBOT_MIN_EDGE: "0.01"` stays, because it still drives the v1 shadows. Its comment now says it applies to v1 only.
- The "1c after fees" claim was in the workflow comment and `docs/PIPELINE.md`, not the README; both are corrected.
- Every `net_edge` block detail now reads `net_edge=<edge> threshold=<threshold>`, so the threshold in force is on record.

### 2.2 Price band
- **Floor:** `v2_price_floor` default 0.10 → **0.25**.
- **Low-price threshold:** new `v3_low_price_threshold_multiplier` (default 2.0) applies below `v3_low_price_below` (default 0.40). v3 then needs a 4c net edge under 40c. These rows are tagged `low_price_threshold`.
- **Near-miss logging kept for blocked rows:**
  - A row under the floor used to be blocked before any near-miss logging. It is now saved as a near miss with `reason='price_floor'`, still carrying its fair and net edge.
  - It is scored like the other near misses, and `pbot clv-report` has a `v3_price_floor` cohort.
  - The `net_edge` and `entry_window` cohorts keep their old definitions.

### 2.3 Reference books
- **Soft reference off.** The workflow sets `PBOT_V3_ALLOW_SOFT_REFERENCE=false` (repo Variable to override), agreed with Hadi. The config default stays `true`. With it off, a game quoted only by DraftKings/FanDuel has no fair and is blocked as `few_books`. DraftKings and FanDuel are still pulled, still count toward the book total and the dispersion check, and remain available to Phase 3's move detector.
- **Circa stays in `PBOT_ODDS_BOOKMAKERS`.** The brief replaces it only if it quotes under 10% of events over 14 days. That can't be measured yet:
  - `odds_snapshots.books_used` is a count, not a list of books;
  - per-book prices were never stored before Phase 0's `book_odds_json`;
  - only one signal since Oct 20 has `book_probs_json`.
  The only sample, docs/PIPELINE.md's Oct 23 NFL pull, had Circa on 0 of 27 events. Once Phase 0 ships, the share can be read with `json_extract(book_odds_json,'$.circa')` over 14 days, and the 10% rule applied then.

### 2.4 Entry window as config
- **Keys:** `PBOT_ENTRY_WINDOW_MIN_HOURS` (72) and `PBOT_ENTRY_WINDOW_MAX_HOURS` (**168**, agreed with Hadi; the brief said 240).
  - Before this, the v3 strategy had no upper bound, and feed planning looked 7 days (168h) ahead.
  - 168h keeps credit spend where it was.
  - v3 now blocks games more than 168h out as `entry_window`. They are still shadow-logged like any other entry-window row. 0 turns either bound off.
- **`min_lead_hours`** follows `entry_window_min_hours`: setting either sets both, so `PBOT_MIN_LEAD_HOURS` keeps working.
- **Feed planning** (`feed_scores`, `rotation_scores`, `select_rotation_feeds`, `odds-trigger`) uses `entry_lookahead(settings)`, the max.
- **Shadow logging** for `<2h`, `2-24h`, `24-72h` and `72h+` is unchanged.
- **RESERVED:** moving the live window to 1–48h. See the decisions below.

### 2.5 Feed eligibility and measurement pulls
- `feed_selection.eligible()` already required `lead_p90_h >= min_lead_hours + 6`. Since `min_lead_hours` now follows `entry_window_min_hours`, that is the brief's rule.
- **New `PBOT_MEASURE_FEEDS`** (default `icehockey_nhl,basketball_nba,basketball_ncaab`) and **`PBOT_MEASURE_DAILY_CREDIT_CAP`** (default 2). On rotation runs, `select_measure_pull` offers at most one pull:
  - It looks at each measure feed's earliest upcoming Kalshi game.
  - The pull is offered while the run is within ±1h of that game's T−6h or T−90m. The width is so a 2-hourly sportsbook slot lands inside it.
  - It must be a target not yet pulled. Finishing a game already started comes first, then the feed with the fewest pulls in 7 days, then configured order.
  - A free `/events` check must show the game listed.
- **The credit is extra, but capped in the pacer.** `OddsPacer.grant_measure_credit` adds one credit to the run's budget only when:
  - fewer than the cap's measurement pulls are recorded for the UTC day;
  - the run is not a trigger run;
  - at least 25 credits would remain afterwards.
  The credit is left out of the carry, so it never borrows from the next run's share. Spending it lowers the month's remaining credits, which shrinks later shares on its own.
- **Shadow-only by construction.**
  - The strategy treats every event in a measure feed as an entry-window event, so it can never trade. Its near misses carry the tag `measure_only`.
  - `league_disabled` is skipped for measure feeds. NBA is in `v2_disabled_sports`, which exists to stop trades, and these events cannot trade.
- **Ledger:** each pull is recorded in a new `measure_pulls` table (feed, event, target, kickoff).

### 2.6 Depth on near misses
- New `shadow_decisions.ask_depth` and `open_interest` columns are set on every near-miss row. Tags are now stored on near misses too.
- `v3_min_ask_depth` stays 50 for entries.
- **Thin-market rows now logged.** A thin book is still blocked as `thin_market`, but the row is also saved as a near miss with `reason='thin_market'`, its fair and its net edge. It uses the same reference and fair as the main path, and rows whose reference fails are skipped. Before this, no near miss could have depth under 50, so the rule couldn't be scored against CLV. `pbot clv-report` has a `v3_thin_market` cohort.

### Acceptance: `scripts/replay.py --rules v5`

`run_v5_bucket_replay` is read-only.
- **Rules applied:** the 2.2 and 2.3 rules, in the strategy's gate order (soft reference, price floor, low-price threshold).
- **Inputs:** each fill's recorded net edge; for pre-v4 fills, fair − price − fee paid per contract.
- **Outputs:** per-fill P&L, and CLV for `clv_status='ok'` rows only.

On paper-ledger `8d50f46`:

| v5 bucket | Fills | Realized P&L | CLV n | Bid CLV | Mid CLV |
|---|---:|---:|---:|---:|---:|
| soft_reference | 1 | −$32.68 | 1 | +7.00c | +7.50c |
| price_floor | 50 | +$944.96 | 0 | – | – |
| low_price_threshold | 10 | −$105.12 | 0 | – | – |
| unchanged | 26 | −$247.14 | 0 | – | – |

| Near misses | Moves to `price_floor` | CLV n | Bid CLV | Stays | CLV n | Bid CLV |
|---|---:|---:|---:|---:|---:|---:|
| v3 `net_edge` (1,577) | 298 | 60 | −1.73c | 1,279 | 276 | −0.86c |
| v3 `entry_window` (693) | 139 | 115 | −1.04c | 554 | 461 | −1.00c |

How to read this:
- **The floor would have removed every winner.** 61 of the 87 fills change bucket, and the 50 under 25c made +$944.96: 35 `sportsbook_arb` and 15 `underdog_value`, all from v1. Those 50 hold the winning legs of the five longshot events: +$1,221.20 from their under-25c fills, and +$1,195.74 for the five events overall. That is the variance the brief describes.
- **Their CLV can't be judged.** None of them has a clean close (`in_game` or `no_commence`).
- **The near misses are the usable evidence.** Under 25c, the 60 scored `net_edge` near misses are −1.73c vs bid, and the band's verdict in `pbot clv-report` is "rejected".
- **Fill 87,** the only v3 fill, moves to `soft_reference`. It lost $32.68 with +7c CLV.
- **Caveat:** v1 fills had no v3 net edge, so their low-price bucket uses v1's own fair. Treat that row as indicative.

### Not changed in Phase 2
- The dashboard's existing near-miss panels still read only `net_edge` and `entry_window`. The new cohorts are in the Phase 0 report panel.
- The maker price path (`_v3_maker_price`) still uses the flat 2c threshold. Phase 4 reworks the maker path.

## Phase 3: spend odds credits where repricing happens

### Why it isn't the brief's straight replacement
- **The brief:** replace the 2-hour grid with per-game targets at T−24h, T−6h, T−90m and T−10m for every eligible game.
- **Measured cost on the `8d50f46` ledger:** games kicking off Oct 20 – Nov 2, grouped by feed and kickoff, would have cost about **588 credits in 14 days, ~42 a day**. The free tier allows ~16 a day.
- **Cost by feed** (14 days): NCAAF 101, Serie A 92, La Liga 80, MLS 59, Ligue 1 52, Liga MX 51, EPL 48, NFL 36, Bundesliga 36, UFC 33.
- **No trading:** all four targets fall inside 24h, while the live entry window is 72h+. Replacing the grid would leave the bot no 72h+ odds and so no entries, which in effect flips the RESERVED window decision.
- **What was built (agreed with Hadi):** a **hybrid**. The rotation keeps `PBOT_ODDS_ENTRY_SHARE` (0.5) of each run's credit for 72–168h entry pulls. The pull plan spends the rest, close first, and its pulls can run on any 30-minute slot.

### 3.1 Per-game targets (`pull_plan`)
- **Planning** (`pull_plan.plan_pulls`, local SQL only). Every paper run and `pbot pull-due` plans targets for every event that kicks off within 26h in a feed whose latest `feed_stats` row is eligible.
  - Kickoff is the linked Odds API `commence_time` (Phase 0's `odds_snapshots.event_ticker`) when known.
  - Otherwise it is the latest `market_snapshots.expected_expiration` minus the pre-game offset: 2h for soccer, 2.5h for NBA, 3h otherwise.
  - Rows go into the new `pull_plan` table (event, feed, target, kickoff, target_at, window, priority, status, credits, timestamps). Targets that expire unpulled become `missed`, or `skipped_budget` if they were due but unfunded.

| Target | Window | Priority |
|---|---|---|
| T−10m (sharp close) | kickoff −20m to −2m | 1 |
| T−90m | ±30m | 2 |
| T−6h | ±1h | 3 |
| T−24h | ±1h | 4 |

- **Grouping.** One pull of a feed satisfies every due target of that feed, and the credit is split across them. Six 1 pm NFL games cost one credit per target. Targets also count as pulled, at zero plan credits, when their feed was pulled anyway for a rotation, move-trigger or measurement pull.
- **Budget.**
  - The plan may spend `floor(day budget × (1 − entry share))` credits per UTC day. The day budget is the cadence planner's figure for today, else the last known balance spread over the days left.
  - A lower-priority target is funded only if credits left today exceed the higher-priority pull groups still ahead today, so the close is never starved by an earlier T−24h.
  - `OddsPacer` stays the hard cap. `rotation_share` scales the rotation's per-run credit, and plan credits never borrow from the carry.
  - Plan credits stop at the 25-credit reserve.
  - The free `/events` check runs before every plan pull, and `x-requests-last == 1` is still asserted.
- **Slots.**
  - Sportsbook (2-hour) runs add one funded plan feed on top of the rotation.
  - Scanner slots run `pbot pull-due`. When a funded target is due, they run `pbot paper -s sportsbook_v3 --cycles 1 --plan FEED`: one feed, one credit at most, carry untouched, `runs.trigger_reason='plan:FEED'`.
  - A due plan target wins over a move trigger on the same slot.
- **Timing.** The workflow's :17/:47 slots land 13 minutes before :30/:00 kickoffs, so the T−10m close usually falls in the 20-minute sharp-close window. Kickoffs at other minutes can miss it. A 17:45 UCL kickoff has no slot in 17:25–17:43, and its close is recorded as `missed`, not taken late. Phase 6's scheduler doesn't change this, because it uses the same minutes.

### 3.2 Sharp move detector (shadow only)
- **Trigger.** For every v3 event pulled, the strategy compares the devigged sharp fair (`clv.sharp_fairs`: Pinnacle, else Circa/BetOnline) with the event's previous linked snapshot taken within 48h. The reference must be the same book both times; a switch from Circa to Pinnacle is not a move.
- **On a move of 2c or more on a leg:**
  - It fetches that market's Kalshi orderbook (free; at most 20 per run).
  - It saves a near miss with `reason='sharp_move'` and tags `move_<cents>`, plus one of `kalshi_lagged`, `kalshi_followed` or `kalshi_unknown` (no earlier Kalshi snapshot).
  - "Lagged" means the Kalshi ask for the side moved less than half the sharp move since the earlier pull.
- **Follow cohort.** A lagged side that then passes every normal v3 gate (net edge at the side's threshold included) is also saved as a would-trade shadow with `reason='sharp_move_follow'`.
  - The trade decision itself is unchanged: such a side trades exactly when it would have without the detector.
  - The cohort is measured only, until `pbot clv-report` calls `v3_sharp_move_follow` "supported".
- **Scoring:**
  - Both cohorts are scored for CLV and have `clv-report` rows.
  - CLV scoring now also takes `<2h` near misses. Before, it skipped them, which would have left the T−90m and T−10m rows (and Phase 2.5's measurement rows) unscored.
- Like the existing `odds_triggers` (Kalshi ask moves) and v2's `line_move_follower`, this needs at least two pulls of the same event. With the pull plan, more events now get several pulls inside 48h.

### 3.3 Credit report
- `scripts/odds_budget.py` now ends with a pull-plan section:
  - by target: planned, pulled, skipped_budget, missed, plan credits, useful ratio;
  - by feed: targets, pulled, plan credits, useful ratio.
- The useful ratio is pulled / (pulled + skipped_budget + missed + no_events).
- No `pull_plan` rows exist until this ships, so the first week's table will be added from the paper ledger after a week of runs.

### Acceptance
- **`tests/test_pull_plan.py`** simulates every :17/:47 slot of November 2026:
  - the real `data/cadence_calendar.json`;
  - a dense synthetic slate: Sunday NFL, Saturday NCAAF and EPL, Tuesday/Wednesday UCL;
  - the rotation always spending its full share.
- **Result:** spend never goes past 500. The rotation made 417 pulls and the plan 79, with 4 credits left.
  - Plan credits never dipped into the reserve.
  - On game days the plan ran out of credits (329 targets `skipped_budget`). The close got the most funding: 91 T−10m targets pulled against 54 T−6h.
  - On days with no plan targets, the plan's unused half flows back to the month, and the rotation spends it. So "entry share 0.5" means the rotation keeps at least half.
- **Unit slates:**
  - **Sunday NFL** (six 1 pm games, 21:05, 21:25): all 32 targets pulled, the 1 pm cluster for ≤4 credits, its close on the 17:47 slot.
  - **Short-credit day:** the close is funded, and the earlier T−6h/T−90m targets end as `skipped_budget`.
  - **Midweek UCL** (two 17:45 and six 20:00 games): the 20:00 cluster for ≤4 credits; the 17:45 close is `missed`.
- **Other tests cover:**
  - plan-mode pacer: one credit, carry untouched, reserve;
  - `pbot pull-due`;
  - kickoff source preference;
  - the workflow branch;
  - the sharp-move detector: lagged vs followed, small moves, book switches, the follow shadow.

### Config keys added
| Key | Default | Meaning |
|---|---|---|
| `ODDS_PULL_PLAN` | `hybrid` | `off` disables the plan and the rotation keeps every credit |
| `ODDS_ENTRY_SHARE` | `0.5` | Share of each run's credit the rotation keeps for 72h+ entry pulls |

Both are in `paper.yml` as repo Variables.

## Phase 4: queue-aware maker shadow

**Built against test data only.** This session cannot reach `api.elections.kalshi.com`. Field names for `/markets/trades` are taken from Kalshi's fixed-point naming and parsed defensively:
- `count_fp`, else `count`;
- `yes_price_dollars`, else `yes_price` in cents, else `no_price…`;
- `taker_side`, `created_time`.

**I'm not certain they match the live API.** `taker_side` is read as the side the taker bought, so a `no` taker trade hits resting YES bids. If either assumption is wrong, the queue fills will be wrong and the tests won't catch it. Rows that don't parse are dropped, not guessed.

### 4.1 Trades and depth snapshots
- **Client calls:**
  - `KalshiClient.get_trades(ticker, min_ts, max_ts)` pages `GET /markets/trades` and returns `Trade` rows, oldest first.
  - `get_orderbook_levels(ticker, depth=20)` returns every displayed bid level per side.
- **Snapshots** (`orderbooks.snapshot_orderbooks`, every paper run) go into the new `orderbook_snapshots` table (ticker, fetched_at, YES levels, NO levels as JSON).
  - **Targets, in order:** tickers with an open maker shadow, tickers we hold, then near-miss tickers from the last 24h whose game hasn't started.
  - A ticker snapshotted in the last 25 minutes is skipped.
  - Calls share the run's CLV budget: whatever `record_clv` leaves of `clv_max_calls_scanner` (300) on scanner runs, or 50 on sportsbook runs.

### 4.2 Queue model
- **Price rule.** The brief's point: with a 1c spread, bid+1c is the ask. A maker shadow now:
  - rests at bid+1c when the spread is ≥2c (`maker_price_rule='bid_plus_1'`);
  - rests at the bid when it is 1c (`at_bid`); before this, no shadow was placed at all on a 1c spread;
  - uses the v3 maker price under `maker_first` (`maker_first`).
- **Queue at placement.** `queue_ahead` is the displayed size at our price on our side:
  - 0 when the price improves the book or no size shows at that price;
  - from the latest depth snapshot no more than 30 minutes old (`queue_source='orderbook'`);
  - else from the top of book the strategy saw (`quote`);
  - else unknown (NULL).
- **Fill rule** (`maker_fill.queue_fill`):
  - Matching trades are those at or through our price that hit our side: `no`-taker trades at `yes_price ≤ p` for a YES bid; `yes`-taker trades at `1 − yes_price ≤ p` for a NO bid.
  - The shadow is filled at the first trade where matching volume since placement reaches `queue_ahead + size`.
  - The brief said "exceeds". **Reaches** (≥) is what makes its own example (behind 300, fills on 400) work for a 100-contract order.
- **Scoring.** `score_maker_shadows` now makes up to two calls per shadow: candles as before, then the trade tape.
  - It stores `fill_queue_at` and `queue_status` (`filled`, `unfilled`, `no_queue`, `unavailable`).
  - `fill_trade_through_at` and `fill_optimistic_at` are still filled in, for comparison.
- **Cancel rules** are unchanged (`v3_maker_cancel_move`, `v3_maker_ttl_minutes`).

### 4.3 Scoring the cohort
- Queue fills get `clv_kalshi`, `clv_kalshi_bid` and `clv_sharp_close` like every other row.
- `maker_shadow` now has a `clv_status`, set by the same rules as fills.
- Paper P&L uses the per-series maker fee stamped at placement (Phase 1; 0 on `quadratic` series).
- `maker_shadow_metrics` adds `queue_*` figures alongside `trade_through_*` and `optimistic_*`, and the dashboard's scoring table includes them.
- `pbot clv-report` has a `maker_queue` cohort.

### 4.4 The gate
- **What decides now.** `maker_gate.evaluate_maker_gate` passes when:
  - at least `v3_maker_gate_min_queue_fills` (50) v3 queue fills have `clv_status='ok'`;
  - the 95% CI lower bound of their `clv_kalshi` is above 0;
  - their mean is not below the taker cohort's.
  If there is no taker cohort, the queue fills' CI decides alone.
- **What was dropped.** The 50-taker-position requirement, and the old scored-shadow and trade-through-fill minimums.
  - Their figures are still computed and logged.
  - `v3_maker_gate_min_positions`, `_min_scored` and `_min_filled_clv` stay in config but no longer decide.
  - `auto` still resolves to taker until the gate passes.
- **Logging.** `execution_mode_log` gains `queue_n`, `queue_mean` and `queue_ci_low`; the old columns are still written. The CLI prints `queue_fills: n/50` or `queue_ci: low …`.
- **Dashboard.** The maker-first checklist now shows the three new checks.

### Acceptance (`tests/test_maker_queue.py`)
- **Fill rule:**
  - an order behind 300 contracts does not fill on 200 traded;
  - it fills on 400 traded, at the 400th contract;
  - an improving order (queue 0) fills on the first trade at its price;
  - trades above the price, trades lifting the other side, and trades before placement don't count; trades through the level do.
- **Parsing:** fixed-point and legacy trade rows, the paginated client call, depth parsing.
- **Queue at placement:** a fresh snapshot vs. a stale one with quote fallback; improving and empty levels.
- **Strategy:** at the bid on a 1c spread (queue 70), bid+1c on a 2c spread (queue 0).
- **Scoring:** queue fill beside the candle rules, `no_queue`, and `unavailable` when the tape fails.
- **Snapshots:** target priority, the call cap, the freshness skip.
- **Gate:** the tests are rewritten for the queue evidence.

## Phase 5: a second free reference price (Polymarket)

**Built against test data only.** This session's network policy blocks `gamma-api.polymarket.com` and `clob.polymarket.com`. I'm not certain the response shapes match the live APIs:
- **Gamma events:** `markets[]` with JSON-string `outcomes` and `clobTokenIds`, `gameStartTime`/`startDate`, `groupItemTitle`.
- **CLOB `/book`:** `bids`/`asks` with `price`.
- **Sport tag slugs:** `nfl`, `cfb`, `nba`, `cbb`, `nhl`, `epl`, `ucl`, `mls`, …

The tag map is a config key (`polymarket_tags`), so wrong slugs can be fixed without code. Unexpected fields are skipped, never guessed, and every failure is logged without stopping the cycle.

### 5.1 Client, matching and quotes
- **Client.** `venues/polymarket.py` is read-only and needs no key: Gamma `/events?tag_slug=…` (paged) for discovery, and CLOB `/book?token_id=…` for best bid/ask.
- **What gets quoted** (`reference_quotes.tracked_events`): Kalshi events with an open maker shadow, an open position, or a near miss in the last 24h, whose game hasn't started.
  - Outcomes come from the legs' tickers (team codes, `TIE` = draw).
  - Kickoff comes from a linked Odds API `commence_time`, else `expected_expiration` minus the pre-game offset.
- **Matching** (`match_tokens`). A Polymarket event matches when its start is within 3h of the Kalshi kickoff and both teams resolve, via a new `teams.team_for_reference_name`:
  - exact alias (optional `polymarket_names`, then Odds, Kalshi and ESPN names), else a nickname that exactly one team's Odds name ends with;
  - ambiguous names ("New York", "United") fail closed.
  - A two-outcome market naming both teams gives one token per team. Yes/No markets about a team or the draw give that outcome's YES token, which is how 3-way soccer is read.
- **Polling** (scanner runs only, in `paper`; `polymarket_enabled`, default on):
  - mids are written to the new `reference_quotes` table (event, `side` = outcome id, `source='polymarket'`, mid, bid, ask, token, fetched_at);
  - an event quoted in the last 25 minutes is skipped unless it kicks off within 40 minutes, so a near-close sample is always taken;
  - Gamma results are cached per sport per run, and `polymarket_max_requests` (200) caps Gamma plus CLOB calls per run.
- **Unmatched events.** They go into the new `reference_unmatched` table, at most once a day per event, with any Polymarket names no alias resolved.
  - `pbot reference-gaps` renders these as an `alias_gaps.md`-style Markdown list.
  - The workflow writes it to `polymarket_alias_gaps.md` on the `paper-ledger` branch each run.
  - Checked names can be added to `teams.json` as `polymarket_names`.

### 5.2 `clv_polymarket`
- **Definition.** The Polymarket mid for the side at the Kalshi close, minus entry, in cents.
  - The side is P(outcome) for YES, 1 − P(outcome) for NO, so 3-way soccer is handled.
  - It is stored on `fills`, `shadow_decisions` and `maker_shadow`, and scored each cycle by `clv.record_polymarket_clv`.
- **Deviation from the brief** ("mid at Kalshi T−5"): Polymarket is polled on 30-minute scanner runs, not at T−5, so the close is the latest sample in [kickoff − 35 min, kickoff]. The :17/:47 slots usually give a sample 13 minutes before :00/:30 kickoffs.
- **Report.** `pbot clv-report` gains `clv_polymarket` as a fourth metric in every cohort. The dashboard's CLV evidence table gains Polymarket row and mean columns.
- **Never traded.** Polymarket data is not used for any trading decision.

### Acceptance (`tests/test_polymarket.py`)
- **Parsing:** JSON-string lists, closed and mismatched markets skipped, start-time formats, best bid/ask.
- **Client:** paged Gamma calls and the request count.
- **Team names:** alias and nickname resolution, with ambiguous names failing closed.
- **Matching:**
  - a 2-way match needs both teams and a kickoff within 3h; unmapped names are reported;
  - a 3-way soccer match via team and draw Yes/No markets.
- **Polling:** quotes written, the request cap, unmatched logging.
- **`clv_polymarket`:** the close window (a sample 50 minutes out is ignored), the NO side, and the report metric.
- **CLI:** `pbot reference-gaps`.

## Phase 6: scheduler reliability

### 6.1 Cloudflare cron trigger
- **`infra/cloudflare-trigger/worker.js` plus `wrangler.toml`.** A Cloudflare Workers cron (`17,47 * * * *`, free plan) that calls GitHub's `workflow_dispatch` API for `paper.yml` with `inputs.slot`.
  - `sportsbook` for the :17 slot of even UTC hours (the same rule as the `17 */2 * * *` cron), else `scanner`. A late firing still counts as its own slot.
  - It authenticates with a fine-grained PAT stored as the Worker secret `GITHUB_TOKEN` (this repo only, Actions: read and write).
  - A non-204 answer throws, so the failure shows in the Worker's logs.
- **`paper.yml`.**
  - It takes an optional `slot` input. The cron schedule stays as a fallback; its minutes are already :17/:47, off :00.
  - The slot comes from the input, else from the cron that fired.
  - `SPORTSBOOK_SLOT` honours the input. A manual dispatch with no input still includes the sportsbook step, as before.
- **Double runs** (`pbot slot-guard --slot …`):
  - When a dispatch and the cron fallback both fire, the second run (queued behind the first by the `paper-ledger` concurrency group) finds a run of the same slot in the ledger within 20 minutes.
  - It then skips the paper cycle and the commit, so credits are never spent twice.
  - Move-trigger and pull-plan runs (`trigger_reason` set) don't count as slot runs. Manual runs skip the guard.
- **RESERVED:** Hadi creates the Cloudflare account and the PAT. The steps are in `infra/cloudflare-trigger/README.md`. Until then, nothing changes: the GitHub schedule runs as today.

### 6.2 Dead-man check
- **"Last run" is `runs.ran_at`.** Every paper cycle already writes one, as aware UTC, so no new column is needed.
- **`pbot health --max-age-minutes 90`:**
  - prints the last run time and how many of the 48 half-hour slots ran in the last 24h;
  - exits 1 when the last run is older than the limit, or when there are no runs at all.
- **`.github/workflows/health.yml`** runs hourly at :33, off the paper bot's minutes.
  - It reads the ledger from `paper-ledger` and runs the check (with `pipefail`, so `tee` can't hide a failure).
  - On failure it comments on the open issue titled "paper bot missed runs", or opens one, using the workflow's own token with `issues: write`.

### Acceptance (`tests/test_scheduler.py`, `infra/cloudflare-trigger/worker.test.mjs`)
- **Slot guard:** skips a slot that already ran, ignores plan and trigger runs, and rejects an unknown slot.
- **Health:** passes when fresh, exits 1 when stale or empty.
- **`paper.yml`:** has the `slot` input, the input-aware `SPORTSBOOK_SLOT`, the guard on both the cycle and the commit, and the fallback crons.
- **`health.yml`:** has its cron, pipefail and the issue logic.
- **`wrangler.toml`:** has the cron and carries no token.
- **Worker** (via `node --test`, which pytest runs when Node is installed): the slot rule including a late cron, the exact dispatch request, failure on non-204, the token kept out of the body, and plain HTTP refused.

## Phase 7: bug audit and sizing

### 7.1 Audit findings

| Item | Finding | Action |
|---|---|---|
| (a) `liquidity_dollars` removed (1 Oct 2026) | No code reads it. `parse_market` reads `yes_bid_size_fp`/`yes_ask_size_fp`. `thin_market`, `make_signal`'s depth gate and `feed_selection.liquidity_weights` use those sizes (via `Quote` or `market_snapshots.yes_ask_size`). `parse_orderbook` read only `orderbook_fp`/`orderbook` with `yes`/`no` keys. | **Fixed, defensively.** `_book_levels` also reads `yes_dollars`/`no_dollars` and converts legacy integer-cent prices. The live key names could not be checked from here. |
| (b) Event/milestone `status` for in-game detection | Not used. `_in_play_reasons` uses kickoff time, book updates after kickoff, and price or consensus extremes; nothing reads an event `status`. | None. |
| (c) 3-way soccer and exposure dedupe | v2/v3 key exposure by `position_key`. NO on A in a 2-way game is the other team's key; in a 3-way game it is `not:A`, which is not YES on B (NO also wins on a draw). The settlement-rule gate requires a Kalshi `TIE` leg plus an Odds draw for soccer. `position_cost`, `event_cost` and `held_keys` count every v2/v3 strategy, not one, and `opposite_position` stops a second outcome in the same event. `Engine._exposure` (v1 only) is per strategy by design; v1 now runs only in shadow, so v1's stacking can't recur. | Tests added: the 2-way merge and the 3-way NO leg kept separate. |
| (d) `_fresh_books` passed books with a missing or unparsable `last_update` | **Real.** A book absent from `book_updates`, or with an unreadable time, was treated as fresh. | **Fixed.** Unknown age now means stale, so the game can fall to `stale_books`/`few_books`. Test added. |
| (e) Aware UTC; `commence_time` never from ticker dates for NFL/NCAAF | `analysis.start_time` uses ticker times only for MLB (`KXMLBGAME…HHMM`), or a published kickoff list, so NFL/NCAAF kickoffs are never inferred from ticker dates. Strategy window maths parse with `_parse_time` (aware). `venues/kalshi._ts` returned a naive datetime for a value without an offset, which would raise when compared with aware times. | **Fixed:** `_ts` reads offset-less values as UTC. Test added. Not changed: v1's `_early_enough` anchors on Kalshi's expected *expiration* (about 3h after kickoff), so its 72h lead is really about 69h before kickoff; v3's window gate uses kickoff. |
| (f) `thin_market` detail prints `ask_depth=10.34` | Sizes come from the `_fp` fixed-point fields, which carry fractional contract counts. The comparison is in contracts (`ask_size_for(side) < v3_min_ask_depth`, 50 contracts), not dollars. The detail prints the raw Decimal. | None: the value is correct, just not rounded for display. |

### 7.2 Sizing
- **`PBOT_SIZING_MODE`** (workflow default `flat`, also a repo Variable):
  - **`flat`:** every v2/v3 entry stakes one unit, `max_order_notional` (25 dollars). The position, event, daily and cash caps still apply, and large-gap entries are still halved.
  - **`kelly`:** quarter-Kelly (`v2_kelly_fraction`) on an edge equal to the smaller of the model's edge and **half the cohort's mean `clv_kalshi`**. The cohort is this rules version's fills with `clv_status='ok'`. This applies only once the cohort is "supported" (n ≥ 50 and 95% CI lower bound > 0); until then sizing is flat.
- **What changes today.** Before this, sizing was quarter-Kelly on the model's own edge. The fill cohort has one clean-closing fill, so `kelly` behaves as `flat` for now.
- **Not changed:** `scripts/replay.py` still replays historical sizing with quarter-Kelly, as the v3 replay did.

### Acceptance (`tests/test_v5_audit.py`)
- the order-book parser on dollar keys and legacy cents;
- `_fresh_books` dropping books with missing or unreadable update times;
- aware `_ts`;
- the 2-way position-key merge and the 3-way NO leg kept separate;
- flat staking one unit;
- `kelly` staying flat while the cohort is unsupported, then capping the edge at half the supported cohort's CLV (1c of a 2c mean; 5 dollars at 0.50 on a 1,000-dollar bankroll).

## Phase 8: dashboard and docs

### 8.1 Dashboard: "Measurement health"
A new section below "Is it actually working?". Data comes from `dashboard._v5_health_data`, except where noted.

| Panel | What it shows | Source |
|---|---|---|
| Cards | Model-expected profit, realized profit, the gap (labelled as variance, in model SDs) and the share of paper bets beating the close | Expected is Σ((fair − price)·count − fee) over settled fills; realized is recomputed per fill (win `count·(1−price) − fee`, loss `−(count·price + fee)`), since `settlements.pnl` is per ticker. The model SD is √Σ count²·fair·(1−fair). |
| CLV by cohort | Each `pbot clv-report` cohort, all rows: n, mean vs Kalshi mid with 95% CI, share beating the close, verdicts for mid, bid and sharp close | `clv_report` (already in the payload since Phase 0); the per-group breakdown stays under Details |
| Practice resting orders | Fill rate and drift to the close (mid and bid) for the queue, trade-through and optimistic rules | `quality.maker_shadow_scoring`, which was computed but not drawn before |
| Data freshness | Newest row and its age for each odds feed, Kalshi markets, quotes, order books and closing prices, Polymarket and injuries; for odds feeds, the median and p90 lag between the newest book update and our pull over 7 days | `odds_snapshots.max_book_update`, plus each table's own timestamp |
| Runs per day | Distinct half-hour slots that ran per UTC day vs 48 scheduled (today counts only slots already due), plus triggered runs | `runs`; slot runs have no `trigger_reason`, the same rule `pbot health` uses |
| Rejections per day | Gate rejections per reason for each of the last 7 UTC days | `blocks` |

On the 5 Nov ledger, the cards read expected +$153.84 against realized +$560.02 over 87 settled fills, which matches the brief. The gap is +$406.18, about 1.4 model SDs. Runs per day shows 8–16 of 48 slots a day since 22 Oct, the gap the Phase 6 Worker is meant to close.

The page also gets one CSS rule, `main > * { min-width:0; }`. Without it, wide tables stretched every section past the screen on a phone: 1,004px wide on a 390px screen before this PR, and 1,133px with the new tables. The two-column block (recent results and account value) also gets a column minimum of `min(460px,100%)` instead of 460px. With both changes the page is exactly 390px wide on a 390px screen, and desktop still shows two columns.

### 8.2 README
- The "1c after fees" and "0.32 bets a day" text the brief names was never in the README:
  - Phase 2 corrected the "1c" claim in the workflow comment and `docs/PIPELINE.md`;
  - "0.32 bets a day" is only in `docs/CHANGES_v4.md`, as history.
- New sections:
  - **Current rules (v5):** reference books, entry thresholds, execution gate, sizing, credit split and Polymarket, with the workflow's defaults.
  - **How to read the evidence:** the n ≥ 50 and whole-CI-above-zero rule, "P&L is not evidence" with the current numbers, and links to `pbot clv-report`, the dashboard and `docs/evidence/`.
- Out-of-date lines corrected in place:
  - the strategies table now lists `sportsbook_v3` and marks the v1 strategies;
  - the fee line now describes per-series fees;
  - the sportsbook slot schedule;
  - the two Roadmap entries that now exist (maker fills, Polymarket);
  - "the run covers NFL/NCAAF/NBA/NHL" now says feeds are chosen per run by `feed_stats` eligibility and mapped games;
  - the Roadmap's WebSocket order-book item is removed, since v5 is public REST only.
- Everything else is unchanged.

### Acceptance (`tests/test_v5_dashboard.py`)
- per-fill P&L recompute, the open fill excluded, and the model SD;
- feed age, book lag, and a naive timestamp read as UTC;
- slot counting (duplicate runs in one slot count once; today counts only slots already due);
- rejections per reason per day within 7 days;
- every panel rendered;
- the README stating the rule, linking `docs/evidence/`, and no longer giving the old schedule, fee formula, fixed feed list or WebSocket plan.

## Decisions for Hadi
1. **Network access for later phases.** Allow `kalshi.com`, `api.elections.kalshi.com`, `gamma-api.polymarket.com` and `clob.polymarket.com` in the environment's network settings. Without them:
   - Phase 1.2's centicent rounding stays RESERVED;
   - Phases 4.1, 5 and 7.1a can be checked only against test fixtures. Phase 4's trade-tape field names and the `taker_side` direction in particular need one real `/markets/trades` response. Phase 5 needs one real Gamma `/events` and CLOB `/book` response, and the sport tag slugs need checking.
2. **Centicent fee rounding (RESERVED, still `cent`).** Once the July 2026 fee schedule PDF has been checked, add `PBOT_FEE_ROUNDING: centicent` to the paper step's `env` in `paper.yml` if it rounds fee plus cost to a centicent (the workflow does not read a repo Variable for it today). On the historical fills that is worth about 0.5c per order.
3. **Live entry window (RESERVED, unchanged at 72–168h).** Moving it to 1–48h is one repo Variable: `PBOT_ENTRY_WINDOW_MIN_HOURS`, plus `_MAX_HOURS`. The Phase 0 numbers (near misses, bid CLV) argue against moving it now:
   - 72h+: −1.01c (n=336, rejected);
   - 24–72h: −1.03c (n=383, rejected);
   - 2–24h: −0.96c (n=193, rejected).
   No window has CI > 0. NHL, NBA and NCAAB near misses from the 2.5 measurement pulls will add <24h rows.
4. **Price floor 0.25 (done as the brief asks; reversible).** On historical fills it would have removed all +$944.96 of realized profit. The near-miss evidence below 25c is −1.73c vs bid (rejected), so the floor stands unless you want the old 0.10 back. To restore it, set `PBOT_V2_PRICE_FLOOR: "0.10"` in `paper.yml`'s env; the workflow does not read a repo Variable for it.
5. **The Odds API $30/month tier (still undecided).**
   - On the free tier the plan's half is about 8 credits a day. The real Oct 20 – Nov 2 schedule needed ~42 a day for full target coverage. In the November simulation, grouping and ride-along pulls covered 297 of 650 targets, favouring the close.
   - Covering every target for the eligible feeds takes ~42 credits a day (~1,300 a month), more with NHL/NBA/NCAAB.
   - The $30 tier's 20,000 credits would cover that with room to spare, and would let `ODDS_ENTRY_SHARE` stay at 0.5 without starving either side.
6. **`ODDS_ENTRY_SHARE` (0.5).** Lower it to move credits from 72h+ entry pulls to near-kickoff measurement. That fits only if you expect the window decision (3) to move toward the close.
7. **Cloudflare account and PAT (Phase 6, RESERVED).** Create both and deploy the Worker by following `infra/cloudflare-trigger/README.md`, about 10 minutes. Until then the bot keeps running on the GitHub schedule alone, with the slot guard and health check already active.
8. **Sizing mode (`flat` by default, as the brief asks).** Switch the repo Variable `PBOT_SIZING_MODE` to `kelly` to size by evidence once a fill cohort is supported.
9. **Live orders**: out of scope.
