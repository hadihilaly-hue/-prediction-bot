# Paper-trading predictions

Updated 2026-09-28 18:37 UTC. Cash **$970.40** (started $1000.00), realized PnL **$0.00**, fees $1.75, settled 0, wins 0, open positions 4.

Brier (lower is better): strategy - vs market price -.

`price` is what we (paper) pay for `side`; `fair` is the strategy's probability that `side` wins; `edge` is fair − price − fees. `traded` = no means the risk limits or cash blocked the fill.

| when (UTC) | strategy | market | side | price | fair | edge | size | traded | result | why |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-28 18:37 | sportsbook\_arb | KXNCAAFGAME-26OCT03OSUIOWA-OSU | no | 0.1600 | 0.1805 | +0.0105 | 69 | yes | open | Ohio State Buckeyes consensus 0.819 from 3 books \(Iowa Hawkeyes vs Ohio State Buckeyes\) |
| 2026-09-28 18:37 | sportsbook\_arb | KXNCAAFGAME-26OCT03WYONDSU-NDSU | no | 0.0900 | 0.1101 | +0.0101 | 9 | yes | open | North Dakota State Bison consensus 0.890 from 3 books \(North Dakota State Bison vs Wyoming Cowboys\) |
| 2026-09-28 18:37 | sportsbook\_arb | KXNCAAFGAME-26OCT03USUBSU-BSU | no | 0.0800 | 0.1015 | +0.0115 | 100 | yes | open | Boise State Broncos consensus 0.898 from 3 books \(Boise State Broncos vs Utah State Aggies\) |
| 2026-09-28 18:37 | sportsbook\_arb | KXNCAAFGAME-26OCT03USUBSU-USU | yes | 0.0800 | 0.1015 | +0.0115 | 100 | yes | open | Utah State Aggies consensus 0.102 from 3 books \(Boise State Broncos vs Utah State Aggies\) |
