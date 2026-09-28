# Paper-trading predictions

Updated 2026-09-28 22:32 UTC. Cash **$970.40** (started $1000.00), realized PnL **$0.00**, fees $1.75, settled 0, wins 0, open positions 4.

Brier (lower is better): strategy - vs market price -.

`price` is what we (paper) pay for `side`; `fair` is the strategy's probability that `side` wins; `edge` is fair − price − fees. `traded` = no means the risk limits or cash blocked the fill.

| when (UTC) | strategy | prediction | side | price | fair | edge | size | traded | result | why | ticker |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-28 18:37 | sportsbook\_arb | Ohio St. wins — NO: Kalshi says 16%, we say 18%; bought at 16¢ | no | 0.1600 | 0.1805 | +0.0105 | 69 | yes | open | Ohio State Buckeyes consensus 0.819 from 3 books \(Iowa Hawkeyes vs Ohio State Buckeyes\) | KXNCAAFGAME-26OCT03OSUIOWA-OSU |
| 2026-09-28 18:37 | sportsbook\_arb | North Dakota St. wins — NO: Kalshi says 9%, we say 11%; bought at 9¢ | no | 0.0900 | 0.1101 | +0.0101 | 9 | yes | open | North Dakota State Bison consensus 0.890 from 3 books \(North Dakota State Bison vs Wyoming Cowboys\) | KXNCAAFGAME-26OCT03WYONDSU-NDSU |
| 2026-09-28 18:37 | sportsbook\_arb | Boise St. wins — NO: Kalshi says 8%, we say 10%; bought at 8¢ | no | 0.0800 | 0.1015 | +0.0115 | 100 | yes | open | Boise State Broncos consensus 0.898 from 3 books \(Boise State Broncos vs Utah State Aggies\) | KXNCAAFGAME-26OCT03USUBSU-BSU |
| 2026-09-28 18:37 | sportsbook\_arb | Utah St. wins — YES: Kalshi says 8%, we say 10%; bought at 8¢ | yes | 0.0800 | 0.1015 | +0.0115 | 100 | yes | open | Utah State Aggies consensus 0.102 from 3 books \(Boise State Broncos vs Utah State Aggies\) | KXNCAAFGAME-26OCT03USUBSU-USU |
