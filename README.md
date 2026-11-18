# tmbot — semi-automated trade manager for Capital.com and MetaTrader 5

You take the entry. The bot takes everything after it.

By default it never opens a position (see "Automatic trading" for the optional,
off-by-default mode that does). It watches your Capital.com or MetaTrader 5 (Exness)
account, notices when you
have manually entered a trade, asks you to confirm, and then runs the exit plan:
scaled partial closes, an automatic move to break-even, and a trailing stop that
tightens or holds with the trend. Alongside that it publishes a daily
analytical report per instrument — a direction bias, three targets and one stop
derived from support/resistance and liquidity zones.

---

## Languages

English and Arabic, switched live with `/lang ar` / `/lang en` or `--lang` on
the command line. Two rules make the switch safe with positions open:

- **Nothing stored is translated.** `BUY`, `MANAGING`, `TP1` stay English in
  SQLite and on the wire. Switching re-renders text; it never rewrites a record.
- **Every interpolated value is bidi-isolated in Arabic.** A price dropped raw
  into right-to-left text renders scrambled — `SL 3398.10` can come out with its
  digits reordered. Each value is wrapped in FSI/PDI so it lays out on its own.

Log lines stay English deliberately, so they stay greppable and can be pasted
into a search or an issue whatever language the UI is in. Each decision carries
both forms: a translated `reason_key` for you and an English `reason` for the
log and the action journal.

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
- An **annotated chart** with every report: candles in plain ink, each target
  and the stop drawn and labelled with what it pays in R, liquidity zones
  shaded, and the risk between entry and stop filled in. Colour is reserved for
  decisions — the conventional take-profit green sits 4.1 ΔE from stop red under
  deuteranopia, so the teal used instead scores 9.9, and every level carries a
  text label and its own line style so nothing depends on colour alone.
- Runs on a schedule for a watchlist, refreshes intraday, and tells you when
  the bias flips or a level is invalidated.

**2. Automated trade management (post-entry)**

Two exit models, chosen with `management.exit_model`. **The default is
`three_deals`.**

`three_deals` (default) — you open **three** deals and each is closed whole at its own target:

| Trigger | Action |
| --- | --- |
| TP1 trades | Close deal 1 entirely; move deals 2 and 3 to the **exact entry price** |
| TP2 trades | Close deal 2 entirely |
| TP3 trades (no extension left) | Close deal 3 |
| Trend strong, price near TP3 | Extend TP3 for deal 3 only |
| Trend strong / moderate | Trail deal 3 only — deals 1 and 2 must not be trailed out of their targets |

Deals on the same instrument and side opened within `group_window_minutes` of
each other are recognised as one basket, and a single `/confirm` adopts all of
them. A deal that fills late joins the basket without asking again, and while a
basket is short of its legs the adoption message says how many are still
outstanding — a lone deal in this mode closes at TP1 and stops there.

> **`three_deals` needs hedging mode switched on at Capital.com.** With it off
> the broker nets the three deals into a single position and the legs cannot be
> closed separately. The bot checks at startup and says so rather than
> discovering it mid-trade. If your account cannot run hedged, set
> `exit_model: partial_close` instead.

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

---

## Automatic trading and the 30-day plan

Both are optional. Neither changes how the bot manages a trade you opened.

### Automatic trading (off by default)

Automatic trading lets the bot open the trades its own analysis approves, with
the stop and target attached at the broker. It is **off** until you switch it on
in three places:

1. `auto.enabled: true` in `config.yaml` (it also needs your Telegram token and
   chat id, because it is only ever controlled from your own chat), then restart.
2. `/auto_on` in Telegram.
3. On a **real-money account** only: `auto.allow_live: true` as well, and then a
   one-time code that `/auto_on` sends you and you type back
   (`/auto_on 4821`) within `auto.confirm_seconds`. A live account always starts
   a session OFF; it never resumes by itself after a restart.

Start on the demo account. The bot refuses to turn on if the risk per trade is
not 1, 2 or 3 %, if MetaTrader 5's Algo Trading button is off, or if the account
nets positions (three separate deals need hedging).

**Commands** (only your configured Telegram chat can send them)

