---
name: testing-pbot-demo-cli
description: Safely exercise prediction-bot CLI against Kalshi demo with recorded terminal evidence and cleanup.
---

# Prediction-bot demo CLI testing

## Devin Secrets Needed
- PBOT_KALSHI_API_KEY_ID
- PBOT_KALSHI_PRIVATE_KEY_PEM
- Sportsbook real-data testing additionally requires PBOT_ODDS_API_KEY.

## Setup
Use the repository `.venv` and install `pip install -e '.[dev]'` only if necessary.
Check current `config.py` and `cli.py`: the PEM secret may not be directly consumed by Settings. If the CLI requires `PBOT_KALSHI_PRIVATE_KEY_PATH`, use an in-memory Linux memfd rather than writing the private key to disk. A long-lived Python launcher can call `os.memfd_create`, write the PEM environment value to it, set the child's key path to `/proc/<launcher pid>/fd/<fd>`, and keep the launcher alive while the terminal runs. Never print PEM or API-key values.

The host setting is `PBOT_KALSHI_API_BASE`, not `PBOT_KALSHI_REST_URL`. The default host is `https://external-api.demo.kalshi.co/trade-api/v2`.

For a recorded CLI session, start a separate Konsole inheriting the launcher's environment and `.venv/bin` PATH. Run `script -q -f <artifact path> -c 'bash --noprofile --norc'` for a transcript. Maximize with `wmctrl`, click terminal to focus, and record GUI interactions. Close the terminal at completion so the launcher's memory-backed key disappears.

## Real-data checks
Use `pbot markets --series KXNFLGAME --limit 5` to seek active liquid demo markets; the unfiltered list may mainly contain empty multivariate markets. Recheck prices before selecting a tiny far-off limit order.

Run `pbot scan`, `pbot paper --cycles 1`, and `pbot report`. Baseline the existing SQLite ledger first; do not reset someone else's data. No signals is a valid liveness result, not proof of fills, fees, atomic grouped admission or FOK behavior. Report these separately as untested when no natural opportunity exercises them.

## Risk guards and cleanup
- Default max position is 100 contracts: 101 exceeds it; 100 does not inherently exceed it. To test rejecting count 100, explicitly lower the configured cap.
- Supply `--` before positional arguments for negative counts, to exercise application validation rather than option parsing.
- A non-demo URL containing `demo` in its path should still refuse a valid-shaped order without `--yes-i-mean-it`. Never supply the override for non-demo testing.
- Baseline account balance, positions and resting orders. Place no more tiny demo orders than required. A one-contract resting order plus a cap of one should make the next one-contract attempt reject existing exposure.
- Check `pbot --help`: cancellation may be client-only via `prediction_bot.cli._client(_settings(), True).cancel_order(order_id)`. Cancel only orders created by the test.
- Demo reads immediately after cancellation may briefly return stale resting orders. Confirm cancellation acknowledgement, then re-read until the test order is absent within a bounded interval; never equate acknowledgement alone with cleanup.
- Demo endpoints may return transient HTTP 500. Record the failure, then perform a bounded read-only retry; do not silently erase the failed observation. Avoid retrying order writes without checking whether an order was already accepted.
- End by verifying no new resting orders or positions and unchanged balance if no fills occurred.
