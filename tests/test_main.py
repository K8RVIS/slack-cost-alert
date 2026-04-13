import importlib.util
import json
import os
import sys
import types
import unittest
from datetime import date, datetime, timezone, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "main.py"


class _FakeBoto3Module:
    def client(self, service_name, region_name=None):
        return types.SimpleNamespace()


class _FakeRequestsModule:
    @staticmethod
    def get(*args, **kwargs):
        return types.SimpleNamespace(json=lambda: {"rates": {"KRW": 1400}})

    @staticmethod
    def post(*args, **kwargs):
        return types.SimpleNamespace(raise_for_status=lambda: None)


def load_main_module():
    spec = importlib.util.spec_from_file_location("cost_alert_main", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)

    original_env = os.environ.copy()
    original_boto3 = sys.modules.get("boto3")
    original_requests = sys.modules.get("requests")

    os.environ.setdefault("AWS_REGION", "ap-northeast-2")
    os.environ.setdefault("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/test/test/test")
    os.environ.setdefault("COST_START_DATE", "2024-01-15")
    os.environ.setdefault("BUDGET_USD", "100")

    sys.modules["boto3"] = _FakeBoto3Module()
    sys.modules["requests"] = _FakeRequestsModule()

    try:
        spec.loader.exec_module(module)
    finally:
        os.environ.clear()
        os.environ.update(original_env)

        if original_boto3 is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = original_boto3

        if original_requests is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = original_requests

    return module


class ReportDateTests(unittest.TestCase):
    def test_report_date_uses_previous_kst_day_and_caps_at_final_date(self):
        module = load_main_module()
        resolver = getattr(module, "resolve_report_date", None)

        self.assertIsNotNone(resolver, "resolve_report_date() should exist")

        regular_run = datetime(2026, 4, 14, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        final_run = datetime(2026, 5, 23, 10, 0, tzinfo=timezone(timedelta(hours=9)))

        self.assertEqual(resolver(regular_run), date(2026, 4, 13))
        self.assertEqual(resolver(final_run), date(2026, 5, 22))


class CostWindowTests(unittest.TestCase):
    def test_cumulative_cost_query_starts_from_cost_start_date(self):
        module = load_main_module()
        fetcher = getattr(module, "fetch_cost_amount", None)

        self.assertIsNotNone(fetcher, "fetch_cost_amount() should exist")

        calls = []

        class FakeCeClient:
            def get_cost_and_usage(self, **kwargs):
                calls.append(kwargs)
                return {
                    "ResultsByTime": [
                        {"Total": {"UnblendedCost": {"Amount": "10.00"}}},
                        {"Total": {"UnblendedCost": {"Amount": "15.50"}}},
                    ]
                }

        amount = fetcher(
            FakeCeClient(),
            start_date=date(2024, 1, 15),
            end_date=date(2026, 5, 22),
            granularity="MONTHLY",
        )

        self.assertEqual(amount, 25.5)
        self.assertEqual(
            calls[0]["TimePeriod"],
            {"Start": "2024-01-15", "End": "2026-05-23"},
        )


class AlertScopeTests(unittest.TestCase):
    def test_alert_scope_path_is_campaign_based(self):
        module = load_main_module()
        builder = getattr(module, "build_alert_scope_path", None)

        self.assertIsNotNone(builder, "build_alert_scope_path() should exist")
        self.assertEqual(
            builder(date(2024, 1, 15), date(2026, 5, 22), 100.0),
            "/custom_cost_alerts/2024-01-15_to_2026-05-22_budget_100",
        )


class MessageTests(unittest.TestCase):
    def test_slack_payload_uses_text_field_instead_of_discord_content(self):
        module = load_main_module()
        payload_builder = getattr(module, "build_slack_payload", None)

        self.assertIsNotNone(payload_builder, "build_slack_payload() should exist")
        payload = payload_builder("hello slack")

        self.assertEqual(payload, {"text": "hello slack"})
        self.assertNotIn("content", payload)

    def test_report_message_mentions_cumulative_budget_usage_fields(self):
        module = load_main_module()
        builder = getattr(module, "build_daily_report_message", None)

        self.assertIsNotNone(builder, "build_daily_report_message() should exist")

        message = builder(
            run_time_kst=datetime(2026, 4, 13, 10, 0, tzinfo=timezone(timedelta(hours=9))),
            report_date=date(2026, 4, 12),
            daily_cost=12.34,
            cumulative_cost=67.89,
            budget_usd=100.0,
            cost_start_date=date(2024, 1, 15),
        )

        self.assertIn("기준 시각", message)
        self.assertIn("일일 비용", message)
        self.assertIn("전체 누적 비용", message)
        self.assertIn("예산: $100.00", message)
        self.assertIn("사용률: 67.9%", message)
        self.assertIn("집계 기준일: 2026-04-12", message)
        self.assertNotIn("이번 달", message)


class ThresholdTests(unittest.TestCase):
    def test_threshold_alerts_are_sent_once_per_campaign_scope(self):
        module = load_main_module()
        checker = getattr(module, "check_budget_thresholds", None)

        self.assertIsNotNone(checker, "check_budget_thresholds() should exist")

        sent_messages = []
        stored_parameters = []

        class FakeSsmClient:
            def __init__(self):
                self.responses = [
                    {"Parameters": [], "NextToken": "page-2"},
                    {
                        "Parameters": [
                            {
                                "Name": "/custom_cost_alerts/2024-01-15_to_2026-05-22_budget_100/50"
                            }
                        ]
                    },
                ]

            def get_parameters_by_path(self, **kwargs):
                return self.responses.pop(0)

            def put_parameter(self, **kwargs):
                stored_parameters.append(kwargs)

        result = checker(
            cumulative_cost=85.0,
            budget_usd=100.0,
            ssm_client=FakeSsmClient(),
            scope_path="/custom_cost_alerts/2024-01-15_to_2026-05-22_budget_100",
            send_message=sent_messages.append,
            sent_at=datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result, ["80"])
        self.assertEqual(len(sent_messages), 1)
        self.assertIn("80%", sent_messages[0])
        self.assertEqual(
            stored_parameters[0]["Name"],
            "/custom_cost_alerts/2024-01-15_to_2026-05-22_budget_100/80",
        )
        self.assertEqual(
            json.loads(stored_parameters[0]["Value"])["threshold_percentage"],
            "80",
        )


if __name__ == "__main__":
    unittest.main()