| Command | What it does |
| --- | --- |
| `/auto_on` | Turn automatic entries on (live: asks for the code). |
| `/auto_off` | Stop new entries immediately. |
| `/auto_status` | ON or OFF, risk, instruments, session, open deals, loss-limit use, latest decisions. |
| `/risk [1\|2\|3]` | Show or set the risk per trade, as % of equity. Remembered. |
| `/daily_report` | Today's equity, results, risk and the automatic mode's counts. |
| `/plan_status` | The 30-day plan's status (same as `/plan30`). |
| `/panic` | Turn automatic trading OFF **and close every managed trade**; asks for a code first. |
| `/msg_auto`, `/short` | Message length only (this used to be `/auto`). `/auto` now just points you here. |

**What happens when it is turned OFF.** New entries stop at once. Positions that
are already open are *not* closed: the normal engine keeps managing them (stops,
targets, trailing, break-even, reversal alerts), and you can still `/close <id>`.
The only thing that closes everything is `/panic`, after a confirmation. The same
applies when the daily loss limit is reached: new entries stop, management
continues.

**What it checks, in this order, for one instrument every `auto.scan_minutes`:**

1. instrument permitted (`auto.instruments`, else the watchlist)
2. no unsettled earlier order; trading session (`session_start`–`session_end`
   UTC, on `weekdays`)
3. daily loss limit not reached (`daily_loss_limit_percent` of the day's starting
   equity; equity is read from the account, so deposits and withdrawals during
   the day distort it)
4. no open position on the instrument; open deals below `max_open_positions`
5. the full analysis (technicals, news, entry quality, risk sizing)
6. verdict **APPROVED**; signal strength at least `min_signal_strength`; higher
   timeframe **ALIGNED**; a clear direction; none of the "unsure or risky"
   reasons that also trigger the long report; risk sized inside the selected
   limit for the selected risk %
7. market tradeable; not in the cool-down for that instrument and side

Anything else is rejected, logged with its reason (`auto_reject` events, shown in
`/auto_status`) and, only when a good signal was stopped by a guard, messaged
once an hour. Every order request, acceptance, failure and state change is also
logged (`auto_order`, `auto_open`, `auto_error`, `auto_state`).

**Order safety.** The stop is the plan's technical stop; it is never widened to
fit a lot size, and the risk % is never raised to make a trade pass. If even the
broker's smallest lot loses more than the limit, the trade is skipped and the
reason says so. Duplicate protection writes its markers *before* an order is
sent, so a crash or a timeout can only block a repeat: after a failure the bot
does not retry, it reads the account to see what really opened, and the trades
that did open are managed normally. A basket that fails part-way keeps the deals
that opened and stops there. With `dry_run: true` nothing is sent.

**Limits worth knowing.** On MetaTrader 5 the order function is new and has been
tested against a stand-in for the MetaTrader5 package, not a live terminal: run
it on a demo account and watch the first trades. Entries are market orders at
the quoted price, so the fill can differ; the stop and target are attached to the
order. Position sizing counts half the spread, `report.slippage` and
`report.commission_per_lot`; swaps and financing are not predicted.

### Risk per trade

The default is 1% of current equity. `/risk 1`, `/risk 2` or `/risk 3` changes
it; 2% and 3% are higher-risk settings that apply only because you chose them,
and the bot says so when you do. The lot size comes from equity, the stop
distance, the contract value per point, the broker's minimum, step and maximum
lot, half the spread, `report.slippage` and `report.commission_per_lot`. It
reduces the number of deals first; if the broker's minimum lot would still lose
more than the limit, the trade is rejected, never forced to 0.01 lot.

### The 30-day plan

`/plan30 start [capital]` begins a 30-day performance record from $100. It is
tracking and reporting, not a target: nothing it shows ever makes the bot take a
trade or more risk. Two ledgers are kept apart:

* **Theoretical**: starts at the capital and compounds 25 % a day. This is
  arithmetic (about $80,800 after 30 days), not a forecast.
* **Actual**: starts at the capital; each day starts where the previous day
  really ended. Built only from the bot's recorded fills (broker-booked profit,
  commission and swap when the broker reports them; otherwise price move × lots
  × contract value, marked *estimated*; otherwise the bot asks you for the
  number with `/plan30 result` and holds later days).

