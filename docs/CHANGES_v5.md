# v5 changes (2026-11-03)

v5 builds on v4 (`docs/CHANGES_v4.md`) and the credit cadence work (`docs/CHANGES_cadence.md`, which also calls itself "v5"; this file covers the v5 brief's phases). At the taker ask, after the 0.07·P·(1−P) fee, Kalshi game markets track the devigged sharp consensus to within the fee in every window the bot has sampled. So the bot "barely trades" because its rules are working, and the realized P&L is variance on an edge that is roughly zero or slightly negative. v5 sets out to:
- measure against the right benchmarks;
- spend odds credits where repricing happens;
- simulate maker execution properly;
- stop buying the price bands that are reliably negative.

It is still paper-only: no live-order path, no credential changes, and every ledger change is additive.

**Status.** Phase 0 is in this PR. Later phases follow one PR each, after Phase 0 is reviewed.

## What changed, by phase

| Phase | PR | What |
|---|---|---|
| 0: measurement | this PR | `clv_sharp_close` / `clv_sharp_source`, per-book odds and the Kalshi event ticker on `odds_snapshots`, `pbot clv-report` (text and `--json`), dashboard panel, `docs/evidence/2026-11-03.json` |

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

## Decisions for Hadi
1. **Network access for later phases.** Allow `kalshi.com`, `api.elections.kalshi.com`, `gamma-api.polymarket.com` and `clob.polymarket.com` in the environment's network settings. Without them:
   - Phase 1.2's centicent rounding stays RESERVED;
   - Phases 4.1, 5 and 7.1a can be checked only against test fixtures.
2. **Live entry window (RESERVED, unchanged at 72h+).** Phase 0's numbers are above. No window has CI > 0; every window is "rejected" vs the bid and vs the mid.
3. **The Odds API $30/month tier**: still undecided (Phase 3).
4. **Cloudflare account and PAT**: still undecided (Phase 6).
5. **Live orders**: out of scope.
