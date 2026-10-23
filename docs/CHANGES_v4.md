# v4 changes (2026-10-23)

v4 builds on v3 (`docs/CHANGES_v3.md`). It spends the free Odds API credits on games that could actually trade, scores every near miss against Kalshi's close, and adds the measurements needed to decide the remaining open choices with data. It is still paper-only: there is no live-order path, no credential changes, and every ledger change is additive.

**Status at the time of writing.** All v4 code is merged, but no scheduled sportsbook run has executed it yet. GitHub skipped or delayed the scheduled slots on Oct 23, and the last sportsbook run was 15:46 UTC, before v4 Phase 1 merged. The baseline numbers below are therefore the v3-era numbers. Every "after" figure fills in once runs resume. The tables come from `scripts/odds_budget.py` and the dashboard.

## What changed, by phase

| Phase | PR | What |
|---|---|---|
| 1: lead-aware feeds | [#46](https://github.com/hadihilaly-hue/-prediction-bot/pull/46) | `feed_stats` and `feed_pulls`, `pbot feed-plan`; only feeds with games beyond `min_lead_hours` get credits |
| 12 runs × ~1 feed | [#48](https://github.com/hadihilaly-hue/-prediction-bot/pull/48) | Replaces the fixed-slot parts of Phases 1 and 4. Details below the table |
| 3: CLV hygiene | [#47](https://github.com/hadihilaly-hue/-prediction-bot/pull/47) | `clv_status` (`ok`, `in_game`, `no_commence`, `pending`), commence-time backfill from Kalshi events; CLV aggregates use `ok` rows only |
| 2: near-miss CLV | [#49](https://github.com/hadihilaly-hue/-prediction-bot/pull/49) | Every candidate blocked by `net_edge` or `entry_window` is saved (one per position per day, at most 400 a run) and scored against Kalshi's T-5 bid (`clv_kalshi_bid`); edge-bucket and window panels; `fills.net_edge` |
| 4: priorities and move triggers | [#50](https://github.com/hadihilaly-hue/-prediction-bot/pull/50) | Rotation order: large-gap recheck, then a feed whose Kalshi ask moved ≥1¢, then value × staleness. Details below the table |
| 5: schedule awareness | [#51](https://github.com/hadihilaly-hue/-prediction-bot/pull/51) | Weekday prior: ×1.5 for NFL and NCAAF Monday–Wednesday UTC, and for soccer and UFC Thursday–Sunday. `feed_pulls.gate_candidates` counts, per pull, positions that passed every gate or failed only `net_edge` |
| 6: single-sharp cohort | [#52](https://github.com/hadihilaly-hue/-prediction-bot/pull/52) | Details below the table. Shadow only, with a separate dashboard panel |

**12 runs × ~1 feed ([#48](https://github.com/hadihilaly-hue/-prediction-bot/pull/48)).**
- The sportsbook step runs every 2 hours, with `PBOT_ODDS_RUNS_PER_DAY=12`.
- A fractional pacer is saved in `odds_pacer_state`, so most runs pull 1 feed and every third pulls 2.
- Feeds are picked by value × staleness.
- A pull is skipped, and its credit banked, when no eligible market moved and the feed was pulled under 6h ago.
- The free `/events` check runs before each pull, and `x-requests-last == 1` is asserted on every pull.

**Move-triggered pulls ([#50](https://github.com/hadihilaly-hue/-prediction-bot/pull/50)).**
- Scanner-only slots run the free `pbot odds-trigger`. On a move it starts `paper --trigger FEED`: 1 credit, at most 4 a day, v3 only.
- The credit is taken from the next scheduled run's share, so the day stays at about 16 credits.
- Optional measurement windows (1–2h, 5–7h, 22–26h) are off by default.

**Single-sharp cohort ([#52](https://github.com/hadihilaly-hue/-prediction-bot/pull/52)).**
- Applies to college football and soccer games with fewer than the required books, where Pinnacle or Circa quotes the game.
- That one book's fair price is used, with stricter gates: edge after fees ≥ 3¢, spread ≤ 3¢, ask depth ≥ `v3_min_ask_depth`.
- Passing sides are saved as `shadow_decisions` rows with `reason='single_sharp'`.

Phase 7 (OddsPapi as a second free closing-line source) was not built, because it needs an account Hadi has not created.

### Deviations from the brief
- **`odds_planner` stays `fixed`.** The brief made `event_driven` the default in Phase 4. That would bypass the 12-run rotation from #48, which Hadi said replaces the fixed-slot parts. The two Phase 4 priorities went into the rotation instead.
- **Single-sharp has no `trade` mode.** Only `off` and `shadow` exist. Trading at half size is decision 1 below.
- **Games with both Pinnacle and Circa still count as "single-sharp"** when they are under `min_books`. For example, college football needs 3 books. This matches the brief's wording: "fewer than the required books but Pinnacle or Circa quotes it".

## Config keys added (env prefix `PBOT_`)

| Key | Default | Meaning |
|---|---|---|
| `ODDS_FEED_SELECTION` | `rotation` | `rotation` (12×1), `lead_aware` (Phase 1 only), `legacy` |
| `ODDS_RUNS_PER_DAY` | `12` in the workflow | Pacer share per run = credits left this month / days left / runs per day |
| `MIN_LEAD_HOURS` | `72` | Entry window start; feeds with no lines beyond it score 0 |
| `ODDS_TRIGGER_DAILY_CAP` | `4` | Move-triggered pulls per UTC day, counted from the ledger |
| `ODDS_MEASUREMENT_WINDOWS` | `false` | Let 1–2h / 5–7h / 22–26h feeds use leftover slots |
| `ODDS_WEEKDAY_PRIOR` | `true` | Weekday ×1.5 ranking prior |
| `CLV_MAX_CALLS_SCANNER` | `300` | Kalshi candle calls for CLV on scanner-only runs (sportsbook runs keep 50) |
| `V3_SINGLE_SHARP_COHORT` | `shadow` | `off` or `shadow` |

Ledger additions:
- tables `feed_stats`, `feed_pulls`, `odds_pacer_state`, `odds_triggers` and `clv_commence_attempts`;
- columns `clv_status`, `commence_time_backfill` and `commence_source` on `fills` and `shadow_decisions`;
- `shadow_decisions.net_edge`, `clv_kalshi_bid` and `tags`;
- `fills.net_edge`, `feed_pulls.requests_last` and `feed_pulls.gate_candidates`, and `runs.trigger_reason`.

`shadow_decisions` now holds three kinds of rows, so readers must filter by both `would_trade` and `reason`:
- would-trade shadows (`would_trade=1`);
- near misses (`would_trade=0`, `reason` `net_edge` or `entry_window`);
- the single-sharp cohort (`would_trade=1`, `reason='single_sharp'`).

## Evidence rule

A window, edge bucket, threshold or cohort is judged on `clv_kalshi_bid`, the T-5 Kalshi bid minus the entry price, counting only `clv_status='ok'` rows:
- **supported** when n ≥ 50 and the 95% CI lower bound is above 0;
- **rejected** when n ≥ 50 and the 95% CI upper bound is below 0;
- **open** otherwise.

The dashboard shows "Not enough yet" below 50. Maker-first's automatic gate from v3 is unchanged.

## Baseline (2026-10-23, before any v4 run)

| Measure | Baseline | Where the "after" number will come from |
|---|---|---|
| Credits per sportsbook run | 5 (5 feeds), ~15–16 a day at 3 runs | `scripts/odds_budget.py` (credits by feed) |
| Pulls on feeds that could trade | 2 of 5 | `scripts/odds_budget.py` useful-pull ratio |
| Soccer feeds pulled | 0 of 5 tight-market leagues | `feed_pulls` |
| Near-miss rows with CLV | 0 | dashboard near-miss panels (by edge bucket and window) |
| v3 fills with valid closing CLV | 0 | dashboard evidence panel |
| Rows left out of CLV by Phase 3 | 15 `in_game`, 71 `no_commence` (all v1-era fills) | dashboard "left out" notes |

The brief's one-week items cannot be reported yet, because no v4 run has happened. These are:
- per-feed credits and useful-pull ratio after Phase 1;
- scored near-miss counts by window and edge bucket;
- the CLV-by-edge and CLV-by-window tables with CIs.

## Time to 50 observations, re-estimated

- **Real v3 fills.** The v3 replay found about 0.32 bets a day at 3 runs a day. With 12 runs and edges that last about 5h, the earlier estimate was about 16 bets a month, so 50 fills take about 3 months. Nothing in v4 changes the fill rules, so that estimate stands.
- **Near misses.** Near misses don't depend on fills. Every priced game that misses on edge or window is saved, at most once a day per position, so the edge-bucket and window cohorts should pass 50 scored rows in weeks rather than months. The exact rate depends on how many priced games each pull returns, so it will be re-estimated after a week of `gate_candidates` data.
- **Single-sharp cohort.** The one rules run logged on Oct 23 (v2) produced 14 `few_books` blocks, each with a single book, across NHL and college football. Only the college football and soccer ones qualify, and only when that book is Pinnacle or Circa. Expect months, not weeks.

## Known gaps
- GitHub's scheduler dropped or delayed many slots on Oct 23. The pacer's carry is capped at 1 credit, so a long gap loses credit rather than spending it all at once. If this continues, a cron offset or a self-hosted trigger may be worth a look. Changing that needs Hadi's OK, because it changes the cadence.
- The `_fresh_books` line-age check passes a book whose update time is missing or unreadable.
- `useful_pulls_14d` (Phase 1) is based on the raw `feed_pulls.candidates`. `gate_candidates` (Phase 5) is the stricter count.

## Decisions for you
1. **Single-sharp cohort: keep it in shadow (recommended), or trade at half size.** Trading needs a small code change (a `trade` mode). It should wait until the cohort itself is "supported" under the rule above.
2. **OddsPapi (second free closing-line source): skip for now (recommended), or open the free account.** It would give a sportsbook close to compare with Kalshi's. The 250 requests a month and the Pinnacle coverage are vendor claims and unverified. If you want it, I'll send click-by-click steps.
3. **`min_lead_hours`: keep 72 until the window panel has CIs (recommended), or shorten it now.** The window panel's near-miss rows (72h+, 24–72h, 2–24h) will show which window beats the close. Changing it before then would be a guess.
