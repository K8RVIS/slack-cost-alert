import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib import request as urllib_request


KST = timezone(timedelta(hours=9))
FINAL_REPORT_DATE = date(2026, 5, 22)
THRESHOLDS = [0.5, 0.8, 0.9]
ALERTS_PARAMETER_ROOT = "/custom_cost_alerts"


@dataclass(frozen=True)
class Config:
    aws_region: str
    slack_webhook_url: str
    cost_start_date: date
    budget_usd: float


def require_env(name):
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def parse_iso_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid date for COST_START_DATE: {value}") from exc


def format_currency(amount):
    return f"${amount:.2f}"


def calculate_usage_percentage(cost, budget_usd):
    if budget_usd <= 0:
        return 0.0
    return round((cost / budget_usd) * 100, 1)


def current_time_kst():
    return datetime.now(KST)


def resolve_report_date(run_time_kst):
    return min(run_time_kst.date() - timedelta(days=1), FINAL_REPORT_DATE)


def build_alert_scope_path(cost_start_date, final_report_date, budget_usd):
    budget_label = str(int(budget_usd)) if float(budget_usd).is_integer() else str(budget_usd).replace(".", "_")
    return (
        f"{ALERTS_PARAMETER_ROOT}/"
        f"{cost_start_date.isoformat()}_to_{final_report_date.isoformat()}_budget_{budget_label}"
    )


def load_config():
    aws_region = require_env("AWS_REGION")
    slack_webhook_url = require_env("SLACK_WEBHOOK_URL")
    cost_start_date = parse_iso_date(require_env("COST_START_DATE"))
    budget_usd = float(os.getenv("BUDGET_USD", "100"))
    return Config(
        aws_region=aws_region,
        slack_webhook_url=slack_webhook_url,
        cost_start_date=cost_start_date,
        budget_usd=budget_usd,
    )


def get_boto3_client(service_name, region_name):
    import boto3

    return boto3.client(service_name, region_name=region_name)


def fetch_cost_amount(cost_explorer_client, start_date, end_date, granularity):
    if end_date < start_date:
        return 0.0

    response = cost_explorer_client.get_cost_and_usage(
        TimePeriod={
            "Start": start_date.strftime("%Y-%m-%d"),
            "End": (end_date + timedelta(days=1)).strftime("%Y-%m-%d"),
        },
        Granularity=granularity,
        Metrics=["UnblendedCost"],
    )

    total_cost = 0.0
    for result in response.get("ResultsByTime", []):
        amount = result.get("Total", {}).get("UnblendedCost", {}).get("Amount", "0")
        total_cost += float(amount)

    return round(total_cost, 2)


def list_sent_thresholds(ssm_client, scope_path):
    thresholds = set()
    next_token = None

    while True:
        request_args = {"Path": scope_path, "Recursive": True}
        if next_token:
            request_args["NextToken"] = next_token

        response = ssm_client.get_parameters_by_path(**request_args)
        for parameter in response.get("Parameters", []):
            thresholds.add(parameter["Name"].rsplit("/", 1)[-1])

        next_token = response.get("NextToken")
        if not next_token:
            break

    return thresholds


def mark_threshold_as_sent(ssm_client, scope_path, threshold_percentage, sent_at, cumulative_cost, budget_usd):
    ssm_client.put_parameter(
        Name=f"{scope_path}/{threshold_percentage}",
        Value=json.dumps(
            {
                "sent": True,
                "threshold_percentage": threshold_percentage,
                "timestamp": sent_at.astimezone(timezone.utc).isoformat(),
                "cumulative_cost": round(cumulative_cost, 2),
                "budget_usd": round(budget_usd, 2),
            }
        ),
        Type="String",
        Overwrite=True,
    )


def build_threshold_message(threshold_percentage, cumulative_cost, budget_usd):
    return "\n".join(
        [
            f"⚠️ AWS 누적 비용이 예산의 {threshold_percentage}%를 넘었습니다.",
            f"전체 누적 비용: {format_currency(cumulative_cost)}",
            f"예산: {format_currency(budget_usd)}",
            f"사용률: {calculate_usage_percentage(cumulative_cost, budget_usd):.1f}%",
            f"집계 종료일: {FINAL_REPORT_DATE.isoformat()}",
        ]
    )


