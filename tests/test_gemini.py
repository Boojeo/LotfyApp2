"""Gemini second opinion: independent, shown beside the bot, never in charge."""

from __future__ import annotations

import json
import unittest

import requests

from tmbot.analysis import gemini
from tmbot.analysis.report import ReportBuilder, render_text
from tmbot.config import GeminiConfig
from tmbot.models import Bias
from tests.test_style import UP, auto_config, broker_with

KEY = "test-key-never-printed-123"

ANSWER = {
    "technical_direction": "BULLISH", "entry_quality": "WEAK", "technical_strength": 64,
    "market_structure": "higher highs on M15", "timeframe_alignment": "PARTIAL",
    "fundamental_bias": "BULLISH", "news_status": "AVAILABLE",
    "news_summary": "Softer dollar after Fed minutes",
    "key_reasons": ["trend intact"], "risk_concerns": ["RSI stretched"],
    "invalidating_conditions": ["close below 3395"],
}
GROUNDING = {"groundingChunks": [{"web": {"uri": "https://news.example/a", "title": "Fed"}}]}


class Reply:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def answer(payload=ANSWER, grounding=GROUNDING, text=None):
    candidate = {"content": {"parts": [{"text": text if text is not None
                                        else json.dumps(payload)}]}}
    if grounding:
        candidate["groundingMetadata"] = grounding
    return Reply(200, {"candidates": [candidate]})


class FakeSession:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "body": json, "headers": headers})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def plan_for(bias=Bias.BEARISH):
    plan = ReportBuilder(broker_with(M15=UP, H1=UP), auto_config()).build("GOLD")
    plan.bias = bias
    return plan


def reviewer(*replies, clock=None, sleeps=None, **config):
    settings = GeminiConfig(api_key=KEY, **config)
    session = FakeSession(*replies)
    nap = sleeps.append if sleeps is not None else (lambda seconds: None)
    return gemini.GeminiReviewer(settings, session, clock=clock or (lambda: 0.0),
                                 sleep=nap), session


class AnswerTests(unittest.TestCase):
    def test_a_valid_answer_is_structured_and_compared(self):
        review, session = reviewer(answer())
        result = review.review(plan_for(Bias.BEARISH), [])
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["technical_direction"], "BULLISH")
        self.assertEqual(result["agreement"], "CONFLICT", "bot BEARISH vs Gemini BULLISH")
        self.assertEqual(result["sources"][0]["uri"], "https://news.example/a")
        body = session.calls[0]["body"]
        self.assertEqual(body["tools"], [{"google_search": {}}])
        self.assertIn("responseJsonSchema", body["generationConfig"])

    def test_agreement_is_aligned_conflict_or_uncertain_never_averaged(self):
        self.assertEqual(gemini.agreement(Bias.BEARISH, "BEARISH"), "ALIGNED")
        self.assertEqual(gemini.agreement(Bias.BEARISH, "BULLISH"), "CONFLICT")
        self.assertEqual(gemini.agreement(Bias.BEARISH, "UNCERTAIN"), "UNCERTAIN")
        self.assertEqual(gemini.agreement(Bias.BEARISH, "NEUTRAL"), "UNCERTAIN")

    def test_news_without_search_sources_is_reported_unavailable_not_guessed(self):
        review, _ = reviewer(answer(grounding=None))
        result = review.review(plan_for(), [])
        self.assertEqual(result["news_status"], "UNAVAILABLE")
        self.assertEqual(result["fundamental_bias"], "UNAVAILABLE")

    def test_code_fenced_json_is_still_read(self):
        review, _ = reviewer(answer(text="```json\n" + json.dumps(ANSWER) + "\n```"))
        self.assertEqual(review.review(plan_for(), [])["status"], "OK")

    def test_out_of_range_values_are_cleaned_not_trusted(self):
        bad = dict(ANSWER, technical_direction="MOON", technical_strength=900,
                   key_reasons=["a", "b", "c", "d", "e"])
        result = reviewer(answer(bad))[0].review(plan_for(), [])
        self.assertEqual(result["technical_direction"], "UNCERTAIN")
        self.assertEqual(result["technical_strength"], 100)
        self.assertEqual(len(result["key_reasons"]), 3)

    def test_a_model_refusing_schema_with_search_is_asked_plainly(self):
        refused = Reply(400, {"error": {"status": "INVALID_ARGUMENT",
                                        "message": "response schema not supported with tools"}})
        review, session = reviewer(refused, answer())
        self.assertEqual(review.review(plan_for(), [])["status"], "OK")
        self.assertNotIn("generationConfig", session.calls[1]["body"])


