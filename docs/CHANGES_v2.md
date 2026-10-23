# v2 changes (2026-10-23)

This turns the paper bot from a three-week result that was hard to trust into a rule set whose decisions, fees and measurements can be checked. It is still paper-only: there is no new live-order path and no credential changes. Historical ledger rows were never edited; every schema change is additive.

**How to switch.** Each scheduled sportsbook run trades `sportsbook_v2` and runs v1 (`sportsbook_arb` + `underdog_value`) in shadow on the same markets and odds data, recording what v1 would have bought. Set the repo Variable `PBOT_RULES_VERSION=v1` (Settings → Secrets and variables → Actions → Variables) to restore the exact v1 command. The crons, series list and Odds budget are unchanged.

## What changed, by phase

| Phase | PR | Main files |
|---|---|---|
| 0: map and measure | [#30](https://github.com/hadihilaly-hue/-prediction-bot/pull/30) | `docs/PIPELINE.md`, `scripts/audit_ledger.py`, `docs/AUDIT_2026-10-23.md` |
| 1: correctness | [#31](https://github.com/hadihilaly-hue/-prediction-bot/pull/31) | `strategies/sportsbook_v2.py`, `teams.py`, `data/teams.json`, `scripts/build_team_aliases.py`, `paper/ledger.py`, `engine.py` |
| 2: fees, devig, reference, sizing | [#32](https://github.com/hadihilaly-hue/-prediction-bot/pull/32) | `fees.py`, `devig.py`, `strategies/sportsbook_v2.py`, `engine.py`, `paper/ledger.py` |
| 3: measurement | [#33](https://github.com/hadihilaly-hue/-prediction-bot/pull/33) | `clv.py`, `analysis.py`, `dashboard.py`, `engine.py`, `.github/workflows/paper.yml` |
| 4: replay and experiments | [#34](https://github.com/hadihilaly-hue/-prediction-bot/pull/34) | `scripts/replay.py`, `strategies/sportsbook_v2.py`, `config.py`, `dashboard.py` |
| 5: this report | this PR | `docs/CHANGES_v2.md` |

(#29 moved #28, the maker entries and depth gate, onto `main`; it had been merged into #27's branch by mistake.)

### Order of the v2 decision pipeline (per Kalshi event, per run)
Each rejection is written to the `blocks` table with its reason code:
1. `unsupported_series` (UFC and golf: no exact alias table), `league_disabled` (NBA)
2. mapping: `mapping_failed`, `title_mismatch`, `no_odds_match`, `ambiguous_match`, `mapping_sum`
3. timing: `kickoff_guard`, `start_mismatch`, `in_play:{commence_past, book_updated_after_start, consensus_extreme, kalshi_extreme, consensus_jump}`, `entry_window` (the entry window is shadow-logged, not dropped)
4. fees: `fee_unknown`, `unknown_fee_type`
5. reference line: `few_books`, `dispersion`
6. thresholds: `price_floor`, `price_ceiling`, `net_edge`, `longshot_edge`, `big_edge` (with an `edge_reviews` row)
7. one decision per position key: `duplicate_position`, `wide_spread` (sent to the maker shadow book only)
8. sizing and caps: `depth_limited` (logged, not a block), `size_zero`, `cap_position`, `cap_event`, `cap_day`, `cap_total`, `opposite_position`

## Config keys (env prefix `PBOT_`)

| Key | Default | Why |
|---|---|---|
| `rules_version` | `v1` in code; workflow default `v2` | v1 stays runnable for at least four weeks of side-by-side comparison |
| `shadow_strategies` | workflow: `sportsbook_arb,underdog_value` | shadow comparison with v1, at no extra Odds credits |
| `kickoff_buffer_minutes` | NFL/CFB/NBA 30 | brief; 15 confirmed in-game fills (+$158) |
| `kickoff_buffer_default_minutes` | 15 | brief |
| `min_lead_hours` (shared with v1) | 72 | unchanged; v2 shadow-logs the 24–72h, 2–24h and <2h windows so the rule gets measured |
| `in_play_consensus_high` / `_low` | 0.93 / 0.07 | brief; the Edmonton row had consensus 0.956 |
| `in_play_first_consensus_min` / `_max` | 0.25 / 0.75 | brief |
| `in_play_kalshi_price_min` / `_max` | 0.03 / 0.97 | brief; Edmonton NO was bought at 1¢ |
| `in_play_consensus_jump` | 0.15 | brief |
| `max_gross_edge` | **0.07** | brief said 10¢, but two of its three must-reject cases are below 10¢ (UCLA–Oregon 8.2¢, SF–SEA 7.5¢). Edges of 5¢ or more went 4 for 14 and lost $135 |
| `big_edge_whitelist` | empty | an Odds re-fetch costs 1 credit, so it only happens for tickers you list |
| `fee_rounding` | `cent` | Kalshi rounds fees up to the cent; `centicent` is available |
| `v2_min_net_edge` | 0.02 | brief; the 1¢-after-fee edges (CARPHI, CINBAL) are noise |
| `v2_longshot_price` / `v2_longshot_min_gross` | 0.25 / 0.04 | brief; <10¢ went 0 for 8 (−$74) |
| `v2_price_floor` / `_ceiling` | 0.10 / 0.90 | brief; the 5–9¢ CFB longshots went 0 for 17 fills |
| `v2_max_spread` | 0.04 | brief; wider spreads go to the maker shadow book only |
| `min_books` / `min_books_by_sport` | 2 / CFB 3 | brief |
| `max_book_spread` | 0.03 | brief; the reference is too noisy beyond this |
| `longshot_haircut` | `slope` | Kalshi calibration slope 1.04 (2d–1w) and 1.24 (>1w), Le 2026; `flat` and `none` also available |
| `v2_kelly_fraction` | 0.25 | quarter Kelly, because q is an estimate |
| `v2_position_bankroll_fraction` / `v2_event_bankroll_fraction` | 0.02 / 0.02 | brief; the three winning positions were 2–4× normal size because of stacking |
| `v2_daily_bankroll_fraction` / `v2_total_open_bankroll_fraction` | 0.10 / 0.30 | brief |
| `v2_disabled_sports` | `basketball_nba` | NBA injury news lands on game day. Enabling it needs a game-day run after the 11 AM–1 PM injury report (or an injury feed) plus a same-day kickoff guard |
| `exp_line_move_points` | 0.03 | line-move follower, shadow only; no trading before at least 50 observations |
| `exp_road_dog_enabled` | true | narrowed underdog test at small paper size: away team, 15–45¢, 3–7 days out, net edge ≥ 0, high-attention game |
| `exp_road_dog_position_bankroll_fraction` | 0.01 | brief: 1% per-position cap; quarter Kelly |
| `exp_road_dog_high_attention_events` / `exp_road_dog_rivalries` | empty | manual lists; prime time (19:00–23:59 ET) and Thursday/Monday NFL count automatically. Rankings and neutral sites aren't in our data |

The fee schedule is no longer configurable for v2: it comes from Kalshi `/series` (`fee_type`, `fee_multiplier`, cached daily in `series_fees`). MLB has multiplier 0.5; MLS and UFC have no maker fee. v1 keeps its flat 0.07 / 0.0175 fees, and its fills are stamped `fee_model_version=v1-flat`.

## Reference line and maker fill rule chosen
- **Reference line: option C.** Use Pinnacle when it quotes the game, otherwise the median of the configured books. At least 2 books are required (3 for CFB), and a game is skipped when the books' devigged probabilities spread more than 3¢. Each book's probabilities, `reference_source` and `book_count` are stored on every signal. No new books, regions or markets were added. Pinnacle quoted 13 of 27 NFL games in the sample; Circa quoted none.
- **Devig:** multiplicative, additive, power and Shin for 2- and 3-way markets. The fair used is the lowest of the four for the side bought. Soccer NO = 1 − fair(team) from the three-way devig.
- **Maker shadow fill rule: conservative.** A resting order counts as filled only when a later run shows the ask at or below the resting price. Trade-throughs (`last_price` at or below the resting price) are recorded separately so the two rules can be compared. Shadows are checked only on the three sportsbook runs per day, so the fill rule is evaluated at those times only.

## Test results
- `pytest -q`: 184 passed. `ruff check`, `ruff format --check` and `mypy` are clean. CI passed on Python 3.10 and 3.12 for every PR.
- Appendix A fee table: `tests/test_fees.py`. Appendix B devig vectors and method ordering: `tests/test_devig.py`.
- Appendix D ledger regressions: `tests/test_v2_regressions.py`.
  - kickoff-guard cases: SDMIL, KCLV, DENSF, MIAMIN, LADATL
  - in-play detector: EDMSJ, AUBTENN
  - LAFC/Galaxy mapping
  - big edge: MIAMIN, UCLAORE, SFSEA at 53¢ and 54¢
  - KC/LV four-fill collapse
  - the 5–9¢ CFB price-floor cases (UTEPUNM is blocked as `mapping_failed` because UTEP has no alias)
  - the 1¢ net-edge cases (CARPHI, CINBAL; TOLBALL is caught by the price floor first)
- The dashboard is checked in headless Chrome (zero console errors). A tokenizer test stops JS string literals from breaking across lines.
- Smoke run: `pbot paper -s sportsbook_v2` with v1 in shadow, on a copy of the real ledger against the live public Kalshi API. The ledger migrated in place, CLV was recorded for 15 fills, and nothing crashed.

## Replay results (scripts/replay.py --rules v2 --since 2026-09-28)
Run on a copy of the ledger. All 131 stored signals and 86 fills were replayed.

- **v2 would have blocked 82 of the 86 fills.** 77 of them are blocked by a rule other than missing aliases.
- First blocking reason:

| reason | fills |
|---|---|
| `net_edge` | 29 |
| `price_floor` | 16 |
| `in_play:commence_past` | 15 |
| `mapping_failed` (alias missing) | 10 |
| `big_edge` | 4 |
| `duplicate_position` | 4 |
| `longshot_edge` | 2 |
| `mapping_mismatch` (LAFC→Galaxy) | 2 |

- All reasons that apply (a fill can have several): net_edge 51, mapping_failed 10, longshot_edge 46, price_floor 17, commence_past 15, big_edge 8, duplicate_position 4, mapping_mismatch 2.
- **v2 would have taken 6 positions, with settled P&L −$0.46** (v1: +$517.16). The three outcomes behind v1's profit were all blocked: the Brewers fills by the kickoff guard (in-game), and Oklahoma (23.5¢ vs fair 0.27) and South Carolina (13.7¢ vs 0.163) by the longshot and net-edge rules.

| position | contracts | P&L | game-day CLV |
|---|---|---|---|
| BGSU over M-OH (YES) | 39 | +$21.56 | −25.5¢ |
| CHI vs GB (CHI NO) | 23 | +$10.86 | −3.5¢ |
| UNC at PITT (YES) | 27 | +$17.94 | unknown |
| VAN vs TB (TB NO) | 38 | −$11.57 | unknown |
| WPG vs CAR (YES) | 47 | −$20.55 | −7.5¢ |
| HOU at JAC (HOU NO) | 40 | −$18.70 | −6.5¢ |

T-5 CLV is unknown for all six, because their kickoff times can't be derived from stored data.

- This is an upper bound. Dispersion, book count, spread, depth and worst-case devig can't be recomputed from stored signals, and all of them can only block more.
- Takeaway: on this history v2 is roughly flat, as it should be when the bot has no measurable edge. The +$517 came from fills v2 treats as errors or noise.

## CLV
Kalshi's public 1-minute candlesticks cover past games for free. So CLV at T-60, T-15 and T-5 is computed after each game from candles, and the 10–15-minute snapshot cron from the brief is not needed (no cadence change). A separate `clv_gameday` proxy (10:00 ET on the game date) is computed for every fill and always labelled as a proxy.

Appendix D checks against real candles:
- Oklahoma (TEXOKLA) was 25–26¢ all morning before the Oct 10 kickoff. The brief's "~38¢ on game day" was an in-game price, so the real CLV was about +2¢ against the 23.5¢ entry.
- SEA was 61–62¢ before kickoff against a 53–54¢ entry, about +7.5¢. The brief's ~66¢ was slightly high.
- South Carolina was 16.5¢ on Oct 5 and 19.5¢ on game morning against 13.5¢, so +3¢ and +6¢.

## API credit use
- Before this work: 341 / 500 used this month (2026-10-23 07:00 UTC).
- Spent during the work: 1 credit on the NFL sample, 4 on `/participants` (NHL, NBA, MLB, WNBA aliases), and 1 on an unexpected historical `/events` call. `/events` for upcoming games is free. Total after: 346 / 500 (154 remaining) at 2026-10-23 ~08:00 UTC.
- Ongoing: v2 and the shadowed v1 share one Odds client with a 60-second memo, and `OddsPacer` still caps requests per run, so monthly use stays within the existing budget. Big-edge re-fetches cost 1 credit each but only run for whitelisted tickers (none by default). Kalshi reads (quotes, candles, series fees) cost nothing.

## Dashboard
Positions are now the primary unit. Tabs: v2 and v1 historical. Panels:
- positions table, with strategy tags instead of a per-strategy P&L split
- CLV by league, entry window and price band
- 10¢ calibration
- Brier on fills and on positions, with game-bootstrap CI and a "too few to be evidence" note under n=50
- Monte Carlo luck test
- P&L with and without the top 3 outcomes
- data quality: blocks by reason, depth-limited sizes, edge reviews, Odds credits
- maker shadow book
- shadow decisions by window and rules version

On the historical ledger it shows realized P&L $517.16, −$376.00 without the top three, Brier on fills 0.2227 (bot) vs 0.2235 (market), and P(≥ realized) of 8.7% at market prices and 18.8% at bot fair values.

## Known gaps
- Alias coverage: NFL 32/32, NHL 32/32, MLB 30/30, WNBA 15/15, NBA 30/31, MLS 30/30, EPL/La Liga/Serie A/Ligue 1/Liga MX complete, Bundesliga 15/18, UCL 36/42, **NCAAF 109/297, NCAAB 0, Europa League and Conference League 0**. Unmapped teams fail closed (`mapping_failed`), so v2 currently skips many smaller CFB games and every Europa game. Re-run `scripts/build_team_aliases.py` (free) as more games appear.
- UFC and golf outrights are not traded under v2.
- Kickoff times for historical v1 fills are only known for MLB (from the ticker) and three Appendix D NFL games. Everything else is marked unknown.
- The replay can only check v2 rules that stored data supports. Everything else is marked unknown per signal.
- The experiments start from zero observations. Judge the line-move follower and road-dog on CLV at 50 and 100 positions, not on P&L.

## Open questions for you
1. **Which rules trade?** Now v2 trades and v1 is shadowed. (a) Keep this (recommended). (b) Set `PBOT_RULES_VERSION=v1` to trade v1 and log v2 only. Either way, compare them for at least four weeks.
2. **Entry window.** v2 keeps the 72h minimum and shadow-logs the 24–72h, 2–24h and <2h windows. (a) Keep 72h until each window has at least 50 positions with CLV (recommended). (b) Open 24–72h now.
3. **Big-edge threshold.** (a) 7¢, which catches all three Appendix D cases (current). (b) 10¢, per the brief, which lets UCLA–Oregon and SF–SEA through.
4. **Alias coverage** for NCAAF (109/297), NCAAB (0, season not started) and Europa/Conference League (0). (a) I re-run the free builder by hand when you ask. (b) Add a weekly scheduled builder job, which is a new scheduled job and needs your OK. (c) Leave them unmapped; v2 skips them.
5. **Maker shadow checks** run only on the 3 sportsbook runs a day. (a) Also re-check them on the 30-minute book-scanner runs (Kalshi reads are free; small code change, same crons). (b) Leave as is.
6. **UFC and golf** don't trade under v2 (no exact name table). (a) Build exact fighter/golfer name mapping. (b) Keep them off (recommended until the team sports show CLV).
7. **Book closing line.** The brief's optional final-30-minute Odds fetch would cost about one credit per sport per game slot. It is skipped; CLV uses the Kalshi pre-kickoff mid. (a) Keep it skipped (recommended on the free 500/month tier). (b) Upgrade the Odds plan; I'd quote the cost first.
8. **v2 sizes are small**: about 2% of a ~$1,500 bankroll per position, so ~$30, and ~$15 for road-dog. Samples will build slowly. (a) Keep (recommended). (b) Raise the caps after 100 positions if CLV is positive.
