"""Shared fixtures: a valid event, a valid environment, fake AWS clients."""

from __future__ import annotations

import copy
import json

import _bootstrap  # noqa: F401  puts lambda/ on sys.path before spot_handoff is imported

REGION = "us-east-2"
ACCOUNT = "123456789012"
INSTANCE = "i-0123456789abcdef0"
EVENT_ID = "12345678-1234-1234-1234-123456789012"

# The owner-confirmed Hermes oneshot command (README, Configuration). The
# prompt was supplied as several lines; argv words cannot carry a newline, so
# it travels as ONE single-line argument, the lines joined with spaces. Every
# example config in the repository carries exactly these words (see
# test_examples.py), and the argv runs as user coder, detached: the live test
# showed the oneshot writes its note but outlives the host's 60 s exec bound,
# so the host starts it with `docker exec -d` and reports "launched".
HERMES_PROMPT = (
    "AWS Spot interruption: this host is about to be terminated. "
    "Create a recovery note in /home/coder/.hermes/handoffs/spot-latest.md. "
    "Inspect persisted recent sessions and task records. "
    "Record: - active or incomplete task IDs and artifact paths; - last verified progress; - next actions; "
    "- uncertain external side effects that must be checked before retrying. "
    "Do not start implementation, deploy, send messages, or change task status. "
    "Do not claim other running agents have checkpointed unless verified. "
    "If time is short, save a partial note immediately."
)
HERMES_RUN_BUDGET = 45
HERMES_BIN = "/home/coder/.local/bin/hermes"
HERMES_ARGV = [HERMES_BIN, "chat", "--oneshot", "--run-budget", str(HERMES_RUN_BUDGET), "-q", HERMES_PROMPT]
HERMES_USER = "coder"
HERMES_DETACHED = True
YARDR_ARGV = [
    "/home/coder/.local/bin/yardr", "--home", "/home/coder/.yardr", "--actor", "example-operator",
    "mail", "yardmaster",
    "Spot interruption: instance {instance_id} terminates about {deadline} (event {event_id}). "
    "Put all tasks into handoff mode now.",
]

TARGETS = [
    {"name": "hermes", "container": "coder-example-hermes", "user": HERMES_USER, "argv": HERMES_ARGV,
     "detached": HERMES_DETACHED},
    {"name": "yardr", "container": "coder-example-yardr", "user": "coder", "argv": YARDR_ARGV},
]
TAGS = {"Role": "example-workspace-host", "Environment": "example"}


def valid_event(**overrides) -> dict:
    event = {
        "version": "0",
        "id": EVENT_ID,
        "detail-type": "EC2 Spot Instance Interruption Warning",
        "source": "aws.ec2",
        "account": ACCOUNT,
        "time": "2026-10-18T12:00:00Z",
        "region": REGION,
        "resources": [f"arn:aws:ec2:{REGION}b:instance/{INSTANCE}"],
        "detail": {"instance-id": INSTANCE, "instance-action": "terminate"},
    }
    event.update(overrides)
    return event


def valid_env(**overrides) -> dict:
    env = {
        "AWS_REGION": REGION,
        "TARGETS": json.dumps(TARGETS),
        "EXPECTED_TAGS": json.dumps(TAGS),
    }
    env.update(overrides)
    return env


def instance_description(tags: dict | None = None, instance_id: str = INSTANCE, state: str = "running") -> dict:
    tags = TAGS if tags is None else tags
    return {
        "Reservations": [{"Instances": [{
            "InstanceId": instance_id,
            "State": {"Name": state},
            "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
        }]}]
    }


class FakeContext:
    def __init__(self, account: str = ACCOUNT, request_id: str = "req-1"):
        self.invoked_function_arn = f"arn:aws:lambda:{REGION}:{account}:function:spot-handoff"
        self.aws_request_id = request_id


class FakeClientError(Exception):
    """Stands in for botocore.exceptions.ClientError (boto3 is not installed here)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeEC2:
    def __init__(self, response: dict | Exception | None = None):
        self.response = instance_description() if response is None else response
        self.calls: list[dict] = []

    def describe_instances(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return copy.deepcopy(self.response)


class FakeSSM:
    def __init__(self, error: Exception | None = None, command_id: str = "cmd-0001"):
        self.error = error
        self.command_id = command_id
        self.calls: list[dict] = []

    def send_command(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if self.error is not None:
            raise self.error
        return {"Command": {"CommandId": self.command_id, "Status": "Pending"}}
