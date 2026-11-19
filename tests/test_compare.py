"""Old-vs-new comparison and the records export: read-only, and never overwrites."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from tmbot.analysis import compare, funnel, replay
from tmbot.analysis.report import ReportBuilder
from tmbot.broker.paper import PaperBroker
from tmbot.i18n import Translator
from tmbot.models import utcnow
from tmbot.store import Store
from tests.test_audit import histories
from tests.test_style import GOLD, UP, auto_config, broker_with

T = Translator()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def shadow_plans(n=4):
    config = auto_config()
    config.scoring.mode = "shadow"
    plans = []
    for i in range(n):
        plan = ReportBuilder(broker_with(M15=UP, H1=UP), config).build("GOLD")
        plan.plan_id = f"GOLD-test-{i}"
        plan.created_at = utcnow() - timedelta(hours=i)
        plans.append(plan)
    return plans


def run_replay(shadow=True):
    real = PaperBroker()
    real.set_rules(replace(GOLD, epic="GOLD"))
    real.set_quote("GOLD", 3400.0, 3400.2)
    real.account_summary = lambda: {"currency": "USD", "equity": 1_000_000.0}
    broker = replay.ReplayBroker(real, histories(drift=0.00006), equity=1_000_000.0)
    config = auto_config()
    config.scoring.mode = "shadow" if shadow else "legacy"
    return replay.replay(ReportBuilder(broker, config), broker, "GOLD", step=24, horizon=48,
                         min_strength=60.0, t=T, warmup=300)


class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "tmbot-demo.sqlite3")
        store = Store(self.db)
        for plan in shadow_plans():
            store.save_plan(plan)
        store.close()

    def test_reading_plans_leaves_the_database_byte_for_byte_unchanged(self):
        before = sha(self.db)
        plans = compare.read_plans(self.db, utcnow() - timedelta(days=1))
        self.assertEqual(len(plans), 4)
        self.assertTrue(all(p.shadow for p in plans))
        records = compare.records_from_plans(plans, min_strength=60.0, t=T)
        compare.render(compare.compare(records, min_strength=60.0))
        compare.write_records(os.path.join(self.dir, "out.csv"), records)
        self.assertEqual(sha(self.db), before)

    def test_the_connection_cannot_write(self):
        connection = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute("DELETE FROM plans")
        connection.close()

    def test_a_missing_database_is_not_created(self):
        missing = os.path.join(self.dir, "nope.sqlite3")
        self.assertEqual(compare.read_plans(missing, utcnow() - timedelta(days=1)), [])
        self.assertFalse(os.path.exists(missing))

    def test_an_existing_export_file_is_never_overwritten(self):
        target = os.path.join(self.dir, "records.csv")
        Path(target).write_text("precious")
        with self.assertRaises(FileExistsError):
            compare.write_records(target, [])
        self.assertEqual(Path(target).read_text(), "precious")

    def test_the_command_refuses_an_existing_file_and_writes_nothing_else(self):
        from tmbot import cli
        target = os.path.join(self.dir, "records.csv")
        Path(target).write_text("precious")
        args = type("A", (), dict(records=target, epic=None, days=3))()
        config = auto_config()
        config.broker.environment = "demo"
        config.database = os.path.join(self.dir, "tmbot.sqlite3")
        before = sha(self.db)
        self.assertEqual(cli._compare_command(args, config), 2)
        self.assertEqual(Path(target).read_text(), "precious")
        self.assertEqual(sha(self.db), before)

    def test_the_compare_command_runs_on_the_stored_plans(self):
        from tmbot import cli
        args = type("A", (), dict(records=os.path.join(self.dir, "x.jsonl"), epic=None, days=3))()
        config = auto_config()
        config.database = os.path.join(self.dir, "tmbot.sqlite3")      # resolves to ...-demo.sqlite3
        before = sha(self.db)
        self.assertEqual(cli._compare_command(args, config), 0)
        self.assertEqual(sha(self.db), before)
        rows = [json.loads(line) for line in Path(args.records).read_text().splitlines()]
        self.assertEqual(len(rows), 4)


class ReplayExportTests(unittest.TestCase):
    def test_records_carry_every_check_and_the_three_results(self):
        records = run_replay()
        self.assertGreater(len(records), 10)
        for record in records:
            self.assertEqual([g["gate"] for g in record.gates],
                             ["direction", "with_bias", "approved", "strength", "aligned", "risk",
                              "doubtful", "entry_range"])
            failed = [g["blocker"] for g in record.gates if not g["passed"]]
            self.assertEqual(failed[0] if failed else funnel.PASS, record.blocker)
            self.assertEqual(set(record.shadow), {"version", "technical", "fundamental", "overall",
                                                  "legacy", "params"})

    def test_the_replay_decisions_are_identical_with_and_without_shadow(self):
        off, on = run_replay(shadow=False), run_replay(shadow=True)
        self.assertEqual(len(off), len(on))
        for a, b in zip(off, on):
            self.assertEqual((a.ts, a.verdict, a.strength, a.bias, a.blocker, a.flags, a.outcome),
                             (b.ts, b.verdict, b.strength, b.bias, b.blocker, b.flags, b.outcome))
        self.assertTrue(all(not r.shadow for r in off))

    def test_the_csv_has_one_row_per_decision_and_the_documented_columns(self):
        records = run_replay()
        path = os.path.join(tempfile.mkdtemp(), "r.csv")
        self.assertEqual(compare.write_records(path, records), len(records))
        with open(path, newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), len(records))
        self.assertEqual(tuple(rows[0]), compare.COLUMNS)
        self.assertTrue(rows[0]["overall_basis"])

    def test_the_report_counts_old_and_new_and_lists_the_reasons(self):
        records = run_replay()
        result = compare.compare(records, min_strength=60.0)
        self.assertEqual(result["shadowed"], len(records))
        for level in compare.THRESHOLDS:
            self.assertIn(level, result["reach"])
        # no news in a replay: every overall result is technical only, so it equals the technical one
        self.assertEqual(set(result["basis"]), {"technical_only"})
        self.assertEqual(result["accepted_old"], sum(1 for r in records if r.blocker == funnel.PASS))
        text = compare.render(result)
        for needle in ("first failing check", "HYPOTHETICAL", "TP1 first"):
            self.assertIn(needle, text)

    def test_with_no_shadow_the_report_says_how_to_get_one(self):
        text = compare.render(compare.compare(run_replay(shadow=False), min_strength=60.0))
        self.assertIn("scoring.mode: shadow", text)


if __name__ == "__main__":
    unittest.main()
