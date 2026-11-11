# Underdog lane

`underdog_value` picks are real paper entries again, beside v3. Rollback: set the repo
Variable `PBOT_UNDERDOG_LANE` to `off` (shadow rows only, as before).

## What it is

`underdog_value` buys a side priced 10c to 25c when the de-vigged average of every
configured sportsbook rates it above the price by `PBOT_MIN_EDGE` (1c) after the fee. It
made 24 paper fills in October and has only run in shadow since the move to v2/v3.

The lane enters those picks as two books built from the same picks.

| Book | How a pick fills | Moves cash |
|---|---|---|
| Lane book | A pick priced inside the spread (1c above the bid) rests as a paper order and fills only when the market later trades through that price. A pick with no room inside the spread is bought at the ask. | Yes |
| Instant-fill book | The first pick of each game is booked as filled at its price the moment it is made. This is how October was booked. | No |

The gap between the two rows is what assuming an instant fill was worth.

## Why two books

On 2026-11-10 the shadow picks since Oct 25 were scored by hand against the ledger
(`paper-20261110T204756Z-r1162`):

- 40 picks from 32 finished games gained **+3.15c** to the closing price at the price
  the strategy logs (95% range +2.0c to +4.3c, clustered by game).
- That price is 1c above the bid. Where the real ask was on record it sat a median 3c
  higher, and the 8 picks that had one gained +0.9c at the ask before the fee
  (range -1.8c to +3.5c) and -0.2c after it.
- v3's sharp-book fair rated none of the 19 matched picks at or above zero at the ask.
- 4 of the 24 picks with a known result won, at an average price of 18c.

So the picks look good only if a bid 1c above the best bid really fills, and fills
without the price having already moved against it. The lane book measures exactly that.

## Fill rule

The lane book reuses v3's paper orders (`paper_orders`, `Engine._resolve_paper_orders`):

- The order rests from the pick's timestamp for `PBOT_UNDERDOG_LANE_TTL_MINUTES` (480),
  and never past the kickoff buffer.
- Every cycle, including scanner cycles, fetches 1-minute candles for each open order.
  A YES bid at p fills on a candle whose traded low is below p. A NO bid at p fills on a
  traded high above 1 - p. A trade exactly at p does not fill it.
- It fills at p with the maker fee of its series.
- A fresh odds pull that prices the game again and no longer makes it a pick pulls the
  order (`cancel_reason = edge_gone`). A game without a fresh pull keeps its order
  until the TTL.
- v3's own fair updates never touch a lane order: its fair is the all-book average, not
  the sharp fair.

Limits of the rule, both in the lane's favour:

- Size is not modelled. One contract trading through the price fills the whole order.
- A trade in the order's first minute counts even if it printed seconds before the pick.

## Guards

Each skipped pick is a `blocks` row with strategy `underdog_value` and rules `v1`.

| Reason | Meaning |
|---|---|
| `lane_league_disabled` | The sport is in `PBOT_V2_DISABLED_SPORTS` (NBA). |
| `lane_no_kickoff` | The matched game has no usable kickoff time. |
| `lane_entry_window` | Kickoff is closer than `PBOT_ENTRY_WINDOW_MIN_HOURS` (72). There is no upper bound, as in v1. |
| `lane_mapping` | The alias tables disagree with the fuzzy team match (detail `teams`), or v3 matched this Kalshi event to a different sportsbook game (detail `event`). |
| `lane_big_edge` | Fair is more than `PBOT_UNDERDOG_LANE_MAX_GROSS_EDGE` (10c) above the price. |
| `lane_held` | The lane already filled in this game. One side per game. |
| `lane_resting` | A lane order is already resting in this game. |
| `lane_fee` | No fee schedule for the series. |
| `lane_budget` | Lane fills plus resting orders would exceed `PBOT_UNDERDOG_LANE_MAX_OPEN_FRACTION` (30%) of the bankroll. |
| `lane_cash` | Not enough free paper cash. |

`lane_mapping` is the fix for the October LAFC / LA Galaxy fill, where the fuzzy match
priced the Galaxy leg with LAFC's 61% fair and bought it at 18c.

Unchanged from v1: the 10c to 25c band, `PBOT_MAX_ORDER_NOTIONAL` ($25) and
`PBOT_MAX_POSITION_CONTRACTS` (100) per pick, the bid depth of 10 contracts, and the
sportsbook list.

## Daily stops

- The lane stops for the rest of the UTC day once its own settled PnL reaches
  `-PBOT_UNDERDOG_LANE_MAX_DAILY_LOSS` ($50). It is recomputed each cycle, not latched
  in `halts`.
- `PBOT_MAX_DAILY_LOSS` now ignores lane fills, so a bad underdog day no longer halts
  v3. The kill switch and a global halt still stop the lane.
- Worst case for one day is therefore the two limits added together.

Both are entry stops. Neither closes a position that is already open.

## Books stay separate

- Lane fills and orders are `strategy = underdog_value`, `rules_version = v1`, tagged
  `underdog_lane`, with position keys prefixed `ud:`.
- v3's position, event, day and total caps, its held-position checks, the maker gate
  and the Kelly cohort all filter on v2/v3 and do not see the lane.
- Cash is shared: a resting lane order reserves its cost.
- The lane and v3 can hold opposite sides of one game. They are scored as separate books.

## Where to look

- Dashboard: "Underdog bets (sides priced 10c to 25c)", one row per book, plus counts
  of orders placed, filled, waiting, expired and pulled.
- `pbot clv-report`: cohorts `underdog_lane` (resting orders that filled),
  `underdog_instant` (the same resting-order picks booked as filled at once) and
  `underdog_lane_ask` (bought at the ask). They use the same rule as every other
  cohort: 30 games and the whole game-clustered range of the closing-price gain after
  fees on one side of zero.
- Each paper run prints `underdog lane: orders_placed=… bought_at_ask=… pulled=…
  skipped=[…]`.

## Code

- `src/prediction_bot/underdog_lane.py`: guards, order placement, the lane's stop.
- `strategies/sportsbook_arb.py`: `GamePick` and `seen_game_picks()` give each v1 signal
  its game, kickoff and the exact team check. Signals themselves are unchanged.
- `engine.py`: runs the lane after v3's entries when `underdog_value` is a shadow
  strategy, `PBOT_UNDERDOG_LANE=paper` and the engine is not live.
- `paper/ledger.py`: lane queries, `worst_drawdown_since(lane=…)`, the instant-fill
  book in `shadow_decisions` under reason `underdog_lane`.
- `clv.py`: the instant-fill book is scored against the close like a near miss.

With `PBOT_RULES_VERSION=v1` the strategy runs as a primary strategy and the lane does
nothing: v1 mode books fills exactly as it did.
