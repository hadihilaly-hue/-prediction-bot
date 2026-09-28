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

## Strategies

| name | idea |
| --- | --- |
| `book_scanner` | Buys both YES and NO when the asks sum to < $1 after fees (locked profit; rare, mostly a pipeline check). |
| `sportsbook_arb` | De-vigs h2h odds from several sportsbooks, averages them, and buys a team on Kalshi when its ask is below consensus probability minus fees and `PBOT_MIN_EDGE`. |

Add a strategy by subclassing `prediction_bot.strategies.base.Strategy`, implementing
`evaluate(markets) -> list[Signal]` (use `self.make_signal(...)` so fees, `min_edge`
and sizing are applied consistently) and registering it in `strategies/__init__.py`.

## How paper trading works

* Signals are filled immediately at the current ask (a conservative "taker" assumption)
  with Kalshi's general fee formula `ceil(0.07 · C · P · (1−P))`.
* Each cycle first checks Kalshi for settled markets we hold and books the payout.
* `pbot report` shows realized PnL, fees, and two Brier scores: the strategy's
  `fair_prob` vs outcomes, and the *market price paid* vs outcomes. The strategy has
  to beat the market Brier score, not just have positive PnL on a small sample.

## Scheduled paper trading (GitHub Actions)

`.github/workflows/paper.yml` runs one paper cycle three times a day against Kalshi's
**public production** prices (paper mode never places orders; no Kalshi credentials
are used) and commits `paper.sqlite`, `last_run.txt`, `REPORT.txt` and a human-readable
`PREDICTIONS.md` (every signal with price, fair estimate, edge and outcome once settled) to
the `paper-ledger` branch — that file is the place to see what the bot is predicting.
Locally, `pbot predictions` prints the same table.

The run also writes `index.html`, a self-contained dashboard (cards, equity curve, per-run
activity, and sortable/filterable Predictions / Trades / Runs tables). Publish it with
GitHub Pages: Settings → Pages → Source "Deploy from a branch" → branch `paper-ledger`,
folder `/ (root)`; it then lives at `https://<owner>.github.io/<repo>/`. Locally,
`pbot dashboard --out data/index.html` renders the same page from your ledger.

Add `PBOT_ODDS_API_KEY` as a repository Actions secret
(Settings → Secrets and variables → Actions) to include `sportsbook_arb`; the run
covers NFL/NCAAF/NBA/NHL to stay within The Odds API free tier. Trigger a run by hand
from the Actions tab (`workflow_dispatch`).

Demo-venue prices are thin and often stale, so edges seen with the default demo URL
overstate what the real book offers — evaluate strategies on production data.

## Safety

* `PBOT_MODE=paper` (default) never sends orders. `Engine` only sends live orders when
  mode is `live` **and** the caller passes `allow_live=True`; no CLI command does that yet.
* `PBOT_MAX_ORDER_NOTIONAL` and `PBOT_MAX_POSITION_CONTRACTS` cap every signal.
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

* Polymarket adapter (CLOB API) behind the same `Market`/`Signal` types.
* WebSocket order-book feed (`use_yes_price: true`) instead of REST polling.
* Maker-side paper fills (rest at bid, fill only when trades print through).
* Economic-release reaction strategy (CPI / jobs / Fed).
