"""Demo and live side by side: separate credentials, separate everything."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from tmbot.config import AccountCredentials, Config, apply_env, resolve_environment_secrets
from tmbot.errors import ConfigError


def ready(config: Config) -> Config:
    config.broker.demo = AccountCredentials("dk", "me@x.com", "dp")
    config.broker.live = AccountCredentials("lk", "me@x.com", "lp")
    return config


class CredentialTests(unittest.TestCase):
    def test_each_environment_uses_its_own_key(self):
        config = ready(Config())
        config.broker.environment = "demo"
        self.assertEqual(config.broker.active.api_key, "dk")
        config.broker.environment = "live"
        self.assertEqual(config.broker.active.api_key, "lk")

    def test_a_single_shared_key_still_works(self):
        # Nobody's existing .env should break.
        config = Config()
        config.broker.api_key = "k"
        config.broker.identifier = "me@x.com"
        config.broker.password = "p"
        for environment in ("demo", "live"):
            config.broker.environment = environment
            self.assertEqual(config.broker.active.api_key, "k")

    def test_a_specific_key_beats_the_shared_one(self):
        config = Config()
        config.broker.api_key = "shared"
        config.broker.identifier = "me@x.com"
        config.broker.password = "p"
        config.broker.live = AccountCredentials(api_key="live-only")
        config.broker.environment = "live"
        self.assertEqual(config.broker.active.api_key, "live-only")
        self.assertEqual(config.broker.active.identifier, "me@x.com", "falls back per field")

    def test_environment_variables_populate_both(self):
        env = {
            "CAPITAL_DEMO_API_KEY": "dk", "CAPITAL_DEMO_IDENTIFIER": "d@x",
            "CAPITAL_DEMO_PASSWORD": "dp",
            "CAPITAL_LIVE_API_KEY": "lk", "CAPITAL_LIVE_IDENTIFIER": "l@x",
            "CAPITAL_LIVE_PASSWORD": "lp",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            config = apply_env(Config())
        self.assertEqual(config.broker.demo.api_key, "dk")
        self.assertEqual(config.broker.live.identifier, "l@x")

    def test_missing_credentials_name_the_environment(self):
        config = Config()
        config.broker.environment = "live"
        config.broker.live_enabled = True
        with self.assertRaises(ConfigError) as caught:
            config.validate()
        self.assertIn("CAPITAL_LIVE_API_KEY", str(caught.exception))


class IsolationTests(unittest.TestCase):
    def test_the_database_is_never_shared(self):
        # Blending practice results into the live journal would make the only
        # measure of whether this works meaningless.
        config = Config()
        config.broker.environment = "demo"
        demo = config.resolved_database
        config.broker.environment = "live"
        self.assertNotEqual(demo, config.resolved_database)
        self.assertIn("demo", demo)
        self.assertIn("live", config.resolved_database)

    def test_reports_are_never_shared(self):
        config = Config()
        config.broker.environment = "demo"
        demo = config.resolved_report_dir
        config.broker.environment = "live"
        self.assertNotEqual(demo, config.resolved_report_dir)

    def test_a_custom_database_name_is_still_split(self):
        config = Config()
        config.database = "data/mybot.sqlite3"
        config.broker.environment = "live"
        self.assertEqual(config.resolved_database, "data/mybot-live.sqlite3")

    def test_telegram_can_use_a_separate_bot_per_environment(self):
        # Two pollers on one token steal each other's updates, so running both
        # at once with a shared bot silently breaks commands.
        env = {"TELEGRAM_BOT_TOKEN": "shared", "TELEGRAM_CHAT_ID": "1",
               "TELEGRAM_LIVE_BOT_TOKEN": "live-bot"}
        with mock.patch.dict(os.environ, env, clear=False):
            config = apply_env(Config())
            config.broker.environment = "live"
            resolve_environment_secrets(config)
        self.assertEqual(config.telegram.bot_token, "live-bot")

    def test_telegram_falls_back_to_the_shared_bot(self):
        env = {"TELEGRAM_BOT_TOKEN": "shared", "TELEGRAM_CHAT_ID": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            config = apply_env(Config())
            config.broker.environment = "demo"
            resolve_environment_secrets(config)
        self.assertEqual(config.telegram.bot_token, "shared")
        self.assertTrue(config.telegram.enabled)


class LiveGateTests(unittest.TestCase):
    def test_choosing_live_on_the_command_line_is_not_enough(self):
        config = ready(Config())
        config.broker.environment = "live"
        with self.assertRaises(ConfigError) as caught:
            config.validate()
        self.assertIn("live_enabled", str(caught.exception))

    def test_both_acts_together_enable_it(self):
        config = ready(Config())
        config.broker.environment = "live"
        config.broker.live_enabled = True
        config.validate()

    def test_the_gate_does_not_apply_to_demo(self):
        config = ready(Config())
        config.broker.environment = "demo"
        config.validate()

    def test_inspection_commands_run_even_when_gated(self):
        # You need to be able to see the configuration precisely when it is
        # refusing to start.
        config = Config()
        config.broker.environment = "live"
        config.validate(connecting=False)


class TimezoneTests(unittest.TestCase):
    """A wrong report hour looks exactly like a working bot, so it must fail loudly."""

    def test_utc_always_resolves(self):
        from tmbot.config import resolve_timezone
        from datetime import timezone
        self.assertIs(resolve_timezone("UTC"), timezone.utc)

    def test_a_real_zone_resolves(self):
        from tmbot.config import resolve_timezone
        self.assertIsNotNone(resolve_timezone("Asia/Riyadh"))

    def test_an_unknown_zone_names_the_windows_fix(self):
        from tmbot.config import resolve_timezone
        with self.assertRaises(ConfigError) as caught:
            resolve_timezone("Not/AZone")
        self.assertIn("tzdata", str(caught.exception))

    def test_check_catches_it_before_the_bot_runs(self):
        config = ready(Config())
        config.broker.environment = "demo"
        config.report.timezone = "Nowhere/Fictional"
        with self.assertRaises(ConfigError):
            config.validate(connecting=False)

    def test_a_malformed_daily_time_is_rejected(self):
        config = ready(Config())
        config.broker.environment = "demo"
        config.report.daily_time = "7am"
        with self.assertRaises(ConfigError) as caught:
            config.validate()
        self.assertIn("HH:MM", str(caught.exception))

    def test_an_out_of_range_hour_is_rejected(self):
        config = ready(Config())
        config.broker.environment = "demo"
        config.report.daily_time = "25:00"
        with self.assertRaises(ConfigError):
            config.validate()


class DoctorTests(unittest.TestCase):
    """Run `tmbot doctor` against a fake Capital.com that answers per host."""

    def run_doctor(self, answers):
        import io, contextlib, json
        from unittest import mock
        import requests
        from tmbot import cli

        class Response:
            def __init__(self, status, body, headers=None):
                self.status_code, self._body = status, body
                self.headers = headers or {}
                self.text = json.dumps(body)
                self.content = self.text.encode()
            def json(self):
                return self._body

        class HostAwareSession:
            def post(self, url, json=None, headers=None, timeout=None):
                host = "demo" if "demo-api" in url else "live"
                return answers[host]
            def request(self, method, url, **kwargs):
                if url.endswith("/accounts"):
                    return Response(200, {"accounts": [{"accountId": "2527", "preferred": True}]})
                return Response(200, {})
            def close(self):
                pass

        ok = Response(200, {}, {"CST": "c", "X-SECURITY-TOKEN": "x"})
        answers = {k: (ok if v == "ok" else Response(401, {"errorCode": v}))
                   for k, v in answers.items()}
        env = {"CAPITAL_LIVE_API_KEY": "k", "CAPITAL_LIVE_IDENTIFIER": "a@b.c",
               "CAPITAL_LIVE_PASSWORD": "p"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), \
             mock.patch("tmbot.broker.capital.requests.Session", HostAwareSession), \
             contextlib.redirect_stdout(out):
            code = cli.main(["--env", "live", "doctor"])
        return code, out.getvalue()

    def test_null_account_id_is_not_blamed_on_the_key(self):
        # Exactly what a real account returned: demo fine, live refused.
        code, text = self.run_doctor({"demo": "ok", "live": "error.null.accountId"})
        self.assertEqual(code, 1)
        self.assertIn("key and password are fine", text)
        self.assertIn("will NOT fix", text)
        self.assertIn("CFD", text)
        self.assertNotIn("DEMO key", text, "keys belong to the login, not an account")

    def test_wrong_credentials_are_named_as_such(self):
        code, text = self.run_doctor({"demo": "error.invalid.details",
                                      "live": "error.invalid.details"})
        self.assertIn("custom one you set", text)

    def test_working_credentials_say_so(self):
        code, text = self.run_doctor({"demo": "ok", "live": "ok"})
        self.assertEqual(code, 0)
        self.assertIn("Nothing to fix", text)


class TaggingTests(unittest.TestCase):
    def test_alerts_carry_the_environment(self):
        from tmbot.notify.base import NullNotifier
        notifier = NullNotifier()
        notifier.set_tag("[LIVE] ")
        notifier.send("stop moved")
        self.assertEqual(notifier.messages[0][1], "[LIVE] stop moved")

    def test_a_multi_notifier_tags_every_transport(self):
        from tmbot.notify.base import MultiNotifier, NullNotifier
        first, second = NullNotifier(), NullNotifier()
        MultiNotifier([first, second]).set_tag("[DEMO] ")
        self.assertEqual(first.tag, "[DEMO] ")
        self.assertEqual(second.tag, "[DEMO] ")


if __name__ == "__main__":
    unittest.main()
