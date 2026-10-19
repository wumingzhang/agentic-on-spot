"""Turn a validated event plus the configured targets into the SSM command.

The AWS-RunShellScript document takes a list of shell lines. We send:

    set -u
    tmp=$(mktemp ...)
    cat >"$tmp" <<'SPOT_HANDOFF_SCRIPT_EOF'
    ... spot-handoff-notify.sh, verbatim ...
    SPOT_HANDOFF_SCRIPT_EOF
    bash "$tmp" --event-id '...' ... --target <name> <container> <user|-> <argc> <argv...> ...
    rc=$?; rm -f "$tmp"; exit $rc

A target with detached=true is passed as --detached-target with the same
shape; the host script starts that one with `docker exec -d` and reports it
as launched rather than delivered.

Nothing from the event reaches the shell unquoted: every word is validated
(validation.py) and then shlex.quote()d, and the host script re-validates and
never evals. The heredoc delimiter is quoted, so the script body is not
expanded by the SSM agent's shell either.
"""

from __future__ import annotations

import shlex
import string
from pathlib import Path

from .config import Config, Target
from .validation import ARGV_WORD_RE, InterruptionEvent, ValidationError

HOST_SCRIPT_PATH = Path(__file__).with_name("spot-handoff-notify.sh")
HEREDOC_DELIMITER = "SPOT_HANDOFF_SCRIPT_EOF"
DOCUMENT_NAME = "AWS-RunShellScript"


def load_host_script(path: Path = HOST_SCRIPT_PATH) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith("#!"):
        raise ValidationError(f"host script {path} missing or without shebang")
    if HEREDOC_DELIMITER in (line.strip() for line in lines):
        raise ValidationError("host script contains the heredoc delimiter")
    return lines


def placeholder_values(event: InterruptionEvent) -> dict[str, str]:
    """The only values an argv template may interpolate. All validated."""
    return {
        "instance_id": event.instance_id,
        "event_id": event.event_id,
        "event_time": event.time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deadline": event.deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def render_argv(target: Target, event: InterruptionEvent) -> list[str]:
    """Fill {placeholders} in argv words from placeholder_values() only.

    string.Formatter is used to parse, not to format: a field may be a bare
    name from the allowed set, with no attribute access ({x.__class__}), no
    indexing ({x[0]}), no conversion ({x!r}) and no format spec ({x:>10}).
    """
    values = placeholder_values(event)
    out = []
    for word in target.argv:
        pieces = []
        try:
            parsed = list(string.Formatter().parse(word))
        except ValueError as exc:
            raise ValidationError(f"target {target.name}: bad placeholder syntax in argv") from exc
        for literal, field_name, format_spec, conversion in parsed:
            pieces.append(literal)
            if field_name is None:
                continue
            if field_name not in values or format_spec or conversion:
                raise ValidationError(f"target {target.name}: unknown or decorated placeholder {{{field_name}}} in argv")
            pieces.append(values[field_name])
        rendered = "".join(pieces)
        if not ARGV_WORD_RE.match(rendered):
            raise ValidationError(f"target {target.name}: rendered argv word invalid")
        out.append(rendered)
    return out


def render_target_args(target: Target, event: InterruptionEvent) -> list[str]:
    argv = render_argv(target, event)
    keyword = "--detached-target" if target.detached else "--target"
    return [keyword, target.name, target.container, target.user or "-", str(len(argv)), *argv]


def render_invocation(config: Config, event: InterruptionEvent) -> list[str]:
    args = [
        "--event-id", event.event_id,
        "--instance-id", event.instance_id,
        "--deadline", placeholder_values(event)["deadline"],
        "--state-dir", config.state_dir,
        "--per-target-timeout", str(config.per_target_timeout),
        "--inspect-timeout", str(config.inspect_timeout),
    ]
    if config.dry_run == "host":
        args.append("--dry-run")
    for target in config.targets:
        args.extend(render_target_args(target, event))
    return args


def render_commands(config: Config, event: InterruptionEvent, script_lines: list[str] | None = None) -> list[str]:
    script_lines = load_host_script() if script_lines is None else script_lines
    invocation = " ".join(shlex.quote(a) for a in render_invocation(config, event))
    return [
        "set -u",
        'tmp=$(mktemp "${TMPDIR:-/tmp}/spot-handoff.XXXXXX") || exit 97',
        f"cat >\"$tmp\" <<'{HEREDOC_DELIMITER}'",
        *script_lines,
        HEREDOC_DELIMITER,
        f'bash "$tmp" {invocation}',
        'rc=$?; rm -f "$tmp"; exit $rc',
    ]


def render_send_command(config: Config, event: InterruptionEvent, *, comment: str) -> dict:
    """The exact keyword arguments for ssm.send_command."""
    return {
        "DocumentName": DOCUMENT_NAME,
        "InstanceIds": [event.instance_id],
        "Parameters": {
            "commands": render_commands(config, event),
            "executionTimeout": [str(config.execution_timeout)],
        },
        "TimeoutSeconds": config.submission_timeout,
        "Comment": comment[:100],
    }