**Rounding.** Money is rounded to cents, half away from zero, after every step
of the theoretical curve (`ROUND(x, 2)` in Excel). Python's own `round` would
differ by a cent on exact half-cents, so the bot rounds the same way a
spreadsheet does and the exported file matches it.

Each finished day is reviewed once and then preserved: day, date, actual start,
theoretical start, target profit, theoretical end, actual net, actual end, %
return, trades, wins, losses, costs and the worst drop (closed trades only), rule
compliance, and a continue / reduce risk / pause suggestion. `/plan30 export`
sends an `.xlsx` with live formulas. The daily loss limit warns once when
reached; stopping is up to you (or to automatic mode, which stops entering).

## Entry range, tested odds, exit advice and broker-side protection

These are additions: nothing in the analysis, the targets, the stop, the lot
sizing or the exit rules was changed to make room for them.

### Entry range

A plan has one reference price, but the fill you get is rarely exactly that. The
plan now also states how far from it an entry can be before it breaks a rule the
bot **already enforces** (it adds no rule):

* **Worst entry:** the price where, with the lots the plan advised, the loss at
  the stop reaches the risk limit (costs included), or where TP1 stops paying
  `analysis.min_reward_risk` times the risk, whichever comes first.
* **Best entry:** the price where the stop would be closer than
  `analysis.min_stop_atr` ATRs.
* **Preferred entry:** the reference price, the one the lots were sized on.

For five prices across the range (plus the preferred one) the full report shows
the loss at the stop, the profit at each target for the deal that closes there,
the total and the reward/risk, using the plan's own lots, `report.slippage`,
`report.commission_per_lot` and half the spread. The lots never change: a worse
entry means a bigger loss at the same stop, never bigger lots, and the stop and
targets never move.

The range is **no longer valid** when: price leaves it; TP1 is already passed or
the stop touched; a wider spread pushes the loss over the risk limit or TP1 below
the minimum reward/risk; the plan is older than `report.intraday_refresh_hours`
(4 h if that is 0); or a newer plan points the other way. Automatic trading
checks this at the price an order would fill at, and refuses an entry whose range
is no longer valid or could not be worked out.

### Tested odds

No probability in this feature is a constant. `analysis/odds.py` takes every bar
of the instrument's history as a hypothetical entry in the trade's direction, puts
the stop and each target the same distance away *in ATRs* (of that bar), follows
the price for `exit_advice.horizon_bars`, and records which came first. A bar that
touches both counts as the stop. The share that reached each target first is its
**tested reach rate**, the share that hit the stop first its
**reversal-before-target rate**, shown with a Wilson 95 % interval and the number
of test trades. If the trend at the test entry agrees with the trade's direction
(EMA20 vs EMA50) only those are used when there are enough, otherwise all are, and
the message says which. Below `exit_advice.min_samples` test trades no number is
shown at all.

What it is not: a forecast. It describes how often that *geometry* worked on this
instrument's recent past; test trades overlap in time, so the interval is a little
optimistic. The older "heuristic loss-risk score" in the report is a different,
fixed-rule score and is labelled as such.

### Exit advice (advice only)

For every managed deal the bot works out a recommendation (**HOLD, PARTIAL CLOSE,
MOVE STOP LOSS or CLOSE**) and the reasons, and messages you when it changes:

