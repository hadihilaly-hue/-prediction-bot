# Sharp lane

v3's sharp-backed sides are real paper entries, at every price, in two bands. The old
underdog lane becomes a no-cash comparison book. Rollback: set the repo Variable
`PBOT_SHARP_LANE` to `off`, and `PBOT_UNDERDOG_LANE` to `paper` to put cash back on the
underdog lane.

## Why

Paper ledger of 2026-11-24. The underdog lane buys sides priced 10c to 25c when the v1
fair (every book, proportional devig) is above the price. Its fills lose to the close: 13
of 14 graded fills closed below entry, 2.6c worse on average. The problem is which orders
fill. Of the 67 games with a lane order, grouped by v3's sharp fair on the same day:

| Sharp fair against the order price | Games | Filled |
|---|---|---|
| 1c or more above | 30 | 2 |
| 0 to 1c above | 9 | 4 |
| Below | 11 | 7 |
| No v3 price (mostly games 7+ days out) | 17 | 13 |

All 10 graded resting fills are in the last three rows.

v3's own practice resting orders (maker shadows: sharp-backed, every price) are the better
base. Since Nov 11: 621 placed, 24 fills under the trade-through rule, 13 graded at +0.7c
(interval -1.5 to +2.9). Unproven, but not negative.

## Candidates

Exactly the sides the practice book (`SportsbookV2Strategy._feed_maker_shadows`) considers
each cycle:

- v3 near misses blocked only by `net_edge`, `entry_window`, `price_floor` or
  `thin_market`, plus v3 candidates;
- with v3's fair as computed (fresh sharp reference, `PBOT_V3_DEVIG_METHOD`) above the
  side's mid;
- whose game has a known fee schedule and kickoff.

The strategy exposes them as `seen_sharp_lane_candidates()` (beside `seen_maker_shadows()`)
and the games it priced this cycle as `seen_sharp_lane_events()`. What the practice book
records is unchanged. Sides from a measure-only feed and sports in
`PBOT_V2_DISABLED_SPORTS` are skipped.

## Bands

By entry price: `longshot` 0.10 to 0.25, `main` above 0.25 up to 0.90. A price outside
both, or in a band switched off with `PBOT_SHARP_LANE_BANDS`, is not entered.

## Entry rule

Per candidate:

1. **Taker.** If fair - ask - taker fee >= `PBOT_SHARP_LANE_TAKE_EDGE` (0.01): filled at
   the ask now, tagged `taker`, `rule_ask`.
2. **Maker.** Otherwise the practice book's price: bid + 1c when the spread is 2c or more
   (`rule_bid_plus_1`), the bid when it is 1c (`rule_at_bid`). If fair - that price -
   maker fee >= `PBOT_SHARP_LANE_MIN_EDGE` (0.01): a resting paper order, tagged `maker`.
3. Otherwise a `sharp_lane_edge` block.

Fees in the edge test are the unrounded per-contract rate of the series' schedule; the fee
booked is rounded as every other paper fee.

Resting orders use v3's paper-order machinery and the honest fill rule
(`Engine._resolve_paper_orders`, `trade_through_fill`): a YES bid at p fills only on a
1-minute candle that trades below p (a NO bid, above 1 - p), at p with the maker fee. The
TTL is `PBOT_SHARP_LANE_TTL_MINUTES` (480) and never runs past the kickoff buffer. An order
is pulled with `cancel_reason = edge_gone` when a later cycle prices the game again and
the side no longer qualifies at the order's price; a game not priced again keeps its order
to the TTL. v3's own fair updates (`update_paper_orders_for_fair`) never touch a lane order.

## Tags

Every fill, order, twin row and signal carries `sharp_lane`, its band (`band_longshot` /
`band_main`), `maker` or `taker`, the price rule (`rule_ask`, `rule_bid_plus_1`,
`rule_at_bid`), the sharp gap at decision (`gap_0-1c`, `gap_1-2c`, `gap_2-3c`, `gap_3c+`:
fair - mid) and the taker gates that blocked it exactly as the practice book tags them
(`blocked_net_edge`, `blocked_entry_window`, `blocked_price_floor`, `blocked_thin_market`,
or `taker_pass`). Blocks carry the same in `detail`
(`band=main maker rule=bid_plus_1 gates=blocked_net_edge`).

## Guards

Each skipped candidate is a `blocks` row with strategy `sharp_lane`, rules
`sharp_lane`, in this order:

