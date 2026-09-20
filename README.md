# tmbot — semi-automated trade manager for Capital.com

You take the entry. The bot takes everything after it.

It never opens a position. It watches your Capital.com account, notices when you
have manually entered a trade, asks you to confirm, and then runs the exit plan:
scaled partial closes, an automatic move to break-even, and a trailing stop that
tightens or holds with the trend. Alongside that it publishes a daily
analytical report per instrument — a direction bias, three targets and one stop
derived from support/resistance and liquidity zones.

---

## What it does

**1. Daily analytical report (technical + fundamental)**

- Pulls OHLCV and the live quote from the same Capital.com connection your fills
  come from, so the analysis and your executions agree on price.
- Technical read: eight weighted, individually reported factors (trend
  structure, EMA cross and slope, MACD, RSI, ADX with directional index, range
  position, momentum) producing a signed score and a confidence.
- Fundamental read: headlines from Marketaux or Finnhub, synthesised by Claude
  into a bias with drivers, risks and catalysts. The model is asked for
  direction only — **it is never asked for price levels**, so a hallucinated
  number can never become your stop. Without an API key a transparent keyword
  lexicon takes over and the report still ships.
- Output: **one direction bias** (BULLISH / BEARISH / NEUTRAL), **exactly three
  targets** (TP1/TP2/TP3) and **one stop**, every time — structural zones where
  they exist, ATR projections where they do not, with the method stated in the
  report.
- Runs on a schedule for a watchlist, refreshes intraday, and tells you when
  the bias flips or a level is invalidated.

**2. Automated trade management (post-entry)**

Two exit models, chosen with `management.exit_model`.

`partial_close` — you open **one** deal and it gets sliced:

| Trigger | Action |
| --- | --- |
| TP1 trades | Close 50% of the original size |
| TP1 trades | Move the stop to the **exact entry price** |
| TP2 trades | Close a further 25% of the original size |
| Trend strong, price near TP3 | Push TP3 out by 1×ATR rather than exiting into momentum (capped) |
| TP3 trades (no extension left) | Close the runner |
| Trend strong / moderate | Trail the stop behind price (chandelier + structure floor) |
| Trend weak | Hold the stop where it is |

`three_deals` — you open **three** deals and each is closed whole at its own target:

| Trigger | Action |
| --- | --- |
| TP1 trades | Close deal 1 entirely; move deals 2 and 3 to the **exact entry price** |
| TP2 trades | Close deal 2 entirely |
| TP3 trades (no extension left) | Close deal 3 |
| Trend strong, price near TP3 | Extend TP3 for deal 3 only |
| Trend strong / moderate | Trail deal 3 only — deals 1 and 2 must not be trailed out of their targets |

Deals on the same instrument and side opened within `group_window_minutes` of
each other are recognised as one basket, and a single `/confirm` adopts all of
them. A deal that fills late joins the basket without asking again.

> **`three_deals` needs hedging mode switched on at Capital.com.** With it off
> the broker nets the three deals into a single position and the legs cannot be
> closed separately. The bot checks at startup and says so rather than
> discovering it mid-trade.

---

## Install

```bash
git clone https://github.com/Boojeo/LotfyApp2.git && cd LotfyApp2
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # credentials
cp config.example.yaml config.yaml
```

Fill in `.env` with a Capital.com API key (Settings → API integrations; demo and
live need separate keys).

## Run

`--env` is mandatory and has no default, so a live account is never touched by
accident.

```bash
# Check the connection and see how partial closes will be executed
python -m tmbot --env demo --config config.yaml probe
python -m tmbot --env demo --config config.yaml account

# Build a report without connecting to anything else
python -m tmbot --env demo --config config.yaml report GOLD --markdown

# Start the manager (evaluate everything, send nothing)
python -m tmbot --env demo --config config.yaml --dry-run run

# Start the manager for real
python -m tmbot --env demo --config config.yaml run
```

Prove the whole lifecycle on demo before pointing it at `--env live`.

