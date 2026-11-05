# prediction-bot

Venue-agnostic prediction-market bot. Kalshi (demo environment) is the first venue;
strategies are pluggable; everything runs in **paper-trading mode by default** and logs
every signal, fill and settlement to SQLite so you can measure whether a strategy
actually beats the market price after fees before risking money.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env            # optional

pbot markets --series KXNFLGAME          # public data, no credentials
pbot scan -s book_scanner                # evaluate strategies, print signals, no trades
pbot paper -s book_scanner --interval 60 # paper-trade loop (Ctrl-C to stop)
pbot report                              # PnL, fees, Brier score vs market price
```

### Kalshi demo credentials (for `balance` / `order` / live mode)

1. Create an account at <https://demo.kalshi.co> (mock funds).
2. Account & security → API Keys → create key. Save the private key PEM somewhere
   outside the repo (`*.pem` is git-ignored anyway).
3. Set `PBOT_KALSHI_API_KEY_ID` and either `PBOT_KALSHI_PRIVATE_KEY_PATH` or
   `PBOT_KALSHI_PRIVATE_KEY_PEM` (the PEM contents inline; a single-line paste is fine)
   in `.env`.
4. `pbot balance` should print your demo balance.

`pbot order TICKER yes 0.40 1` places a single real limit order on the configured venue.
Against a non-demo API base it refuses unless you pass `--yes-i-mean-it`.

### Sportsbook odds (for `sportsbook_arb`)

Get a free key from <https://the-odds-api.com> and set `PBOT_ODDS_API_KEY`. Then:

```bash
pbot scan -s sportsbook_arb --series KXNFLGAME --series KXNCAAFGAME
```

Game-winner series (`KXNFLGAME`, `KXNCAAFGAME`, `KXNBAGAME`, `KXNHLGAME`, `KXMLBGAME`, ...)
use the books' head-to-head odds; golf major series (`KXMASTERS`, `KXPGA`, `KXUSOPEN`,
`KXTHEOPEN`) use the books' tournament-winner ("outrights") odds. Each sport that has Kalshi
markets in the scan costs one Odds API request per run; the free tier has no F1 or weekly
PGA Tour odds, so those series are not covered.

## Strategies

| name | idea |
| --- | --- |
| `sportsbook_v3` | The strategy the scheduled runs trade with (`PBOT_RULES_VERSION=v3`). Rules are under [Current rules (v5)](#current-rules-v5). |
| `book_scanner` | Buys both YES and NO when the asks sum to < $1 after fees (locked profit; rare, mostly a pipeline check). |
| `sportsbook_arb` | De-vigs h2h odds from several sportsbooks, averages them, and buys a team on Kalshi when its ask is below consensus probability minus fees and `PBOT_MIN_EDGE`. Only enters while the event still has `PBOT_MIN_LEAD_HOURS` (default 72) before it resolves — same-day entries in efficient markets lose. v1 rules; the workflow does not run it under v3. |
| `underdog_value` | Same consensus model, but only buys sides priced 10c-40c (`PBOT_UNDERDOG_MIN_PRICE`/`MAX_PRICE`): skips near-certain favourites and takes the high-payout side when the books say it is undervalued. Tracked separately so its results can be compared with `sportsbook_arb`. Shares one odds fetch per cycle with `sportsbook_arb`, so it costs no extra quota. v1 rules; the workflow does not run it under v3. |

Add a strategy by subclassing `prediction_bot.strategies.base.Strategy`, implementing
`evaluate(markets) -> list[Signal]` (use `self.make_signal(...)` so fees, `min_edge`
and sizing are applied consistently) and registering it in `strategies/__init__.py`.

## How paper trading works

* Signals are filled immediately at the current ask (a conservative "taker" assumption).
  The fee comes from each series' own Kalshi fee schedule (`fees.fee_for_ticker`): the
  quadratic `0.07 · C · P · (1−P)`, maker fees where a series charges them, and the
  series multiplier (0.5 for MLB), rounded up to the cent.
* Each cycle first checks Kalshi for settled markets we hold and books the payout.
* `pbot report` shows realized PnL, fees, and two Brier scores: the strategy's
  `fair_prob` vs outcomes, and the *market price paid* vs outcomes. The strategy has
  to beat the market Brier score, not just have positive PnL on a small sample.

## Current rules (v5)

What `sportsbook_v3` does on the scheduled runs, with the workflow's defaults. Each
setting is a repository Variable, so it can change without a code change.

* **Reference price.** Sharp books only (Pinnacle, else the median of Circa and BetOnline), power devig.
  Soft books are off in the workflow (`PBOT_V3_ALLOW_SOFT_REFERENCE=false`). Books older
  than 3h, or with no readable update time, don't count.
* **Entry.** Buy at the Kalshi ask only when the edge after that series' fee is at least
  `PBOT_V2_MIN_NET_EDGE` (2¢; twice that below 40¢), the price is 25¢–90¢, at least 50
  contracts are offered, and kickoff is 72–168h away (`PBOT_ENTRY_WINDOW_MIN_HOURS` /
  `PBOT_ENTRY_WINDOW_MAX_HOURS`). Rejected candidates are logged with a reason and scored
  as near misses.
* **Execution.** `PBOT_V3_EXECUTION=auto`: taker orders until practice resting orders
  pass the maker gate (50+ queue-model fills with the whole 95% range of their
  closing-price gain above zero, and no worse than the taker fills).
* **Size.** One unit per bet (`PBOT_SIZING_MODE=flat`). `kelly` sizes from the fill
  cohort's own closing-price gain, and only once that cohort is supported.
* **Odds credits.** The free tier's ~500 credits a month go to the 72h+ rotation, a pull
  plan timed to scheduled news (`data/release_calendar.json`: NFL status reports and
  inactives, lineups) plus the T−10m sharp close, and a 50-credit reserve for
  move-trigger pulls.
* **Polymarket** is read-only reference data, never traded.

The full change log is `docs/CHANGES_v5.md`.

## How to read the evidence

* **Closing-price gain (CLV) is the evidence.** Each bet and each near miss is scored, in
  cents per contract, against the Kalshi mid 5 minutes before kickoff (`clv_kalshi`), the
  Kalshi bid (`clv_kalshi_bid`), the devigged sharp close (`clv_sharp_close`) and
  Polymarket (`clv_polymarket`, reference only). Only rows with `clv_status='ok'` count.
* **The rule.** A cohort is *supported* once it has n ≥ 50 scored rows and the whole 95%
  confidence interval is above zero. It is *rejected* at n ≥ 50 with the whole interval
  below zero, and *open* otherwise. Nothing changes because of an open cohort.
* **P&L is not evidence.** On 87 settled fills (as of 5 Nov 2026) the bot made +$560.02
  against +$153.84 its own fair prices expected. The gap is about 1.4 standard deviations
  of luck, not skill.
* **Where to look.** `pbot clv-report` (add `--json` for the snapshot format), the
  dashboard's "Measurement health" section, and dated snapshots in
  [`docs/evidence/`](docs/evidence/).

## Scheduled paper trading (GitHub Actions)

`.github/workflows/paper.yml` runs one paper cycle every 30 minutes against Kalshi's
**public production** prices (paper mode never places orders; no Kalshi credentials
are used). The book scanner runs every cycle. The sportsbook strategy runs on the :17 cycle
of every even UTC hour (and on manual runs). The other cycles pull odds only for a
due pull-plan target or a line-move trigger. An odds pacer keeps the month inside The
Odds API free tier (500 requests/month). Each cycle commits `paper.sqlite`, `last_run.txt`, `REPORT.txt` and a human-readable
`PREDICTIONS.md` (every signal with price, fair estimate, edge and outcome once settled) to
the `paper-ledger` branch — that file is the place to see what the bot is predicting.
Locally, `pbot predictions` prints the same table.

The run also writes `index.html`, a self-contained dashboard (cards, equity curve, per-run
activity, and sortable/filterable Predictions / Trades / Runs tables). Publish it with
GitHub Pages: Settings → Pages → Source "Deploy from a branch" → branch `paper-ledger`,
folder `/ (root)`; it then lives at `https://<owner>.github.io/<repo>/`. Locally,
`pbot dashboard --out data/index.html` renders the same page from your ledger.

