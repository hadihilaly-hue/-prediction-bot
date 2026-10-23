# v3 changes (2026-10-23)

v3 is the next step after v2 (`docs/CHANGES_v2.md`). It aims to stop the bot treating data errors and noise as edge, to make entries cheaper where it is safe, and to measure whether any edge exists before anything scales. It is still paper-only: there is no live-order path, no credential changes, and every ledger change is additive (new columns, tables or rules versions; no historical row is edited).

**How to switch.** Scheduled sportsbook runs trade `sportsbook_v3`, with `sportsbook_v2` and the v1 strategies in shadow on the same odds data, so the shadows cost no extra credits. Set the repo Variable `PBOT_RULES_VERSION` to `v2` or `v1` to go back. Crons and the series list are unchanged.

## What changed, by phase

| Phase | PR | What |
|---|---|---|
| A: estimate and gates | [#38](https://github.com/hadihilaly-hue/-prediction-bot/pull/38) | Power devig as the single fair-price method, sharp-book reference and dispersion, large-gap re-verification, depth floor, stale-line rejection, data-quality stop, same-day no-reentry |
| B: maker execution | [#39](https://github.com/hadihilaly-hue/-prediction-bot/pull/39) | Candle-scored maker shadows (`trade_through` and `optimistic`), simulated `paper_orders`, `maker_first` mode (**off**), `clv_exit` |
| C: price paths and odds budget | [#40](https://github.com/hadihilaly-hue/-prediction-bot/pull/40) | `market_snapshots` (every ~30 min, change-only plus a 6h heartbeat), entry-window CLV, event-driven odds planner (**off**), `scripts/odds_budget.py` |
| D: aliases | [#41](https://github.com/hadihilaly-hue/-prediction-bot/pull/41) | Fail-closed NCAAF, Europa and Conference League aliases; `docs/alias_gaps.md` |
| E: settlement rules | [#43](https://github.com/hadihilaly-hue/-prediction-bot/pull/43) | `data/settlement_rules.json` and a v3 gate; `docs/SETTLEMENT_RULES.md` |
| F: dashboard evidence | [#42](https://github.com/hadihilaly-hue/-prediction-bot/pull/42) | "How much evidence is there?" panel: expected return with 95% ranges, progress to 50 bets, verdicts per entry window |

### v3 pipeline additions (on top of the v2 order)
1. `settlement_rule_unknown` / `settlement_rule_mismatch`: the series must have a rule, every leg's `rules_primary` must contain its phrases, and soccer needs a Kalshi TIE leg plus an Odds Draw probability.
2. `stale_books`: the reference book's line is older than `v3_max_book_age_hours`.
3. `dispersion`: measured across the sharp books (Pinnacle, Circa, BetOnline). If only a soft reference (DraftKings/FanDuel) is available, the spread across all books is used instead.
4. `thin_market`: ask depth on the side being bought is below `v3_min_ask_depth`.
5. Large gaps (7–12¢):
   - Passes only if the same position was flagged 2–36h earlier, at least 2 sharp books agree within 2¢, and a fresh Kalshi orderbook still shows the gap.
   - These entries are **half-sized**.
   - Gaps above 12¢ stay blocked.
6. `already_held`: no re-entry into an open position, or one filled or settled the same ET day.
7. `dq_stop`: if at least 10 events were evaluated and more than half failed a data-quality check (`title_mismatch`, `mapping_sum`, `mapping_mismatch`, `dispersion`, `settlement_rule_mismatch`), the run places nothing.

## Config keys (env prefix `PBOT_`)

| Key | Default | Why |
|---|---|---|
| `rules_version` | `v1` in code; workflow `v3` | v1 and v2 stay selectable and run in shadow |
| `v3_devig_method` | `power` | Replaces "most cautious of four plus a haircut", which double-counted the longshot bias. `shin` and `worst_case` are available. All four methods are still logged |
| `v3_longshot_haircut` | `off` | Power devig already corrects for the longshot bias |
| `v3_allow_soft_reference` | `true` | Allows DraftKings/FanDuel as the reference when no sharp book quotes, with the stricter all-book dispersion check |
| `v3_large_gap_mode` / `v3_large_gap_max` | `reverify` / 0.12 | `block` restores v2's hard 7¢ cap |
| `v3_min_open_interest` | 0 | The brief's 100 would have blocked most markets days before the game (NCAAF median OI ≈ 5, NHL ≈ 33) even when the order book was deep |
| `v3_min_ask_depth` | 50 | Contracts offered on the side being bought |
| `v3_max_book_age_hours` | 3 | Stale-line rejection |
| `v3_dq_stop_fraction` | 0.5 | Data-quality stop |
| `v3_no_reentry` | true | No same-day re-buys |
| `v3_execution` | **`taker`** | `maker_first` is built but off; see the decisions below |
| `v3_maker_cancel_move` / `v3_maker_ttl_minutes` | 0.01 / 480 | Cancel a resting paper order when the price moves 1¢ against it or the edge disappears; expire after 8h |
| `odds_planner` | **`fixed`** | `event_driven` is built but off |

## Replay and measurement (copies of the ledger only)

- **v3 replay** (`scripts/replay.py --rules v3`, 86 historical fills):
  - 82 fills blocked, 77 of them for real rules rather than a missing alias.
  - 8 would-trade positions, **+$26.01** settled, against v2's 6 at −$0.46.
  - Depth, open interest and line age weren't stored historically, so the replay can't apply those gates.
  - This is far too small a sample to mean anything.
- **Maker replay** (`--maker`): of 34 eligible candidates, 2 orders were placed (the same CIN–BAL contract under v2 and v3), and both filled under both fill rules.
  - 22 candidates would have crossed the spread.
  - 10 had no usable quote.
  - 24 used candle closes in place of orderbooks, which weren't stored.
  - There is no evidence yet on adverse selection.
- **Settlement-rule live check (Oct 23):**
  - 0 mismatches across 1,115 open NFL, NCAAF (first 200), NBA, NCAAMB, NHL, MLB and soccer markets, and every soccer event has a TIE leg.
  - UFC's phrase was corrected to match the live text.
  - The golf entries are provisional, since no golf markets were open.
- **Alias coverage:** in all 26 current NCAAF Odds API games that pair uniquely with a Kalshi event, both teams map. 155 NCAAF codes remain unmapped; they are mostly FCS teams with no Odds line, listed in `docs/alias_gaps.md`. The Kalshi codes `LEV` and `CEL` are each reused for two different clubs, so they stay unmapped. NCAAB has 4 Kalshi codes so far, and none map yet (season not started).

## Odds API credits

`runs.odds_used` is the cumulative monthly counter, so the cost per run comes from differences between runs. The observed cost is **5 credits per sportsbook run**. That figure comes from only two runs, so the table below is rough. Re-run `python scripts/odds_budget.py` as runs accumulate.

| Sportsbook runs/day | `fixed` | `event_driven` |
|---:|---:|---:|
| 3 (current) | ~457/month | ~244/month |
| 6 | ~913 (needs the $30 tier) | ~457 (fits the free 500) |
| 12 | ~1,826 (needs the $30 tier) | ~944 (needs the $30 tier) |

Phases D–E used 4 credits. October is at 360 of 500.

## Evidence threshold before scaling or enabling anything

The equity curve is not evidence. A result counts only when, for the rules version being judged:
- **at least 50 positions** have a closing price, and
- the **95% CI lower bound of mean CLV is above 0**.

The same rule enables an entry window (72h+, 24–72h, 2–24h). Turning on `maker_first` additionally requires the `trade_through` maker shadow to show at least 50 scored orders, with filled orders' CLV not worse than the taker's. The dashboard panel "How much evidence is there?" shows progress against these thresholds.

## Known gaps
- No historical orderbook, depth or line-age data exists before Oct 23, so the replays can't test the v3 liquidity or freshness gates.
- `market_snapshots` began in Phase C, so the price-path data starts at the end of October.
- UFC and golf are rule-tabled but still not traded (no exact alias tables).
- NCAAB aliases will need a refresh once Kalshi lists the season's games.

## Decisions for you
1. **Odds API tier:** stay free at 3 runs/day (the default), or pay ~$30/month for 20,000 credits so runs can go to 6–12/day.
2. **Cadence:** switch `odds_planner=event_driven`. That would allow ~6 runs/day on the free tier, but the estimate rests on only two observed runs.
3. **Maker-first:** keep it off until the evidence threshold above is met.
