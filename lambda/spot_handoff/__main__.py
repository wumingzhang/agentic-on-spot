"""Operator helper: render what the Lambda would send, without AWS.

    python3 -m spot_handoff render --event sample-event.json [--env env.json]

Prints the ssm.send_command arguments (the host script inline) as JSON, or
with --shell the exact shell text SSM would run, which can be pasted into a
root shell on a host for a manual test (add DRY_RUN=host in env.json to make
the host script only print the docker commands).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .config import load_config
from .render import render_send_command
from .validation import ValidationError, validate_event


def env_from_file(path: str) -> dict[str, str]:
    """Environment overrides from a JSON object file, as Lambda would see them.

    Lambda variables are strings. A value already written as a string (a
    pre-encoded TARGETS, a number as "90") passes through unchanged; a JSON
    list or object, which the example env file uses for TARGETS and
    EXPECTED_TAGS, is encoded back to JSON text, not str()'d to a Python repr
    that load_config would reject.
    """
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValidationError(f"{path}: --env must be a JSON object")
    return {k: v if isinstance(v, str) else json.dumps(v) for k, v in raw.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m spot_handoff")
    sub = parser.add_subparsers(dest="cmd", required=True)
    render = sub.add_parser("render", help="render the SSM request for an event")
    render.add_argument("--event", required=True, help="path to an EventBridge event JSON file")
    render.add_argument("--env", help="JSON object of environment overrides (TARGETS, EXPECTED_TAGS, ...)")
    render.add_argument("--region", help="deployed region (default: AWS_REGION)")
    render.add_argument("--account", help="deployed account id (default: EXPECTED_ACCOUNT_ID)")
    render.add_argument("--shell", action="store_true", help="print the shell text instead of JSON")
    args = parser.parse_args(argv)

    try:
        env = dict(os.environ)
        if args.env:
            env.update(env_from_file(args.env))
        region = args.region or env.get("AWS_REGION")
        account = args.account or env.get("EXPECTED_ACCOUNT_ID")
        if not region or not account:
            raise ValidationError("need --region/--account or AWS_REGION/EXPECTED_ACCOUNT_ID")
        with open(args.event, encoding="utf-8") as fh:
            event = json.load(fh)
        config = load_config(env)
        parsed = validate_event(event, expected_region=region, expected_account=account)
    except ValidationError as exc:
        print(f"render: {exc}", file=sys.stderr)
        return 2
    request = render_send_command(config, parsed, comment=f"spot-handoff {parsed.event_id}")
    if args.shell:
        print("\n".join(request["Parameters"]["commands"]))
    else:
        print(json.dumps(request, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
