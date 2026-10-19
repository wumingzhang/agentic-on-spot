"""Configuration from the Lambda environment.

TARGETS is a JSON list of objects, each describing one container to notify:

    {"name": "hermes", "container": "coder-example-hermes", "user": "coder", "detached": true,
     "argv": ["/home/coder/.local/bin/hermes", "chat", "--oneshot", "--run-budget", "45", "-q",
              "AWS Spot interruption: this host is about to be terminated. Create a recovery note in ..."]}
    {"name": "yardr", "container": "coder-example-yardr", "user": "coder",
     "argv": ["/home/coder/.local/bin/yardr", "--home", "/home/coder/.yardr",
              "--actor", "example-operator", "mail", "yardmaster", "Spot interruption {instance_id} ..."]}

argv words may use the placeholders {instance_id}, {event_id}, {event_time},
{deadline}; they are filled with validated values only (see render.py).

"detached" (optional, default false) makes the host script start the command
with `docker exec -d` and report it as launched as soon as docker has started
the process, instead of waiting for it to exit. The per-target timeout then
bounds the launch only, not the process. It exists for a command whose own
run outlives the host's window (the Hermes oneshot did in a live test: the
note was written, but the synchronous exec was killed at 60 s); a target
whose exit status is the evidence (yardr mail) stays synchronous.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .validation import (
    ACCOUNT_RE,
    ARGV_WORD_RE,
    CONTAINER_RE,
    MAX_ARGV_WORDS,
    TARGET_NAME_RE,
    USER_RE,
    ValidationError,
    validate_tags,
)

# Control-plane containers that must never receive an exec, whatever the
# operator configures. "coder" is the Coder control server in the Compose
# stack this was written for; a workspace is never named that.
DEFAULT_FORBIDDEN_CONTAINERS = ("coder",)

DRY_RUN_MODES = ("off", "lambda", "host")

STATE_DIR_RE = re.compile(r"^/[A-Za-z0-9_./-]{1,200}$")


@dataclass(frozen=True)
class Target:
    name: str
    container: str
    argv: tuple[str, ...]
    user: str | None = None
    detached: bool = False


@dataclass(frozen=True)
class Config:
    targets: tuple[Target, ...]
    expected_tags: dict[str, str]
    state_dir: str = "/data/spot-handoff"
    # SSM AWS-RunShellScript bounds. executionTimeout is for the whole host
    # script; per_target_timeout is the docker exec bound inside it. Targets
    # run in parallel, so per_target + inspect + kill grace must fit inside
    # execution_timeout, not their sum.
    execution_timeout: int = 90
    submission_timeout: int = 30
    per_target_timeout: int = 60
    inspect_timeout: int = 10
    dry_run: str = "off"
    # TTL for a notice, measured from the event's own `time`. A Spot notice
    # is useless once the two-minute window has passed, so a replayed or
    # delayed event older than this is ignored rather than acted on. 0 turns
    # the check off. Terraform sets the same value on the EventBridge target
    # and Lambda async retry policies, so the three bounds agree.
    max_event_age: int = 180
    # Clock skew tolerated for an event timestamp ahead of the Lambda's clock.
    max_future_skew: int = 300
    forbidden_containers: tuple[str, ...] = DEFAULT_FORBIDDEN_CONTAINERS
    expected_account: str | None = None
    extra: dict = field(default_factory=dict)

    def validate(self) -> "Config":
        if not self.targets:
            raise ValidationError("TARGETS is empty")
        names = [t.name for t in self.targets]
        if len(set(names)) != len(names):
            raise ValidationError("TARGETS names must be unique")
        if self.dry_run not in DRY_RUN_MODES:
            raise ValidationError(f"DRY_RUN must be one of {DRY_RUN_MODES}")
        if not 30 <= self.submission_timeout <= 2592000:
            raise ValidationError("SUBMISSION_TIMEOUT must be within SSM's TimeoutSeconds range (30..2592000)")
        if not 1 <= self.per_target_timeout <= 9999 or not 1 <= self.inspect_timeout <= 9999:
            raise ValidationError("per-target and inspect timeouts must be 1..9999 seconds")
        worst_case = self.per_target_timeout + self.inspect_timeout + 2 * 5  # two kill-after graces
        if worst_case >= self.execution_timeout:
            raise ValidationError(
                f"PER_TARGET_TIMEOUT+INSPECT_TIMEOUT+grace ({worst_case}s) must stay below EXECUTION_TIMEOUT ({self.execution_timeout}s)"
            )
        if self.max_event_age < 0 or self.max_event_age > 86400 or self.max_future_skew < 0:
            raise ValidationError("MAX_EVENT_AGE must be 0 (off) or 1..86400 seconds; MAX_FUTURE_SKEW must be >= 0")
        if not STATE_DIR_RE.match(self.state_dir):
            raise ValidationError("STATE_DIR must be an absolute path of [A-Za-z0-9_./-]")
        if self.expected_account is not None and not ACCOUNT_RE.match(self.expected_account):
            # A typo here would silently ignore every event; fail loudly instead.
            raise ValidationError("EXPECTED_ACCOUNT_ID must be a 12-digit account id")
        return self


def parse_target(raw: object, forbidden: tuple[str, ...]) -> Target:
    if not isinstance(raw, dict):
        raise ValidationError("target is not an object")
    name = raw.get("name")
    if not isinstance(name, str) or not TARGET_NAME_RE.match(name):
        raise ValidationError(f"target name malformed: {str(name)[:40]!r}")
    container = raw.get("container")
    if not isinstance(container, str) or not CONTAINER_RE.match(container):
        raise ValidationError(f"target {name}: container name malformed")
    if container in forbidden:
        raise ValidationError(f"target {name}: container {container!r} is a control-plane container")
    user = raw.get("user")
    if user is not None and (not isinstance(user, str) or not USER_RE.match(user)):
        raise ValidationError(f"target {name}: user malformed")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV_WORDS:
        raise ValidationError(f"target {name}: argv must be a list of 1..{MAX_ARGV_WORDS} words")
    for word in argv:
        if not isinstance(word, str) or not ARGV_WORD_RE.match(word):
            raise ValidationError(f"target {name}: argv word is empty, too long or contains control characters")
    # Only a JSON boolean: "true", 1 or null would be a guess about intent,
    # and the difference between waiting for the command and not is the
    # difference between "delivered" and "launched" in the report.
    detached = raw.get("detached", False)
    if not isinstance(detached, bool):
        raise ValidationError(f"target {name}: detached must be true or false")
    unknown = set(raw) - {"name", "container", "user", "argv", "detached"}
    if unknown:
        raise ValidationError(f"target {name}: unknown keys {sorted(unknown)}")
    return Target(name=name, container=container, argv=tuple(argv), user=user, detached=detached)


def _int_env(env: dict, key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationError(f"{key} must be an integer") from exc


def load_config(env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    try:
        raw_targets = json.loads(env.get("TARGETS", ""))
        raw_tags = json.loads(env.get("EXPECTED_TAGS", ""))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"TARGETS/EXPECTED_TAGS are not valid JSON: {exc.msg}") from exc
    if not isinstance(raw_targets, list):
        raise ValidationError("TARGETS must be a JSON list")
    forbidden_raw = env.get("FORBIDDEN_CONTAINERS")
    forbidden = DEFAULT_FORBIDDEN_CONTAINERS if forbidden_raw is None else tuple(
        p for p in (s.strip() for s in forbidden_raw.split(",")) if p
    )
    targets = tuple(parse_target(t, forbidden) for t in raw_targets)
    return Config(
        targets=targets,
        expected_tags=validate_tags(raw_tags),
        state_dir=env.get("STATE_DIR", "/data/spot-handoff") or "/data/spot-handoff",
        execution_timeout=_int_env(env, "EXECUTION_TIMEOUT", 90),
        submission_timeout=_int_env(env, "SUBMISSION_TIMEOUT", 30),
        per_target_timeout=_int_env(env, "PER_TARGET_TIMEOUT", 60),
        inspect_timeout=_int_env(env, "INSPECT_TIMEOUT", 10),
        dry_run=(env.get("DRY_RUN", "off") or "off").lower(),
        max_event_age=_int_env(env, "MAX_EVENT_AGE", 180),
        max_future_skew=_int_env(env, "MAX_FUTURE_SKEW", 300),
        forbidden_containers=forbidden,
        expected_account=env.get("EXPECTED_ACCOUNT_ID") or None,
    ).validate()
