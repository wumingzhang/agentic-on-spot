"""Lambda entry point: validate, verify the instance, submit the SSM command.

Outcomes:
  * returns {"status": "ignored"|"rejected", ...} for events that must never
    be retried (wrong shape, unsupported action, tag mismatch, missing
    instance). Returning instead of raising stops Lambda's async retries;
  * returns {"status": "submitted", "command_id": ...} when SSM accepted the
    request. That is all it means: the host may still fail or be gone;
  * raises for API errors and timeouts, so the asynchronous invocation is
    retried within the event-age bound set in Terraform.

Logs are JSON lines with a bounded set of fields: no raw event, no
environment, no command text beyond its length.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import Config, load_config
from .render import render_send_command
from .validation import ValidationError, check_instance_tags, validate_event

LOG_FIELD_LIMIT = 512


def log(level: str, msg: str, **fields: Any) -> None:
    record = {"level": level, "msg": msg}
    for key, value in fields.items():
        if value is None:
            continue
        text = value if isinstance(value, (int, float, bool)) else str(value)
        if isinstance(text, str) and len(text) > LOG_FIELD_LIMIT:
            text = text[:LOG_FIELD_LIMIT] + "...(truncated)"
        record[key] = text
    print(json.dumps(record, sort_keys=True), file=sys.stdout, flush=True)


def make_clients(region: str) -> tuple[Any, Any]:
    """Real boto3 clients with finite timeouts and retries.

    Two attempts in total of (3s connect + 10s read) per call keep the two
    calls we make inside the 60s Lambda timeout even when both are slow.
    botocore's `max_attempts` counts retries *after* the first try, so the
    total is spelled out explicitly.
    """
    import boto3  # noqa: PLC0415 - imported lazily so tests need no boto3
    from botocore.config import Config as BotoConfig

    boto_config = BotoConfig(
        region_name=region,
        connect_timeout=3,
        read_timeout=10,
        retries={"total_max_attempts": 2, "mode": "standard"},
    )
    return boto3.client("ec2", config=boto_config), boto3.client("ssm", config=boto_config)


def account_from_context(context: Any) -> str | None:
    arn = getattr(context, "invoked_function_arn", None)
    if isinstance(arn, str):
        parts = arn.split(":")
        if len(parts) >= 5 and parts[4].isdigit():
            return parts[4]
    return None


def describe_instance(ec2: Any, instance_id: str) -> dict:
    response = ec2.describe_instances(InstanceIds=[instance_id])
    instances = [i for r in response.get("Reservations", []) for i in r.get("Instances", [])]
    if len(instances) != 1 or instances[0].get("InstanceId") != instance_id:
        raise ValidationError("DescribeInstances did not return exactly the requested instance")
    return instances[0]


def event_age_problem(event_time: datetime, config: Config, now: datetime) -> str | None:
    """Why an otherwise valid notice should be ignored because of its age, or None."""
    age = (now - event_time).total_seconds()
    if config.max_event_age and age > config.max_event_age:
        return f"stale event: {int(age)}s old, TTL {config.max_event_age}s"
    if age < -config.max_future_skew:
        return f"event time {int(-age)}s in the future exceeds skew tolerance {config.max_future_skew}s"
    return None


def handle(
    event: Any,
    context: Any,
    *,
    config: Config | None = None,
    env: dict | None = None,
    clients: tuple[Any, Any] | None = None,
    now: datetime | None = None,
) -> dict:
    env = os.environ if env is None else env
    request_id = getattr(context, "aws_request_id", None) or str(uuid.uuid4())
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    if not region:
        raise RuntimeError("AWS_REGION is not set")
    try:
        config = load_config(env) if config is None else config
    except ValidationError as exc:
        # Misconfiguration is deterministic: log loudly, do not retry.
        log("error", "configuration invalid", request_id=request_id, reason=str(exc))
        raise

    expected_account = config.expected_account or account_from_context(context)
    if not expected_account:
        log("error", "cannot determine deployed account", request_id=request_id)
        raise RuntimeError("cannot determine deployed account from context or EXPECTED_ACCOUNT_ID")

    try:
        parsed = validate_event(event, expected_region=region, expected_account=expected_account)
    except ValidationError as exc:
        log("info", "event ignored", request_id=request_id, reason=str(exc),
            event_id=event.get("id") if isinstance(event, dict) else None)
        return {"status": "ignored", "reason": str(exc)}

    fields = {"request_id": request_id, "event_id": parsed.event_id, "instance_id": parsed.instance_id}
    now = datetime.now(timezone.utc) if now is None else now
    problem = event_age_problem(parsed.time, config, now)
    if problem:
        log("info", "event ignored", reason=problem, event_time=parsed.time.isoformat(), **fields)
        return {"status": "ignored", "reason": problem, "event_id": parsed.event_id, "instance_id": parsed.instance_id}
    log("info", "interruption warning received", event_time=parsed.time.isoformat(),
        deadline=parsed.deadline.isoformat(), age_s=int((now - parsed.time).total_seconds()),
        targets=len(config.targets), dry_run=config.dry_run, **fields)

    if clients is None:
        clients = make_clients(region)
    ec2, ssm = clients

    started = time.monotonic()
    try:
        instance = describe_instance(ec2, parsed.instance_id)
        check_instance_tags(instance, config.expected_tags)
    except ValidationError as exc:
        log("warning", "instance rejected", reason=str(exc), **fields)
        return {"status": "rejected", "reason": str(exc), **{k: v for k, v in fields.items() if k != "request_id"}}
    except Exception as exc:  # API error, throttle, timeout: fail closed and let the retry policy decide
        log("error", "DescribeInstances failed", error=type(exc).__name__, detail=str(exc), **fields)
        raise
    log("info", "instance verified", state=instance.get("State", {}).get("Name"),
        elapsed_ms=int((time.monotonic() - started) * 1000), **fields)

    request = render_send_command(config, parsed, comment=f"spot-handoff {parsed.event_id}")
    if config.dry_run == "lambda":
        log("info", "dry-run: SendCommand not issued", command_lines=len(request["Parameters"]["commands"]), **fields)
        return {"status": "dry-run", "request": request, **{k: v for k, v in fields.items() if k != "request_id"}}

    try:
        response = ssm.send_command(**request)
    except Exception as exc:
        # Includes InvalidInstanceId when the instance is already gone or not
        # managed by SSM (the termination race). A retry can still win when
        # the agent was merely slow to register.
        log("error", "SendCommand failed", error=type(exc).__name__, detail=str(exc), **fields)
        raise
    command_id = response.get("Command", {}).get("CommandId")
    log("info", "SendCommand accepted; execution not confirmed", command_id=command_id,
        document=request["DocumentName"], execution_timeout=config.execution_timeout,
        submission_timeout=config.submission_timeout,
        elapsed_ms=int((time.monotonic() - started) * 1000), **fields)
    return {
        "status": "submitted",
        "command_id": command_id,
        "instance_id": parsed.instance_id,
        "event_id": parsed.event_id,
        "note": "SSM accepted the command; check the invocation for the outcome",
    }


def lambda_handler(event: Any, context: Any) -> dict:
    return handle(event, context)