Add `PBOT_ODDS_API_KEY` as a repository Actions secret
(Settings → Secrets and variables → Actions) to include the sportsbook strategy. Odds
feeds are chosen per run, not fixed: a feed is pulled only if Kalshi lists its games
early enough for the entry window (`feed_stats`: 90th-percentile lead of at least
`PBOT_ENTRY_WINDOW_MIN_HOURS` + 6h) and it has mapped games, which keeps the month inside
The Odds API free tier. Trigger a run by hand
from the Actions tab (`workflow_dispatch`).

Demo-venue prices are thin and often stale, so edges seen with the default demo URL
overstate what the real book offers — evaluate strategies on production data.

### Scheduler reliability

GitHub drops many scheduled slots. A Cloudflare Worker (`infra/cloudflare-trigger`, setup steps in its README) can start `paper.yml` every 10 minutes (:07, :17, … :57) through `workflow_dispatch` with a `slot` input. The GitHub schedule stays as a fallback, and `pbot slot-guard` stops a second run of the same slot. A tick within 25 minutes of the last scanner run is idle (no ledger commit) unless a pull-plan target is due.

An hourly `health.yml` workflow runs `pbot health`, which fails once the ledger's last run is more than 90 minutes old (repo Variable `PBOT_HEALTH_MAX_AGE_MINUTES`; 40 once the Worker is live). On failure it opens or updates one issue titled "paper bot missed runs".

