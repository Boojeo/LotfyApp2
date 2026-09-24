"""Command line entry points."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from typing import List, Optional

from . import config as config_module
from .analysis.report import ReportBuilder, render_markdown, render_text
from .broker.capital import CapitalComBroker, explain
from .config import Config
from .errors import AuthError, PermanentError, TmbotError
from .manage.supervisor import Supervisor
from .notify.base import ConsoleNotifier, MultiNotifier, Notifier
from .store import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tmbot",
        description=(
            "Semi-automated trade manager for Capital.com: you open the trade, "
            "it runs the exits."
        ),
    )
    parser.add_argument(
        "--env", choices=("demo", "live"), required=True,
        help="which Capital.com environment to connect to. Deliberately has no "
             "default so a live account is never touched by accident.",
    )
    parser.add_argument("--config", help="path to a YAML or JSON config file")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ...")
    parser.add_argument(
        "--lang", choices=("en", "ar"),
        help="display language for alerts and reports (default: from config)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="evaluate and log every decision but send no order modifications",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="start the management daemon")

    report = sub.add_parser("report", help="build the daily report for one epic or the watchlist")
    report.add_argument("epic", nargs="?", help="epic to analyse (default: the whole watchlist)")
    report.add_argument("--markdown", action="store_true", help="print the full markdown report")

    markets = sub.add_parser(
        "markets", help="search for an instrument to find the epic name to use"
    )
    markets.add_argument("term", help="part of the instrument name, e.g. gold")

    hedging = sub.add_parser(
        "hedging",
        help="show or change hedging mode (the three-deal exit model needs it on)",
    )
    hedging.add_argument(
        "state", nargs="?", choices=("on", "off"),
        help="omit to just show the current setting",
    )

    journal = sub.add_parser(
        "journal", help="win rate and R per instrument from recorded fills"
    )
    journal.add_argument("days", nargs="?", type=int, default=30,
                         help="how far back to look (default 30)")

    sub.add_parser("status", help="show managed positions and ladder state")
    sub.add_parser("positions", help="list raw open positions at the broker")
    sub.add_parser(
        "doctor",
        help="work out which account a key belongs to and why a login is refused",
    )
    sub.add_parser(
        "envs",
        help="show which environments are configured and where each stores data",
    )
    sub.add_parser(
        "check",
        help="read back your settings without connecting to anything -- "
             "run this after editing config.yaml",
    )
    sub.add_parser("probe", help="run the partial-close capability probe and exit")
    sub.add_parser("account", help="show the connected account")
    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_config(args: argparse.Namespace) -> Config:
    # These only read settings back, so they must work even when something is
    # missing -- that is exactly when you need to look.
    inspecting = args.command in ("envs", "check", "doctor")
    config = config_module.load(args.config)
    config.broker.environment = args.env
    config_module.resolve_environment_secrets(config)
    if args.dry_run:
        config.dry_run = True
    if args.log_level:
        config.log_level = args.log_level
    if args.lang:
        config.language = args.lang
    config.validate(connecting=not inspecting)
    return config


def _build_notifier(config: Config, store: Store) -> Notifier:
    notifiers: List[Notifier] = [ConsoleNotifier()]
    if config.telegram.enabled:
        from .notify.telegram import TelegramNotifier
        notifiers.append(TelegramNotifier(config.telegram, store))
    return MultiNotifier(notifiers)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _load_config(args)
    except TmbotError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    _configure_logging(config.log_level)
    log = logging.getLogger("tmbot")
    # Printed before any connection is attempted, so it must not claim one.
    if (config.broker.environment == "live" and not config.dry_run
            and args.command not in ("envs", "check", "doctor", "journal")):
        log.warning("LIVE environment selected -- real orders can be modified")

    store = Store(config.resolved_database)
    broker = CapitalComBroker(config.broker)
    notifier = _build_notifier(config, store)

    try:
        if args.command == "run":
            supervisor = Supervisor(broker, store, config, notifier)

            def handle_signal(signum, _frame):
                log.info("signal %s received; shutting down", signum)
                supervisor.stop()

            signal.signal(signal.SIGINT, handle_signal)
            signal.signal(signal.SIGTERM, handle_signal)
            supervisor.run()
            return 0

        if args.command == "doctor":
            import copy

            account = config.broker.active
            chosen = config.broker.environment
            key_tail = account.api_key[-4:] if account.api_key else "(not set)"
            print(f"Trying your {chosen.upper()} key (...{key_tail}, from "
                  f"CAPITAL_{chosen.upper()}_API_KEY) on each Capital.com server.")
            print(f"  login  {account.identifier}")
            print()
            print("This test knocks on BOTH servers on purpose. Normal commands only")
            print(f"ever use the {chosen} server. Keys belong to your login, so one key")
            print("can open any account the API supports on either server.")
            print()
            if not account.configured:
                print(f"Nothing to test: the {chosen} credentials are incomplete.")
                return 1

            results = {}
            for host in ("demo", "live"):
                probe = copy.deepcopy(config.broker)
                probe.environment = host
                probe.demo = probe.live = account
                probe.retry_attempts = 1
                tester = CapitalComBroker(probe)
                try:
                    tester.connect()
                    summary = tester.account_summary()
                    results[host] = ("ok", str(summary.get("accountId", "?")))
                    print(f"  {host} server  accepted -> your {host} account "
                          f"{results[host][1]}")
                except (AuthError, PermanentError) as exc:
                    # Capital.com answered and said no -- the useful case.
                    # Keep the table row to the code; the verdict below explains it.
                    detail = str(exc).split(" -> ")[-1].split(" (")[0].strip()
                    results[host] = ("rejected", detail or type(exc).__name__)
                    print(f"  {host} server  refused  -> {detail}")
                except Exception as exc:
                    # Never reached the server, so this says nothing about the key.
                    detail = type(exc).__name__
                    results[host] = ("unreachable", detail)
                    print(f"  {host} server  no answer -- check your internet")
                finally:
                    try:
                        tester.close()
                    except Exception:
                        pass

            print()
            if results[chosen][0] == "ok":
                print(f"These credentials work on {chosen}. Nothing to fix.")
                return 0
            if "unreachable" in (results["demo"][0], results["live"][0]):
                print("Capital.com could not be reached, so this test proves nothing")
                print("about your key. Check your internet and run it again.")
                return 1
            refusal = results[chosen][1].lower()
            other = "demo" if chosen == "live" else "live"
            if "null.accountid" in refusal:
                print("Your key and password are fine" + (
                    f" -- they log in on {other}." if results[other][0] == "ok" else "."
                ))
                print(f"But the {chosen} server has no account the API is allowed to")
                print("trade for this login. Capital.com only supports CFD and")
                print("spread-bet accounts through the API; MT4, MT5 and some other")
                print("account types are excluded.")
                print()
                print("Generating another key will NOT fix this -- keys belong to")
                print("your login, not to an account. Instead, in Capital.com open")
                print(f"Settings -> My accounts with {chosen.title()} selected, and check the")
                print("account type. You need a CFD (or spread-bet) account there.")
                print("If it is already CFD, send Capital.com support this exact")
                print("error: error.null.accountId on POST /api/v1/session.")
            elif "invalid.details" in refusal:
                print("Capital.com rejected the email, key or password. The")
                print("password is the custom one you set when creating the API")
                print("key, not your login password.")
            else:
                meaning = explain(refusal)
                print(f"Refused on {chosen}: {results[chosen][1]}")
                if meaning:
                    print(meaning[0].upper() + meaning[1:] + ".")
            return 1

        if args.command == "envs":
            settings = config.broker
            print(f"selected      {settings.environment}")
            print()
            for name in ("demo", "live"):
                account = getattr(settings, name)
                shared = bool(
                    settings.api_key and settings.identifier and settings.password
                )
                ready = account.configured or shared
                if account.configured:
                    source = f"CAPITAL_{name.upper()}_*"
                elif shared:
                    # Capital.com issues separate keys per account, so a shared
                    # key is almost always the demo one. Saying "ready" without
                    # that caveat invites a confusing 401 later.
                    source = (
                        "CAPITAL_* (shared -- likely a demo key; live needs its own)"
                        if name == "live" else "CAPITAL_* (shared)"
                    )
                else:
                    source = "not set"
                marker = "->" if name == settings.environment else "  "
                gate = ""
                if name == "live":
                    gate = "  [live_enabled: {}]".format(
                        "yes" if settings.live_enabled else "NO -- live refused"
                    )
                state = "ready" if account.configured else (
                    "unsure" if ready else "missing"
                )
                print(f"{marker} {name:<5} {state:<8} "
                      f"{source}{gate}")
                environment_config = Config()
                environment_config.database = config.database
                environment_config.report = config.report
                environment_config.broker.environment = name
                print(f"      database  {environment_config.resolved_database}")
                print(f"      reports   {environment_config.resolved_report_dir}")
                if account.account_id:
                    print(f"      sub-account {account.account_id}")
            print()
            print("Demo and live never share a database, so practice results "
                  "cannot leak into your live journal.")
            print("Switch with --env demo | --env live. Both can run at once, "
                  "in separate windows.")
            return 0

        if args.command == "journal":
            from .analysis import journal as journal_module
            from .i18n import Translator
            print(journal_module.render(
                journal_module.build(store, max(1, args.days)),
                Translator(config.language),
            ))
            return 0

        if args.command == "check":
            management = config.management
            print(f"config file   {args.config or '(defaults, no file given)'}")
            print(f"environment   {config.broker.environment}")
            print(f"database      {config.resolved_database}")
            print(f"reports       {config.resolved_report_dir}")
            print()
            print(f"exit model    {management.exit_model}")
            if management.exit_model == "three_deals":
                order = " then ".join(management.leg_targets)
                print(f"              you open {len(management.leg_targets)} deals; "
                      f"they are closed whole at {order}")
                print(f"              deals opened within "
                      f"{management.group_window_minutes:.0f} minutes count as one basket")
            else:
                slices = ", ".join(
                    f"{step.fraction:.0%} at {step.stage}" for step in management.ladder
                )
                print(f"              you open 1 deal; it is cut {slices}, rest at TP3")
            print(f"              stop to entry at {management.breakeven_stage}, "
                  f"trailing after {management.trail_after_stage}")
            print()
            watchlist = config.analysis.watchlist
            print(f"watchlist     {len(watchlist)} instrument(s)")
            for item in watchlist:
                print(f"  {item.epic:<16} {item.display or item.epic}")
                if item.news_query:
                    print(f"  {'':<16}   news: {item.news_query}")
            if not watchlist:
                print("  (empty -- reports and plans have nothing to work on)")
            analysis = config.analysis
            print(f"analysis      structure on {analysis.structure_timeframe}, "
                  f"levels and bias on {analysis.entry_timeframe}, "
                  f"ATR({analysis.atr_period})")
            print(f"              stop between {analysis.min_stop_atr} and "
                  f"{analysis.max_stop_atr} ATR, TP1 at least "
                  f"{analysis.min_reward_risk}R")
            print(f"management    decisions on "
                  f"{management.management_timeframe} every "
                  f"{management.poll_seconds:.0f}s")
            print()
            from datetime import datetime
            from .config import resolve_timezone
            zone = resolve_timezone(config.report.timezone)
            local = datetime.now(zone)
            print(f"time now      {local:%H:%M} {config.report.timezone} "
                  f"({datetime.utcnow():%H:%M} UTC)")
            print(f"daily report  {config.report.daily_time} {config.report.timezone}, "
                  f"refreshed every {config.report.intraday_refresh_hours:.0f}h")
            print(f"language      {config.language} "
                  f"({'right-to-left' if config.language == 'ar' else 'left-to-right'})")
            print(f"telegram      {'on' if config.telegram.enabled else 'off'}")
            print(f"news          {config.news.provider}")
            print(f"claude        {'on' if config.llm.enabled else 'off'} "
                  f"({config.llm.model})")
            print()
            print("Settings read cleanly. Nothing was sent to the broker.")
            return 0

        broker.connect()

        if args.command == "probe":
            needed = config.management.exit_model == "partial_close"
            probe = broker.probe_partial_close(needed=needed)
            print(probe.render())
            if not needed:
                print(
                    f"\nexit_model is {config.management.exit_model}: you open three "
                    "deals and each is closed in full, so nothing above blocks you."
                )
                print(
                    "hedging mode is ON -- your three deals will stay separate."
                    if probe.hedging_mode
                    else "WARNING: hedging mode is OFF -- your three deals will be "
                         "merged into one. Run: tmbot hedging on"
                )
            return 1 if probe.blocking else 0

        if args.command == "account":
            account = broker.account_summary()
            print(f"account {account.get('accountId')} ({account.get('accountName', '')})")
            print(f"currency {account.get('currency')}  balance {account.get('balance')}")
            print(f"hedging mode: {broker.hedging_mode()}")
            return 0

        if args.command == "hedging":
            current = broker.hedging_mode()
            if args.state is None:
                print(f"hedging mode is currently: {current}")
                print(
                    "three_deals needs this ON; partial_close works either way."
                    if current is not True
                    else "three deals on one instrument will stay separate positions."
                )
                return 0
            wanted = args.state == "on"
            if current is wanted:
                print(f"hedging mode is already {args.state}; nothing to do")
                return 0
            broker.set_hedging_mode(wanted)
            confirmed = broker.hedging_mode()
            print(f"hedging mode: {current} -> {confirmed}")
            if confirmed is not wanted:
                print(
                    "the account did not accept the change -- open positions or "
                    "orders usually have to be closed first",
                    file=sys.stderr,
                )
                return 1
            return 0

        if args.command == "markets":
            found = broker.search_markets(args.term)
            if not found:
                print(f"nothing matched {args.term!r}")
                return 1
            print(f"{'EPIC':<20} {'INSTRUMENT':<38} STATUS")
            for market in found[:40]:
                print(
                    f"{market.get('epic', ''):<20} "
                    f"{market.get('instrumentName', '')[:38]:<38} "
                    f"{market.get('marketStatus', '')}"
                )
            print("\nUse the EPIC value in your config watchlist and in report/plan commands.")
            return 0

        if args.command == "positions":
            positions = broker.positions()
            if not positions:
                print("no open positions")
            for position in positions:
                print(
                    f"{position.deal_id}  {position.epic:12s} {position.direction.value:4s} "
                    f"{position.size:<8} @ {position.entry_price}  "
                    f"SL {position.stop_level}  TP {position.profit_level}"
                )
            return 0

        if args.command == "status":
            supervisor = Supervisor(broker, store, config, notifier)
            print(supervisor.status_text())
            return 0

        if args.command == "report":
            reporter = ReportBuilder(broker, config)
            epics = [args.epic.upper()] if args.epic else [
                item.epic for item in config.analysis.watchlist
            ]
            if not epics:
                print("no epic given and the watchlist is empty", file=sys.stderr)
                return 2
            failures = 0
            for epic in epics:
                try:
                    plan = reporter.build(epic)
                except Exception as exc:
                    print(f"{epic}: {exc}", file=sys.stderr)
                    failures += 1
                    continue
                store.save_plan(plan)
                print(render_markdown(plan) if args.markdown else render_text(plan))
                print()
            return 1 if failures else 0

        return 2
    except TmbotError as exc:
        log.error("%s", exc)
        return 1
    finally:
        broker.close()
        store.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