| Reason | Meaning |
|---|---|
| `sharp_lane_measure_only` | The side comes from a measure-only feed. |
| `sharp_lane_league_disabled` | The sport is in `PBOT_V2_DISABLED_SPORTS`. |
| `sharp_lane_fee` | No fee schedule for the series. |
| `sharp_lane_band` | No entry price (ask or resting price) sits in an enabled band. |
| `sharp_lane_edge` | Not enough edge at the ask or at the resting price. |
| `sharp_lane_daily_stop` | The lane's own daily stop has tripped. |
| `sharp_lane_game` | Another side of the same game has more edge (one per game). |
| `sharp_lane_v3_held` | v3 holds an open position or has an open paper order in the game. |
| `sharp_lane_held` | The lane already filled in this game, settled or not: no re-entry. |
| `sharp_lane_resting` | A lane order is already resting in this game. |
| `sharp_lane_size` | The notional buys no whole contract. |
| `sharp_lane_kickoff` | A resting order would expire before it is placed. |
| `sharp_lane_budget` | Lane fills plus resting orders would exceed `PBOT_SHARP_LANE_MAX_OPEN_FRACTION` (0.30) of the bankroll. |
| `sharp_lane_cash` | Not enough free paper cash. |

## Stake and limits

- Flat `PBOT_SHARP_LANE_ORDER_NOTIONAL` (10 dollars) per entry: floor(notional / price)
  contracts, capped by `PBOT_MAX_POSITION_CONTRACTS`.
- One position per game across both bands (the highest edge), one resting order per game,
  no re-entry after a fill.
- Lane fills plus resting orders may use at most `PBOT_SHARP_LANE_MAX_OPEN_FRACTION` (0.30)
  of the bankroll (cash plus open position cost).
- Daily stop: the lane stops for the rest of the UTC day once its own settled PnL reaches
  `-PBOT_SHARP_LANE_MAX_DAILY_LOSS` (50). It is recomputed each cycle, not latched.
  `PBOT_MAX_DAILY_LOSS` ignores sharp-lane fills (and underdog-lane fills), so the worst
  day is the limits added together. The kill switch and a global halt still stop the lane.

## Books stay separate

- Strategy `sharp_lane`, rules version `sharp_lane`, position keys `sl:` + v3's key. v3's
  position, event, day and total caps, its held checks, the maker gate and the Kelly cohort
  filter on v2/v3 and never see the lane; `paper_order_reserved_cost(include_lane=False)`
  leaves lane orders out of v3's open cost.
- Cash is shared: a resting lane order reserves its cost.
- The lane skips games v3 is in; v3 does not look at the lane.

## Instant-fill twin

The first entry of each game is also logged as if it filled at once at its price
(`shadow_decisions`, reason `sharp_lane`, `would_trade = 1`), never touching cash. It is
scored against the close like the underdog lane's instant-fill book, so filled orders can
be compared with all picks.

## Switch

`PBOT_SHARP_LANE` is `off` or `paper`. Code default `off`, workflow default `paper`. The
lane does nothing when `PBOT_MODE=live`, whatever the switch says; there is no live code
path.

## Evidence

`pbot clv-report` cohorts, each with the standard net-of-fee, game-clustered verdict
(30 games; supported when the whole 95% interval of net CLV is above zero, rejected when it
is below):

| Cohort | Rows | Fee |
|---|---|---|
| `sharp_lane` | resting orders the market traded through (cash) | maker |
| `sharp_lane_ask` | entries bought at the ask (cash) | taker |
| `sharp_lane_instant` | each game's first pick booked as filled at once (no cash) | its own tag |

Each is split by window, league (`sport`), price band and edge bucket as every cohort, plus
`band`, `price_rule` and `sharp_gap`.

Dashboard: "Sharp lane (sharp-backed bets at every price)", one row per band and one for the
instant-fill twin (bets, open, won, profit, average price move), plus orders placed,
filled, waiting, expired and pulled, the fill rate (filled over finished orders) and the
count bought at the ask.

## Switching a band off

Read `clv-report` for `sharp_lane` and `sharp_lane_ask` split `by_band`, on `clv_kalshi`
net of fees. When a band reaches 30 or more games in a cohort and its verdict is
`rejected` (the whole game-clustered 95% interval of net CLV below zero), switch that band
off: set the repo Variable `PBOT_SHARP_LANE_BANDS` to the other band (`main` or
`longshot`). Before 30 games the verdict is `open` and the band stays on. Fills already
made settle normally; the twin and the other band keep accumulating.

## Code

- `src/prediction_bot/sharp_lane.py`: entry rule, guards, edge-gone pulls, the daily stop.
- `strategies/sportsbook_v2.py`: `seen_sharp_lane_candidates()` and
  `seen_sharp_lane_events()`, filled from `_feed_maker_shadows` without changing what it
  records.
- `models.py`: `SharpLaneCandidate`.
- `engine.py`: runs the lane after v3's entries and the underdog lane when
  `PBOT_SHARP_LANE=paper` and the mode is not live.
- `paper/ledger.py`: lane queries, `worst_drawdown_since(lane="sharp")`, the instant twin,
  and the underdog lane's no-cash orders and fills.
- `clv.py`, `clv_report.py`, `dashboard.py`: grading, cohorts and the section.