## Telegram

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` and the chat interface turns on
automatically. Only the configured chat id is accepted — commands from anywhere
else are logged and dropped.

```
/status          open positions and ladder state
/confirm <id>    start managing a detected position
/decline <id>    leave a detected position alone
/manage <id>     take over a position declined earlier
/report [epic]   rebuild and send the full plan
/plan [epic]     show the stored plan levels
/close <id>      close the remaining size now
/be <id>         move the stop to entry now
/pause /resume   stop or restart all order modifications
```

---

## How the management logic works

### Adoption: confirm before touching anything

Each cycle the bot lists open positions. Anything it has not seen before is
matched to the day's plan for that instrument (or a plan is built on the spot),
rebased onto your actual fill, and offered to you:

```
New position detected: GOLD BUY 2.0 @ 3412.40
Plan GOLD-20260918-aeaf45 -- bias BULLISH
SL 3398.10   TP1 3421.00   TP2 3432.50   TP3 3450.00
Ladder: TP1 50%, TP2 25%; break-even at TP1; trail after TP1

/confirm 000001 to manage it, /decline 000001 to leave it alone.
```

Nothing is sent to the broker until you confirm. Unanswered offers expire (30
minutes by default) rather than lingering. On confirmation the bot installs the
plan's stop and final target, then starts managing.

Rebasing keeps targets where structure put them and preserves the stop's
*distance*, so a worse fill does not silently widen your risk.

### The ladder (`partial_close`)

Fractions are of the **original** size, and every size is rounded **down** to the
instrument's increment so the runner is never over-closed. If a rung cannot be
executed legally (the slice or the remainder would fall below the minimum deal
size), the bot says so and holds the full size rather than guessing — but the
break-even move still happens.

### The basket (`three_deals`)

Each deal carries one target and is closed in full with a plain
`DELETE /positions/{dealId}` — no size field, no ambiguity. That sidesteps the
partial-close uncertainty below entirely, which makes this the more robust
model on this broker if your account can run hedged.

### Break-even

Keyed off price reaching TP1, **not** off the close succeeding. If the close is
rejected the trade still gets de-risked. The stop goes to the entry price
exactly (`breakeven_offset_r: 0.0`). In `three_deals` mode every leg still open
moves, so once TP1 trades the whole basket is risk-free.

### Entries against the bias

If you buy while the plan reads bearish, the bot rebuilds the levels for the
side you are actually on before adopting the position. The bias is still
reported honestly and the adoption message flags the disagreement — but the
stop belongs below your entry on a long, whatever the analysis thinks of it.

### Trailing stop

For a long (mirrored for a short):

```
chandelier = highest_high_since_entry − k × ATR
structure  = last_confirmed_swing_low − 0.25 × ATR
candidate  = max(chandelier, structure)     # the tighter of the two
```

`k` comes from the trend: 2.5 when ADX ≥ 25, 3.0 when ADX ≥ 18, and no trail at
all when the trend is weak. Four guards apply to every candidate:

1. **Ratchet only** — it must improve on the current stop by at least 0.1×ATR.
2. **Never below break-even** once the trade is risk-free.
3. **Never inside the broker's minimum stop distance** from the current price.
4. **One modification per cycle** — a break-even and a trail in the same tick
   collapse into whichever stop is better.

```
entry 3412.4 | TP1 hit -> SL = BE 3412.4
bar 14:00  HH 3438.2  ATR 6.1  ADX 31 (strong)  k=2.5
   chandelier 3423.0 | swing low 3419.8  ->  SL 3423.0 (raised)
bar 14:15  HH 3441.0  ADX 24 (cooling)   k=3.0
   chandelier 3422.7 < current SL        ->  hold 3423.0
```

### Partial closes on Capital.com

Capital.com's REST API does not publicly document a partial `DELETE
/positions/{dealId}`, so the bot probes at startup instead of assuming:

```
[startup] capability probe: partial_close
  -> account hedgingMode = False
  -> DELETE {dealId} with size ... error.position.notfound (body accepted)
  -> netting account: offset deal reduces the position deterministically
  [ok] partial close strategy = NETTING_OFFSET