## Safety

* `PBOT_MODE=paper` (default) never sends orders. `Engine` only sends live orders when
  mode is `live` **and** the caller passes `allow_live=True`; no CLI command does that yet.
* `PBOT_MAX_ORDER_NOTIONAL` and `PBOT_MAX_POSITION_CONTRACTS` cap every signal.
* `PBOT_KILL_SWITCH=true` halts every new entry, including manual `pbot order` (and
  cancels resting live orders); `PBOT_MAX_DAILY_LOSS=<dollars>` halts entries for the rest
  of the UTC day once realized PnL settled that day reaches the limit — the halt is latched
  in the ledger, so a later winning settlement does not reopen trading that day. The
  daily-loss limit is only measured from the paper ledger; with it set, live mode refuses
  to trade (fail closed) until live PnL tracking exists. Both live in code and no strategy
  can override them. The scheduled workflow reads them from the repository *variables*
  `PBOT_KILL_SWITCH` / `PBOT_MAX_DAILY_LOSS` (Settings → Secrets and variables → Actions →
  Variables), so you can pull the plug without a code change.
* `pbot report`, `PREDICTIONS.md` and the dashboard break settled count, wins, realized
  PnL and Brier out **per strategy**, so `book_scanner`, `sportsbook_arb` and
  `underdog_value` can be judged on their own.
* Kalshi 429s are retried with exponential backoff.

## Kalshi API notes

* Demo REST root: `https://external-api.demo.kalshi.co/trade-api/v2`.
* Auth: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE`
  = base64(sign(`timestamp + METHOD + /trade-api/v2/path`)), RSA-PSS-SHA256 or Ed25519.
* Orders go to `POST /portfolio/events/orders` and are quoted from the YES leg:
  `side=bid` buys YES at `price`, `side=ask` sells YES (≡ buys NO at `1 − price`).
  `KalshiClient.create_order(ticker, OutcomeSide.no, count, no_price)` does that
  conversion for you.
* Prices/quantities are fixed-point strings (`*_dollars`, `*_fp`).

## Development

```bash
ruff check src tests && ruff format --check src tests
mypy
pytest
```

## Roadmap

* Polymarket as a venue behind the same `Market`/`Signal` types (today it is read-only
  reference data).
* Economic-release reaction strategy (CPI / jobs / Fed).
