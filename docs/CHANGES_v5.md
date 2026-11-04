# v5 changes (2026-11-03)

v5 builds on v4 (`docs/CHANGES_v4.md`) and the credit cadence work (`docs/CHANGES_cadence.md`, which also calls itself "v5"; this file covers the v5 brief's phases). At the taker ask, after the 0.07·P·(1−P) fee, Kalshi game markets track the devigged sharp consensus to within the fee in every window the bot has sampled. So the bot "barely trades" because its rules are working, and the realized P&L is variance on an edge that is roughly zero or slightly negative. v5 sets out to:
- measure against the right benchmarks;
- spend odds credits where repricing happens;
- simulate maker execution properly;
- stop buying the price bands that are reliably negative.

It is still paper-only: no live-order path, no credential changes, and every ledger change is additive.

**Status.** Phases 0 ([#60](https://github.com/hadihilaly-hue/-prediction-bot/pull/60)), 1 ([#61](https://github.com/hadihilaly-hue/-prediction-bot/pull/61)) and 2 are open as stacked PRs. Later phases follow one PR each.

## What changed, by phase

| Phase | PR | What |
|---|---|---|
| 0: measurement | [#60](https://github.com/hadihilaly-hue/-prediction-bot/pull/60) | `clv_sharp_close` / `clv_sharp_source`, per-book odds and the Kalshi event ticker on `odds_snapshots`, `pbot clv-report` (text and `--json`), dashboard panel, `docs/evidence/2026-11-03.json` |
| 1: fee model per series | [#61](https://github.com/hadihilaly-hue/-prediction-bot/pull/61), stacked on #60 | One fee helper `fees.fee_for_ticker`; the v1 path now uses each series' fee type and multiplier; `fee_model_version='kalshi-series-v2'` |
| 2: entry rules | stacked on #61 | 25c price floor and a doubled threshold below 40c; soft reference off in the workflow; entry window as config (72–168h); shadow-only NHL/NBA/NCAAB measurement pulls (≤2 credits/day); price-floor and thin-market rows kept as near misses with depth and open interest |

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

## Decisions for Hadi
1. **Network access for later phases.** Allow `kalshi.com`, `api.elections.kalshi.com`, `gamma-api.polymarket.com` and `clob.polymarket.com` in the environment's network settings. Without them:
   - Phase 1.2's centicent rounding stays RESERVED;
   - Phases 4.1, 5 and 7.1a can be checked only against test fixtures.
2. **Centicent fee rounding (RESERVED, still `cent`).** Once the July 2026 fee schedule PDF has been checked, add `PBOT_FEE_ROUNDING: centicent` to the paper step's `env` in `paper.yml` if it rounds fee plus cost to a centicent (the workflow does not read a repo Variable for it today). On the historical fills that is worth about 0.5c per order.
3. **Live entry window (RESERVED, unchanged at 72–168h).** Moving it to 1–48h is one repo Variable: `PBOT_ENTRY_WINDOW_MIN_HOURS`, plus `_MAX_HOURS`. The Phase 0 numbers (near misses, bid CLV) argue against moving it now:
   - 72h+: −1.01c (n=336, rejected);
   - 24–72h: −1.03c (n=383, rejected);
   - 2–24h: −0.96c (n=193, rejected).
   No window has CI > 0. NHL, NBA and NCAAB near misses from the 2.5 measurement pulls will add <24h rows.
4. **Price floor 0.25 (done as the brief asks; reversible).** On historical fills it would have removed all +$944.96 of realized profit. The near-miss evidence below 25c is −1.73c vs bid (rejected), so the floor stands unless you want the old 0.10 back. To restore it, set `PBOT_V2_PRICE_FLOOR: "0.10"` in `paper.yml`'s env; the workflow does not read a repo Variable for it.
5. **The Odds API $30/month tier**: still undecided (Phase 3).
6. **Cloudflare account and PAT**: still undecided (Phase 6).
7. **Live orders**: out of scope.