1. a confirmed reversal (the engine's own detector) while in profit → CLOSE;
2. otherwise, for each target still ahead, the expected value of holding to it in
   units of the risk taken (R) = P(target first) × R(target) + P(stop first) ×
   R(stop) + P(neither) × R(now), using the tested rates. If none beats the R
   already in hand, take it: CLOSE (PARTIAL CLOSE in the partial-close model);
3. the *most realistic target* is the one with the best expected value, which is
   **not** assumed to be TP3 (e.g. "TP2 is worth +1.4R, TP3 +0.9R");
4. fading momentum (MACD histogram shrinking, DI spread narrowing) with at least
   one reversal signal firing, while in profit → MOVE STOP to an ATR lock (the
   reversal failsafe's own `tighten_atr`), only if it improves the stop and the
   broker accepts that distance;
5. otherwise HOLD, with the evidence listed (trend strength, ADX, momentum,
   reversal signals, distance to each target).

Without enough history there is no number and the advice says so.

Nothing executes by itself, so the broker-side targets, TP3 extension, trailing
stop and reversal failsafe all keep working exactly as before. You decide:

| Command | What it does |
| --- | --- |
| `/exit [id]` | Show the advice for one deal or all of them. |
| `/do <id>` | Apply the advice for that deal, once, through the normal close / stop path. Refused if it is older than `exit_advice.stale_minutes` or the market has changed it since you read it. |
| `/skip <id>` | Cancel the advice for that deal until it changes. |
| `/targets <id> 2` | **T1–T2 only:** pull the basket's final deal in to TP2 and set that take-profit on the broker; TP3 extension is switched off for it. |
| `/targets <id> 3` | Back to **T1–T2–T3**: the plan's TP3 is restored on the broker. |

### Broker-side protection

Two kinds of protection are kept apart in every message:

* **Broker-side** (`/protect`): the stop-loss and take-profit stored on the
  broker's server. They stay active if the app, the computer or the internet goes
  down. Whatever stop and target the app last set stay there.
* **Dynamic**: what only works while the app runs: the move to break-even, the
  halfway risk cut, the trailing stop, the reversal failsafe and the TP3
  extension. If the app is off they simply do not happen; nothing is closed.

`protection` adds a check that the first kind is really there: after every change
(after a short grace period), every `protection.audit_seconds` (2.5 s by default;
it reuses the positions already fetched each cycle, so it adds no broker requests),
**at start-up and
after a reconnect before monitoring resumes** (each trade is re-read from the
broker and a summary says what was verified). A stop that is missing or looser
than the one the bot last set is put back and confirmed on the server; a tighter
stop is adopted, not fought; a missing target is put back; a target that differs
is only reported, because it may be your own edit. Each repair has its own
idempotency key, waits `repair_cooldown_seconds` between attempts and stops after
`max_repairs`, so it cannot send duplicates or fight the broker. While `/pause` is
on, or in `dry_run`, it only alerts. A failed repair never marks the trade ERROR.

`/protect [id|verify]` shows, per deal: the stop (what it locks or risks, in R and
money, and whether the broker confirms it), the target, what needs the app running,
an ATR reference (where a trailing stop or a 1-ATR lock would sit now, **not
applied**) and the loss if the stop is hit against the risk limit.

Not done on purpose: an earlier, extra profit-lock ratchet. The existing
break-even, halfway and trailing schedule is unchanged; the ATR reference and the
MOVE STOP advice only show where a tighter stop would go.

## Install

```bash
git clone https://github.com/Boojeo/LotfyApp2.git && cd LotfyApp2
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # credentials
cp config.example.yaml config.yaml
```

Fill in `.env` with a Capital.com API key (Settings → API integrations), or
with your MetaTrader 5 login if you use Exness — see
[Using Exness / MetaTrader 5](#using-exness--metatrader-5).

## Demo and live together

Both accounts are configured at once and chosen per run. Capital.com issues
separate API keys for each, so `.env` holds `CAPITAL_DEMO_*` and
`CAPITAL_LIVE_*` (a single `CAPITAL_*` set still works and is used for both).

```bash
python -m tmbot --env demo --config config.yaml envs     # what is configured, and where it stores data
python -m tmbot --env live --config config.yaml doctor  # why a login is being refused
python -m tmbot --env demo --config config.yaml run
python -m tmbot --env live --config config.yaml run
```

**Nothing is shared between them.** The database and the reports directory are
suffixed with the environment automatically — `tmbot-demo.sqlite3` and
`tmbot-live.sqlite3`. That is not a setting: one journal holding both would mix
practice results into your live record and destroy the only measure of whether
this works. Every alert is stamped `[DEMO]` or `[LIVE]` so an alert is never
ambiguous.

Both can run at the same time, in separate windows. If you do, give each its own
Telegram bot (`TELEGRAM_DEMO_BOT_TOKEN` / `TELEGRAM_LIVE_BOT_TOKEN`) — two
pollers on one token steal each other's updates, and commands would land in
whichever process grabbed them first.

`doctor` tries the selected environment's credentials against **both**
Capital.com hosts and says what follows. API keys belong to your *login*, not to
an account, so the same key authenticates on both servers; what differs is which
account the server can find for you. `error.null.accountId` therefore means the
key is fine but that server has **no account the API supports** — Capital.com
only allows CFD and spread-bet accounts, and excludes MT4, MT5 and some other
types. Regenerating keys cannot fix that; the account type has to change. A host
that never answers is reported as unreachable rather than blamed on the key.

Capital.com's documented error codes are explained inline wherever they surface,
so a refusal says what it means rather than just which code it was.

**Live takes two deliberate acts**: `broker.live_enabled: true` in the config
file *and* `--env live` on the command line. Either alone is refused, so no
single typo can point the bot at real money.

## Using Exness / MetaTrader 5

Set `broker.platform: mt5` and the bot talks to a MetaTrader 5 terminal on the
same Windows PC instead of Capital.com's API. Everything else is unchanged:
the same reports, the same exit rules, the same Telegram commands, the same
separate demo and live journals. The Capital.com connection stays available;
switching back is one line in the config.

How it connects: the `MetaTrader5` Python package does not reach the broker
itself. It drives the MT5 terminal program, and the terminal holds the
connection to Exness. So the terminal has to be installed, and this only runs
on Windows.

One-time setup:

1. Install **MetaTrader 5 from Exness** (Personal Area → Trading platforms)
   and log in to your account in it once, ticking *Save password*.
2. Click the **Algo Trading** button in the MT5 toolbar so it is green.
   Without it every stop move and close is refused (retcode 10027), and the
   bot says so at startup.
3. In `.env`, fill `MT5_DEMO_LOGIN`, `MT5_DEMO_PASSWORD`, `MT5_DEMO_SERVER`
   (and the `MT5_LIVE_*` ones for real money). These are the account number,
   the account's *trading* password and the server name, exactly as in the
   MT5 login window.
4. In `config.yaml`, set `broker.platform: mt5`.
5. Run `update.bat` (or `pip install -r requirements.txt`), which installs the
   `MetaTrader5` package.
6. Find your symbol names — they differ per broker and account type (Exness
   Standard accounts add an `m`: `XAUUSDm`):
   ```bash
   python -m tmbot --env demo --config config.yaml markets gold
   ```
   Copy the first column into your watchlist.
7. `python -m tmbot --env demo --config config.yaml doctor` walks through all
   of the above and says what is left to fix.

What is different on MT5:

- **Hedging is fixed per account.** MT5 accounts are either hedging or netting
  from the day they are opened; `hedging on` cannot change it. `three_deals`
  needs a hedging account (Exness MT5 accounts are hedging by default).
- **Partial closes are native.** A part of a position is closed by naming its
  ticket, so no opening order is ever sent — not even to probe.
- **The environment is verified.** `--env demo` on a real-money account, or
  `--env live` on a demo one, is refused at connect.
- **Exits are exact.** A position the broker closed while the bot was off is
  journalled at its real closing price from the MT5 deal history, not an
  estimate.
- **The terminal is the connection.** If it closes or restarts, the bot
  re-attaches on the next call; if it stays down, the bot degrades and keeps
  retrying like any other API drop. The broker-side stop is always on the
  position, so a dead terminal leaves the trade protected.
- **Server time.** MT5 stamps bars and positions in the broker's server time;
  `broker.mt5.server_utc_offset_hours` converts it (0 for Exness).

## Run

`--env` is mandatory and has no default, so a live account is never touched by
accident.

```bash
# Read your settings back without connecting to anything
python -m tmbot --env demo --config config.yaml check

# Check the connection and see how partial closes will be executed
python -m tmbot --env demo --config config.yaml probe

# Find the epic code for an instrument, and check hedging mode
python -m tmbot --env demo --config config.yaml markets gold
python -m tmbot --env demo --config config.yaml hedging
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
/now [symbol]    a full analysis on live prices this second: bias, style,
                 TP1-3/SL with distance, lot size, every indicator signal,
                 nearest support/resistance, open trades (no news/AI/chart)
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

Each deal rests at the broker with **its own** take-profit — deal 1 at TP1,
deal 2 at TP2, deal 3 at TP3 — so the targets hold even while the bot is off.
Deals are numbered by opening time (ticket as tie-breaker), never by the order
the broker lists them.

Each deal carries one target and is closed in full with a plain
`DELETE /positions/{dealId}` — no size field, no ambiguity. That sidesteps the
partial-close uncertainty below entirely, which makes this the more robust
model on this broker if your account can run hedged.

### Break-even

Keyed off price reaching TP1, **not** off the close succeeding. If the close is
rejected the trade still gets de-risked. The stop goes to the entry price
exactly (`breakeven_offset_r: 0.0`). In `three_deals` mode every leg still open
moves, so once TP1 trades the whole basket is risk-free.

### Halfway stop

Between entry and TP1 the stop would otherwise not move, and that is where
every loss is made. Once price has covered `risk_cut_at` of the way to TP1
(default half), the stop is pulled in so only `risk_cut_to` of the original
risk is left (default half): risking 10 points, price gets 5 of the way to
TP1 and turns — you lose 5, not 10. It happens once, only ever tightens, and
break-even at TP1 takes over from there. The trade-off: a trade that dips back
before going on to TP1 can now be stopped out for a small loss.

### Trend reversal failsafe

Four independent signals, checked on every cycle: a DI cross, a close beyond the
last confirmed swing, an EMA cross, and a momentum flip that is widening rather
than flat. `min_signals` of them must agree **and** keep agreeing for
`confirm_cycles` in a row — a single bar poking through a swing low is noise,
which is what the stop is already for. ADX gates the whole thing: a market with
no trend has no trend to reverse.

Detection is deterministic on purpose. A language model is seconds slow, answers
the same input differently twice, and cannot be backtested — none of which belong
on the path that decides whether to pull your stop in.

What it does on a confirmed reversal is bounded by the system's rules:

- `tighten` (default) pulls the stop to `tighten_atr` behind price. Still
  ratchet-only, still inside the broker's minimum distance, so it can only ever
  reduce risk.
- `close` exits the remaining size.
- `alert` touches nothing.

Below `min_r_to_act` it alerts and does nothing, because the only ways to
"recover" a losing position are widening the stop or adding to it, and both are
forbidden. You get told; the decision is yours. `/hold <id>` mutes the alerts
for one trade without changing how it is managed.

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

### How a decision is made

Unless automatic trading is on, the bot never opens a trade, so its "decision" is advice for the deal you
place, shown in every report and `/now`:

```
MARKET DATA -> TECHNICAL ENGINE -> DIRECTION -> ENTRY QUALITY (+ higher timeframe)
  -> LEVELS (SL from structure/ATR, TP1-3) -> HARD RISK LIMIT -> VERDICT
```

- **Direction** comes from two pooled categories — *trend* (EMA position,
  EMA cross, EMA slope, ADX direction) and *momentum* (MACD, RSI, momentum) —
  with one vote each (60/40). Correlated indicators no longer stack.
  The number shown is **signal strength**, not a probability of winning.
- **Entry quality** is separate: GOOD / WEAK / POOR, with the reasons —
  price stretched from its average, a level in the way before TP1, stretched
  RSI, TP1 pushed past structure, wide spread. The higher timeframe is read on
  its own: ALIGNED / PARTIAL / CONFLICT. A conflict lowers quality instead of
  being averaged into the score.
- **Hard risk limit** (`report.risk_percent`, default 1%): all deals
  together may lose at most that share of **equity** at the stop, spread
  included, using the broker's contract value, minimum, step and maximum lot.
  The stop is never moved to make size fit. If 3 deals at the broker minimum
  would be over the limit, 2 are advised (TP1 / TP3), then 1 (TP1); if even
  one is over, the verdict is **REJECTED — RISK LIMIT**. Before you
  `/confirm`, the bot shows what the deals you actually opened lose at their
  stops and warns loudly if that is over the limit.
- **Verdict**: risk first, then direction, then quality — APPROVED, CAUTION
  or REJECTED. Signal strength plays no part.

Every analysis is stored with all of the above (`plans` table) and every exit
with the broker's booked price, profit, commission, swap and deal number
(`fills`), so performance by symbol, style, strength band and verdict can be
measured once enough real trades exist.

### Trade style: how long a trade is planned for

With `style.mode: auto` the bot decides, per instrument and at every analysis,
how long the trade should be planned for, and builds the stop and the three
targets on the matching charts:

| Style | Charts (structure / levels / managed on) | Typical hold |
| --- | --- | --- |
| scalp | H1 / M15 / M5 | 1–5 h |
| intraday | H4 / H1 / M15 | 5–24 h |
| swing | D1 / H4 / H1 | 1–5 days |

The rule is fixed, not a guess: start from the fastest style in
`style.allowed`, and step up to a slower one only when that slower chart has a
strong trend (ADX at or above `intraday_min_adx` / `swing_min_adx`) pointing
the same way as the faster one. A ranging or disagreeing slower chart keeps the
trade quick. The report states the style and the reason.

A trade keeps the style it was **entered** under until it closes — a later
analysis picking another style never changes how a running trade is trailed.
The three deals of a basket always share one management chart.

**Lot size.** A slower style means wider targets *and* a wider stop. Every
report therefore says how many lots per deal keep a stop-out to
`report.risk_percent` of your balance (rounded down to the broker's lot step).
If even the minimum lot risks more than that, the report says so with the real
percentage. This is advice for the deal you place — unless automatic trading is on, the bot still never opens
one.

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

## Is it working?

`/journal [days]` (or `tmbot journal 30`) aggregates every recorded exit:

```
Performance, last 30 days (R = multiples of risk)

GOLD   3 trades   2W 1L   +1.25R   avg +0.42R
  TP1 2/3 (67%)   TP2 1/3 (33%)   TP3 1/3 (33%)

Total 5 trades  +0.75R  |  best GOLD  |  worst OIL_BRENT
Reversal fired on 1 trades (avg +0.50R) vs 4 without (avg +0.06R)
4 exit(s) estimated from the last quote, not observed
```

Everything is in **R** — what a trade returned as a multiple of what it risked —
because R is scale-free: 0.1 lots and 10 lots on the same idea score identically,
so the number measures the decisions rather than the position size.

Two deliberate omissions. It does not report account currency, because broker
fees, overnight charges and slippage are not captured and a figure that looks
like a P&L statement but isn't one is worse than none. And it does not claim the
reversal failsafe "saved" you anything — that needs a counterfactual nobody has,
so it reports the two populations side by side and lets you judge.

Exits the broker made (a stop-out, a target filled server-side) were never
observed, so their price is estimated from the last quote and the count of
estimates is printed. **The journal is written as trades close and cannot be
reconstructed afterwards** — it has to be running before the trades are, or that
period is simply unmeasured.

## Layout

```
tmbot/
  config.py            YAML behaviour + environment secrets
  i18n.py              English/Arabic catalogue, bidi isolation, term lookup
  models.py            Direction, Candle, Quote, MarketRules, TradePlan, ManagedTrade
  store.py             SQLite: plans, trades, action journal, events
  broker/
    base.py            BrokerAdapter interface + PartialCloseStrategy
    capital.py         Capital.com REST: session, retry, re-auth, probe
    mt5.py             MetaTrader 5 (Exness): terminal login, retcodes, native partials
    factory.py         Picks the adapter from broker.platform
    paper.py           In-memory netting broker for tests and offline runs
  analysis/
    chart.py           Annotated plan charts, light/dark, Arabic-shaped
    journal.py         Performance aggregation in R
    reversal.py        Four-signal trend-reversal detection (deterministic)
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
tests/                 200 tests, no network
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
- A closed market is left alone: the quote is a stale last price and any modify
  would be rejected, so no decisions are taken until it reopens. Reports still
  build over a weekend.
- Trailing and partials need the daemon running. Your broker-side stop and final
  target stay in place if it stops, but the ladder does not advance.
- `NEUTRAL` plans are marked advisory-only: levels are still published and a
  position will still be managed, but nothing in the report argues for taking
  the trade.