```

**Netting-offset is preferred whenever the account nets.** An
opposite-direction deal of exactly the partial size can only ever *reduce* the
position. A `DELETE` whose `size` field is silently ignored closes the whole
thing — turning a partial take-profit into a full exit and throwing away the
runner. `DELETE_WITH_SIZE` is used only on hedging accounts (where an offset
would open a second position) or when you pin it in config.

Either way, every partial is **verified**: the position is re-read and its size
compared against what was expected. If the whole position vanished when a
remainder was due, partial closes are disabled on the spot and you are told.

### Surviving API drops

- Connection resets, timeouts, 429 and 5xx are retried with exponential backoff
  and jitter; a 429's `Retry-After` overrides the computed delay.
- An expired session token is **not** retried — it re-authenticates once and
  replays, which is the only thing that can actually work, and it does not count
  against the circuit breaker.
- 4xx rejections are permanent: they are not hammered, they mark the trade
  `ERROR` and they raise a loud alert, because a stop that did not move is risk
  you think is gone.
- After five consecutive transport failures the circuit opens and the bot enters
  degraded mode: it holds all state and takes **no** decisions until the
  connection recovers. Your broker-side stop is still in place.
- Market data has a staleness guard — decisions are never made on a quote older
  than `max_quote_age_seconds`.

### Crash safety

Every broker mutation is claimed in a SQLite action journal under an idempotency
key (`{dealId}:partial:TP1`, `{dealId}:breakeven`, …) before it is sent:

- A retryable failure **releases** the claim, so the next cycle re-evaluates from
  the real position state.
- A success **completes** it, so the same rung can never fire twice.
- Anything left in flight when the process died is released at startup and
  re-derived from the broker, which is always the source of truth for size and
  stop level.

A netting-offset close can hand the remainder a new deal id; the bot detects
that and re-maps the trade rather than reporting it closed.

---

## Layout

```
tmbot/
  config.py            YAML behaviour + environment secrets
  models.py            Direction, Candle, Quote, MarketRules, TradePlan, ManagedTrade
  store.py             SQLite: plans, trades, action journal, events
  broker/
    base.py            BrokerAdapter interface + PartialCloseStrategy
    capital.py         Capital.com REST: session, retry, re-auth, probe
    paper.py           In-memory netting broker for tests and offline runs
  analysis/
    indicators.py      EMA/SMA/RSI/ATR/ADX/MACD/swings, pure Python
    levels.py          Zone clustering -> TP1/TP2/TP3 + SL
    bias.py            Weighted technical read
    news.py            Marketaux / Finnhub / none
    fundamental.py     Claude synthesis with a lexicon fallback
    report.py          Report assembly and rendering
  manage/
    rules.py           Pure decisions: ladder or leg exit, break-even, trail, extension
    engine.py          Execution with idempotency and verification
    supervisor.py      Poll loop, adoption, schedules, commands
  notify/              Console, Telegram (with command polling), fan-out
  cli.py
tests/                 99 tests, no network
```

The split that matters: `manage/rules.py` is pure. It takes a trade and a market
snapshot and returns a list of decisions. Every scenario in the test-suite —
gapping through two rungs at once, a stop that would loosen, an indivisible
position size, a cooling trend — is a snapshot in and a decision list out, with
no broker involved.

## Tests

```bash
python -m unittest discover -s tests -t .
```

---

## Limits worth knowing

- **Capital.com's public API docs were unreachable from the build environment**
  (blocked by egress policy), so the endpoint shapes come from published client
  libraries and the IG-family conventions Capital.com follows. Run `probe` and
  `account` against your demo account first — they exercise the session,
  accounts, preferences, markets and positions endpoints, and will surface any
  mismatch immediately.
- The bot manages positions; it does not size them. Risk per trade is yours.
- Trailing and partials need the daemon running. Your broker-side stop and final
  target stay in place if it stops, but the ladder does not advance.
- `NEUTRAL` plans are marked advisory-only: levels are still published and a
  position will still be managed, but nothing in the report argues for taking
  the trade.
