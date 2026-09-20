"""Command line entry points."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from typing import List, Optional

from . import config as config_module
from .analysis.report import ReportBuilder, render_markdown, render_text
from .broker.capital import CapitalComBroker
from .config import Config
from .errors import TmbotError
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

    sub.add_parser("status", help="show managed positions and ladder state")
    sub.add_parser("positions", help="list raw open positions at the broker")
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
    config = config_module.load(args.config)
    config.broker.environment = args.env
    if args.dry_run:
        config.dry_run = True
    if args.log_level:
        config.log_level = args.log_level
    config.validate()
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
    if config.broker.environment == "live" and not config.dry_run:
        log.warning("connected to the LIVE account -- real orders will be modified")

    store = Store(config.database)
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
