"""Strict validation of the EventBridge event and of configured values.

Everything that ends up in a shell command line or in a log line passes
through here first. The rules are deliberately narrow: an EC2 instance id, an
EventBridge event id, a docker container name and a unix user name all have a
small, well-known alphabet, and anything outside it is rejected rather than
quoted around.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EXPECTED_SOURCE = "aws.ec2"
EXPECTED_DETAIL_TYPE = "EC2 Spot Instance Interruption Warning"
EXPECTED_ACTION = "terminate"

# Spot interruption notices arrive about two minutes before termination
# (best effort). The deadline passed to the host is informational only.
NOTICE_WINDOW = timedelta(seconds=120)

INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]{8}([0-9a-f]{9})?$")
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
ACCOUNT_RE = re.compile(r"^\d{12}$")
TARGET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}(:[A-Za-z0-9_.-]{1,64})?$")
TAG_KEY_RE = re.compile(r"^[\w .:/=+@-]{1,128}$")
TAG_VALUE_RE = re.compile(r"^[\w .:/=+@-]{0,256}$")
# A single argv word for docker exec: printable, no NUL/newline/unit separator
# (the host script uses 0x1f internally to carry argv between its phases).
ARGV_WORD_RE = re.compile(r"^[^\x00-\x08\x0a-\x1f\x7f]{1,2048}$")
MAX_ARGV_WORDS = 64


class ValidationError(ValueError):
    """The event or the configuration is not acceptable. Never retried."""


@dataclass(frozen=True)
class InterruptionEvent:
    event_id: str
    instance_id: str
    region: str
    account: str
    time: datetime

    @property
    def deadline(self) -> datetime:
        return self.time + NOTICE_WINDOW


def _require_str(mapping: dict, key: str, what: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{what}.{key} missing or not a string")
    return value


def parse_time(value: str) -> datetime:
    """Parse EventBridge's ISO-8601 time ("2026-10-18T12:34:56Z")."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"event.time not ISO-8601: {value[:40]!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_event(event: object, *, expected_region: str, expected_account: str) -> InterruptionEvent:
    """Accept exactly one shape of event; raise ValidationError for anything else.

    Region and account are compared with the deployed context so a rule
    forwarding events across accounts or regions can never make this Lambda
    issue commands for a fleet it was not deployed for.
    """
    if not isinstance(event, dict):
        raise ValidationError("event is not an object")
    source = _require_str(event, "source", "event")
    if source != EXPECTED_SOURCE:
        raise ValidationError(f"unexpected source {source[:40]!r}")
    detail_type = _require_str(event, "detail-type", "event")
    if detail_type != EXPECTED_DETAIL_TYPE:
        raise ValidationError(f"unexpected detail-type {detail_type[:60]!r}")
    detail = event.get("detail")
    if not isinstance(detail, dict):
        raise ValidationError("event.detail missing or not an object")
    action = _require_str(detail, "instance-action", "event.detail")
    if action != EXPECTED_ACTION:
        raise ValidationError(f"unsupported instance-action {action[:20]!r}")
    instance_id = _require_str(detail, "instance-id", "event.detail")
    if not INSTANCE_ID_RE.match(instance_id):
        raise ValidationError("event.detail.instance-id is not an EC2 instance id")
    event_id = _require_str(event, "id", "event")
    if not EVENT_ID_RE.match(event_id):
        raise ValidationError("event.id malformed")
    region = _require_str(event, "region", "event")
    if region != expected_region:
        raise ValidationError(f"event.region {region[:20]!r} is not the deployed region")
    account = _require_str(event, "account", "event")
    if account != expected_account:
        raise ValidationError("event.account is not the deployed account")
    # Resources, when present, must name the same instance; a mismatch is a
    # malformed or tampered event.
    resources = event.get("resources")
    if resources is not None:
        if not isinstance(resources, list) or not all(isinstance(r, str) for r in resources):
            raise ValidationError("event.resources malformed")
        if resources and not any(r.endswith(f":instance/{instance_id}") for r in resources):
            raise ValidationError("event.resources does not name event.detail.instance-id")
    time = parse_time(_require_str(event, "time", "event"))
    return InterruptionEvent(event_id=event_id, instance_id=instance_id, region=region, account=account, time=time)


def validate_tags(expected: object) -> dict[str, str]:
    if not isinstance(expected, dict) or not expected:
        raise ValidationError("expected_tags must be a non-empty object")
    out: dict[str, str] = {}
    for key, value in expected.items():
        if not isinstance(key, str) or not TAG_KEY_RE.match(key):
            raise ValidationError(f"bad tag key {str(key)[:40]!r}")
        if not isinstance(value, str) or not TAG_VALUE_RE.match(value):
            raise ValidationError(f"bad tag value for {key!r}")
        out[key] = value
    return out


def check_instance_tags(instance: dict, expected: dict[str, str]) -> None:
    """Every expected tag must be present with exactly the expected value."""
    actual = {t.get("Key"): t.get("Value") for t in instance.get("Tags", []) if isinstance(t, dict)}
    missing = [k for k in expected if k not in actual]
    if missing:
        raise ValidationError(f"instance lacks expected tags: {sorted(missing)}")
    wrong = [k for k, v in expected.items() if actual.get(k) != v]
    if wrong:
        raise ValidationError(f"instance tag values differ from expected: {sorted(wrong)}")
