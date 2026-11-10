# Maker backtest from the public trade tape

Decisions since 2026-10-23T00:00:00+00:00 until the latest row; edge = (fair - our price) x 100 - maker fee; all figures in cents per contract after fees; intervals are mean ± 1.96 × SE clustered by game.

## Validity checks

| check | result | detail |
|---|---|---|
| tape sanity | PASS | ok |
| side semantics | PASS | ok |
| one order per position | PASS | 0 overlapping order pairs across 12 positions |

## Headline

| cohort | orders | games | C fills | fill rate | clv_close on C fills (95% CI) | per order placed (95% CI) | C fills from X | clv Q | clv X | clv T | clv A | taker | missed |
|---|--:|--:|--:|--:|---|---|--:|---|---|---|---|---|---|
| edge >= 0.0c | 6 | 6 | 3 | 50% | +1.50c (-0.76 to +3.76) [n=3] | +0.75c (-0.46 to +1.96) [n=6] | 0% | +1.50c [n=3] | - | +1.50c [n=3] | +2.00c [n=6] | -0.30c [n=6] | +2.50c [n=3] |
| edge >= 0.5c | 6 | 6 | 3 | 50% | +1.50c (-0.76 to +3.76) [n=3] | +0.75c (-0.46 to +1.96) [n=6] | 0% | +1.50c [n=3] | - | +1.50c [n=3] | +2.00c [n=6] | -0.30c [n=6] | +2.50c [n=3] |
| edge >= 1.0c | 6 | 6 | 3 | 50% | +1.50c (-0.76 to +3.76) [n=3] | +0.75c (-0.46 to +1.96) [n=6] | 0% | +1.50c [n=3] | - | +1.50c [n=3] | +2.00c [n=6] | -0.30c [n=6] | +2.50c [n=3] |
| edge >= 1.5c | 0 | 0 | 0 | - | - | - | - | - | - | - | - | - | - |
| edge >= 2.0c | 0 | 0 | 0 | - | - | - | - | - | - | - | - | - | - |
| control: edge < 0 | 6 | 6 | 3 | 50% | +2.50c (+0.24 to +4.76) [n=3] | +1.25c (-0.24 to +2.74) [n=6] | 0% | +2.50c [n=3] | - | +2.50c [n=3] | +3.00c [n=6] | +0.70c [n=6] | +3.50c [n=3] |

**Verdict:** NOT YET: 3 of 50 C fills with a closing line; 3 of 30 games among those fills; clv_close interval lower end is -0.76c, not above 0

## Breakdowns at edge >= 1.0c

### By league

| group | orders | games | C fills | fill rate | clv_close on C fills (95% CI) | per order placed | taker |
|---|--:|--:|--:|--:|---|---|---|
| KXNFLGAME | 6 | 6 | 3 | 50% | +1.50c (-0.76 to +3.76) [n=3] | +0.75c [n=6] | -0.30c [n=6] |

### By window

| group | orders | games | C fills | fill rate | clv_close on C fills (95% CI) | per order placed | taker |
|---|--:|--:|--:|--:|---|---|---|
| 72h+ | 0 | 0 | 0 | - | - | - | - |
| 24-72h | 0 | 0 | 0 | - | - | - | - |
| 2-24h | 6 | 6 | 3 | 50% | +1.50c (-0.76 to +3.76) [n=3] | +0.75c [n=6] | -0.30c [n=6] |
| <2h | 0 | 0 | 0 | - | - | - | - |

### By spread

| group | orders | games | C fills | fill rate | clv_close on C fills (95% CI) | per order placed | taker |
|---|--:|--:|--:|--:|---|---|---|
| 1c | 6 | 6 | 3 | 50% | +1.50c (-0.76 to +3.76) [n=3] | +0.75c [n=6] | -0.30c [n=6] |
| 2c+ | 0 | 0 | 0 | - | - | - | - |

### By price band

| group | orders | games | C fills | fill rate | clv_close on C fills (95% CI) | per order placed | taker |
|---|--:|--:|--:|--:|---|---|---|
| <0.40 | 0 | 0 | 0 | - | - | - | - |
| 0.40-0.60 | 6 | 6 | 3 | 50% | +1.50c (-0.76 to +3.76) [n=3] | +0.75c [n=6] | -0.30c [n=6] |
| >0.60 | 0 | 0 | 0 | - | - | - | - |

### By queue_ahead

| group | orders | games | C fills | fill rate | clv_close on C fills (95% CI) | per order placed | taker |
|---|--:|--:|--:|--:|---|---|---|
| 0 | 1 | 1 | 1 | 100% | -0.50c [n=1] | -0.50c [n=1] | -2.80c [n=1] |
| 1-100 | 2 | 2 | 1 | 50% | +1.50c [n=1] | +0.75c [n=2] | -1.30c [n=2] |
| 101-1000 | 3 | 3 | 1 | 33% | +3.50c [n=1] | +1.17c [n=3] | +1.20c [n=3] |
| >1000 | 0 | 0 | 0 | - | - | - | - |

### Settlement P&L on C fills (noisy; for reference)

| cohort | settle (95% CI) |
|---|---|
| edge >= 0.0c | +17.17c (-48.17 to +82.50) [n=3] |
| edge >= 0.5c | +17.17c (-48.17 to +82.50) [n=3] |
| edge >= 1.0c | +17.17c (-48.17 to +82.50) [n=3] |
| control: edge < 0 | +17.17c (-48.17 to +82.50) [n=3] |

## Orders and tape

- Decision rows: 15; candidate orders: 12; scored: 12.
- Skipped decision rows: ask_mismatch 2, position_busy 1.
- Kickoff source: none.
- Unscored orders (tape): none.
- Candidates dropped: none.
- planned windows: 6

## Limits

- Decision points exist only at odds pulls; between pulls the bot had no fair and placed nothing.
- queue_ahead is the size at the best bid when we join, taken from the nearest market snapshot (at most 5 minutes old); orders that join later are ahead of us only if they arrive before us, which the snapshot cannot show.
- Our own order is assumed not to change anyone else's behaviour (no one reacts to the new best bid at bid + 1c).
- The fair is the power-devig sharp fair stored on the decision row.
- Candidates are chained by the C rule: a position stops at its first C fill. The Q, X, T and A columns are measured on those same orders.
- The X rule also uses market snapshots, which are minutes apart; a run-over between two snapshots that left no trade on the tape is missed.
- clv_close uses the T-5 mid recorded for the decision row (clv_status = 'ok' only); orders without one count in fill rates but not in clv figures.

## Rules

- Q (queue): taker volume at or through our price reaches queue_ahead + our size.
- X (run over): a trade strictly through our price, or a later snapshot whose ask for our side is at or below our price.
- C (headline): the earlier of Q and X.
- T (touch): the first taker trade at our price or better (optimistic).
- A (always): filled at placement (no adverse selection; an upper bound).
- taker: the same decisions taken at the ask, (T-5 mid - ask) x 100 - taker fee.
- missed: clv_close of the orders that did not fill under C.
- Block trades never count.