class FailureTests(unittest.TestCase):
    def status(self, *replies):
        result = reviewer(*replies)[0].review(plan_for(), [])
        return result["status"], result.get("reason", "")

    def test_each_failure_is_named(self):
        cases = [
            (requests.Timeout(), "TIMEOUT"),
            (requests.ConnectionError(), "NETWORK"),
            (Reply(400, {"error": {"status": "INVALID_ARGUMENT",
                                   "message": "API key not valid. Please pass a valid API key."}}),
             "BAD_KEY"),
            (Reply(403, {"error": {"status": "PERMISSION_DENIED", "message": "denied"}}), "BAD_KEY"),
            (Reply(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}}),
             "RATE_LIMITED"),
            (Reply(404, {"error": {"status": "NOT_FOUND", "message": "no model"}}), "ERROR"),
            (Reply(503, {"error": {"status": "UNAVAILABLE", "message": "overloaded"}}), "ERROR"),
            (Reply(200, {"candidates": []}), "NO_ANSWER"),
            (Reply(200, {"promptFeedback": {"blockReason": "SAFETY"}}), "NO_ANSWER"),
            (answer(text=""), "NO_ANSWER"),
            (answer(text="I think gold goes up."), "MALFORMED"),
            (Reply(200, None, text="<html>"), "MALFORMED"),
        ]
        for reply, expected in cases:
            with self.subTest(expected=expected, reply=reply):
                self.assertEqual(self.status(reply)[0], expected)

    def test_no_key_means_no_call_and_says_so(self):
        session = FakeSession()
        review = gemini.GeminiReviewer(GeminiConfig(api_key=""), session)
        self.assertEqual(review.review(plan_for(), [])["status"], "DISABLED")
        self.assertEqual(session.calls, [])

    def test_the_key_goes_in_a_header_and_never_into_a_message(self):
        review, session = reviewer(Reply(403, {"error": {"message": f"bad key {KEY}"}}))
        result = review.review(plan_for(), [])
        self.assertNotIn(KEY, session.calls[0]["url"])
        self.assertEqual(session.calls[0]["headers"]["x-goog-api-key"], KEY)
        self.assertNotIn(KEY, result["reason"])
        self.assertNotIn(KEY, render_text(dict_plan(result)))


def dict_plan(review):
    plan = plan_for()
    plan.second_opinion = review
    return plan


class CostTests(unittest.TestCase):
    def test_the_same_question_within_the_window_is_not_asked_twice(self):
        now = [0.0]
        review, session = reviewer(answer(), answer(), clock=lambda: now[0])
        plan = plan_for()
        review.review(plan, [])
        self.assertTrue(review.review(plan, []).get("cached"))
        self.assertEqual(len(session.calls), 1)
        now[0] = 301.0
        review.review(plan, [])
        self.assertEqual(len(session.calls), 2)

    def test_failures_are_not_cached(self):
        review, session = reviewer(requests.Timeout(), answer())
        plan = plan_for()
        self.assertEqual(review.review(plan, [])["status"], "TIMEOUT")
        self.assertEqual(review.review(plan, [])["status"], "OK")


def quota_error(message, quota_id="", retry=None):
    details = []
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id}]})
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": retry})
    return Reply(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                 "message": message, "details": details}})