def check_budget_thresholds(cumulative_cost, budget_usd, ssm_client, scope_path, send_message, sent_at):
    existing_thresholds = list_sent_thresholds(ssm_client, scope_path)
    sent_thresholds = []

    for threshold in THRESHOLDS:
        threshold_percentage = str(int(threshold * 100))
        threshold_cost = budget_usd * threshold

        if cumulative_cost < threshold_cost or threshold_percentage in existing_thresholds:
            continue

        send_message(build_threshold_message(threshold_percentage, cumulative_cost, budget_usd))
        mark_threshold_as_sent(
            ssm_client=ssm_client,
            scope_path=scope_path,
            threshold_percentage=threshold_percentage,
            sent_at=sent_at,
            cumulative_cost=cumulative_cost,
            budget_usd=budget_usd,
        )
        sent_thresholds.append(threshold_percentage)

    return sent_thresholds


def build_daily_report_message(
    run_time_kst,
    report_date,
    daily_cost,
    cumulative_cost,
    budget_usd,
    cost_start_date,
):
    return "\n".join(
        [
            f"📊 AWS 비용 일일 보고",
            f"기준 시각: {run_time_kst.strftime('%Y-%m-%d %H:%M %Z')}",
            f"집계 기준일: {report_date.isoformat()}",
            f"일일 비용: {format_currency(daily_cost)}",
            f"전체 누적 비용: {format_currency(cumulative_cost)}",
            f"예산: {format_currency(budget_usd)}",
            f"사용률: {calculate_usage_percentage(cumulative_cost, budget_usd):.1f}%",
        ]
    )


def build_slack_payload(message):
    return {"text": message}


def send_slack_message(slack_webhook_url, message):
    payload = json.dumps(build_slack_payload(message)).encode("utf-8")
    request = urllib_request.Request(
        slack_webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib_request.urlopen(request, timeout=5) as response:
        status_code = getattr(response, "status", 200)
        if status_code >= 400:
            raise RuntimeError(f"Slack webhook request failed with status {status_code}")


def lambda_handler(event, context):
    config = load_config()
    run_time_kst = current_time_kst()
    report_date = resolve_report_date(run_time_kst)

    cost_explorer_client = get_boto3_client("ce", config.aws_region)
    ssm_client = get_boto3_client("ssm", config.aws_region)

    daily_cost = fetch_cost_amount(
        cost_explorer_client=cost_explorer_client,
        start_date=report_date,
        end_date=report_date,
        granularity="DAILY",
    )
    cumulative_cost = fetch_cost_amount(
        cost_explorer_client=cost_explorer_client,
        start_date=config.cost_start_date,
        end_date=report_date,
        granularity="MONTHLY",
    )

    report_message = build_daily_report_message(
        run_time_kst=run_time_kst,
        report_date=report_date,
        daily_cost=daily_cost,
        cumulative_cost=cumulative_cost,
        budget_usd=config.budget_usd,
        cost_start_date=config.cost_start_date,
    )
    send_slack_message(config.slack_webhook_url, report_message)

    scope_path = build_alert_scope_path(
        cost_start_date=config.cost_start_date,
        final_report_date=FINAL_REPORT_DATE,
        budget_usd=config.budget_usd,
    )
    sent_thresholds = check_budget_thresholds(
        cumulative_cost=cumulative_cost,
        budget_usd=config.budget_usd,
        ssm_client=ssm_client,
        scope_path=scope_path,
        send_message=lambda message: send_slack_message(config.slack_webhook_url, message),
        sent_at=run_time_kst,
    )

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": "AWS cost alert completed",
                "report_date": report_date.isoformat(),
                "daily_cost": daily_cost,
                "cumulative_cost": cumulative_cost,
                "budget_usd": config.budget_usd,
                "thresholds_sent": sent_thresholds,
                "alert_scope_path": scope_path,
            }
        ),
    }