class RateLimitTests(unittest.TestCase):
    """Free keys allow a few requests a minute: pace, explain, and back off."""

    def test_after_a_limit_it_waits_the_time_google_asks_for(self):
        now = [0.0]
        review, session = reviewer(
            quota_error("Quota exceeded. Please retry in 33s.",
                        "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "33s"),
            answer(), clock=lambda: now[0])
        plan = plan_for()
        first = review.review(plan, [])
        self.assertEqual(first["status"], "RATE_LIMITED")
        self.assertIn("per-minute", first["reason"])

        now[0] = 10.0
        second = review.review(plan, [])
        self.assertEqual(second["status"], "RATE_LIMITED")
        self.assertIn("next try in", second["reason"])
        self.assertEqual(len(session.calls), 1, "no call while paused")

        now[0] = 40.0
        self.assertEqual(review.review(plan, [])["status"], "OK")

    def test_the_daily_quota_says_so(self):
        result = reviewer(quota_error(
            "You exceeded your current quota.",
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier"))[0].review(plan_for(), [])
        self.assertIn("daily free quota", result["reason"])

    def test_a_model_without_free_quota_says_to_change_it(self):
        result = reviewer(quota_error(
            "Quota exceeded for metric: generate_content_free_tier_requests, limit: 0, "
            "model: gemini-3.5-flash"))[0].review(plan_for(), [])
        self.assertIn("no free quota", result["reason"])
        self.assertIn("gemini.model", result["reason"])

    def test_questions_are_spaced_out_not_fired_in_a_burst(self):
        sleeps = []
        review, _ = reviewer(answer(), answer(), sleeps=sleeps)
        review.review(plan_for(Bias.BEARISH), [])
        other = plan_for(Bias.BULLISH)
        other.epic = "SILVER"                       # a different question, not cached
        review.review(other, [])
        self.assertEqual(sleeps, [6.0])


class SafetyTests(unittest.TestCase):
    """Gemini can disagree loudly; it can never change the bot's decision."""

    def build(self, review):
        class Stub:
            def review(self, *args, **kwargs):
                return review
        builder = ReportBuilder(broker_with(M15=UP, H1=UP), auto_config(), reviewer=Stub())
        return builder.build("GOLD", second_opinion=True)

    def test_a_conflicting_opinion_changes_no_level_size_or_verdict(self):
        alone = ReportBuilder(broker_with(M15=UP, H1=UP), auto_config()).build("GOLD")
        disagreeing = self.build(dict(ANSWER, technical_direction="BEARISH",
                                      status="OK", agreement="CONFLICT", model="m"))
        for name in ("bias", "direction", "sl", "tp1", "tp2", "tp3"):
            self.assertEqual(getattr(disagreeing, name), getattr(alone, name), name)
        self.assertEqual(disagreeing.sizing, alone.sizing)
        self.assertEqual(disagreeing.assessment["verdict"], alone.assessment["verdict"])

    def test_a_crashing_reviewer_still_returns_the_plan(self):
        class Broken:
            def review(self, *args, **kwargs):
                raise RuntimeError("boom")
        plan = ReportBuilder(broker_with(M15=UP, H1=UP), auto_config(),
                             reviewer=Broken()).build("GOLD", second_opinion=True)
        self.assertEqual(plan.second_opinion["status"], "ERROR")
        self.assertTrue(plan.sizing)

    def test_the_bots_decision_comes_first_then_the_second_opinion(self):
        text = render_text(self.build(dict(ANSWER, status="OK", agreement="CONFLICT",
                                           model="gemini-3.5-flash", sources=[])))
        self.assertLess(text.index("VERDICT:"), text.index("SECOND OPINION"))
        self.assertIn("CONFLICT", text)

    def test_a_silent_gemini_is_reported_by_name(self):
        text = render_text(self.build(gemini.failure("TIMEOUT", "no answer within 25s", "m")))
        self.assertIn("Gemini did not respond -- TIMEOUT", text)
        self.assertIn("stands on its own", text)


if __name__ == "__main__":
    unittest.main()
