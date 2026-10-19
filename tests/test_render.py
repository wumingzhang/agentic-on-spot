import json
import shlex
import unittest

import support  # noqa: F401,I001  sets sys.path for spot_handoff
from spot_handoff.config import load_config
from spot_handoff.render import (
    DOCUMENT_NAME,
    HEREDOC_DELIMITER,
    HOST_SCRIPT_PATH,
    load_host_script,
    render_argv,
    render_commands,
    render_invocation,
    render_send_command,
)
from spot_handoff.validation import ValidationError, validate_event
from support import ACCOUNT, EVENT_ID, HERMES_ARGV, HERMES_PROMPT, INSTANCE, REGION, TARGETS, valid_env, valid_event


def parsed():
    return validate_event(valid_event(), expected_region=REGION, expected_account=ACCOUNT)


class RenderTest(unittest.TestCase):
    def test_host_script_is_shipped_and_free_of_delimiter(self):
        self.assertTrue(HOST_SCRIPT_PATH.exists(), HOST_SCRIPT_PATH)
        lines = load_host_script()
        self.assertTrue(lines[0].startswith("#!"))
        self.assertNotIn(HEREDOC_DELIMITER, [l.strip() for l in lines])

    def test_invocation_preserves_hermes_argv_exactly(self):
        # Detached, user coder, argc 7, and the whole prompt as the seventh
        # word: the host script counts argv words, so a prompt split by a
        # shell or a newline would shift every word after it.
        args = render_invocation(load_config(valid_env()), parsed())
        i = args.index("--detached-target")
        self.assertEqual(args[i:i + 5], ["--detached-target", "hermes", "coder-example-hermes", "coder", "7"])
        self.assertEqual(args[i + 5:i + 12], HERMES_ARGV)
        self.assertEqual(args[i + 11], HERMES_PROMPT)
        self.assertEqual(args[i + 12], "--target", "nothing between the prompt and the next target")
        self.assertNotIn("\n", HERMES_PROMPT)

    def test_detached_is_per_target_and_off_by_default(self):
        # Only the target that asks for it is passed as --detached-target;
        # a config without the key renders exactly as before.
        targets = [{"name": "a", "container": "coder-a", "argv": ["x"], "detached": True},
                   {"name": "b", "container": "coder-b", "argv": ["y"]},
                   {"name": "c", "container": "coder-c", "argv": ["z"], "detached": False}]
        args = render_invocation(load_config(valid_env(TARGETS=json.dumps(targets))), parsed())
        keywords = [(w, args[k + 1]) for k, w in enumerate(args) if w in ("--target", "--detached-target")]
        self.assertEqual(keywords, [("--detached-target", "a"), ("--target", "b"), ("--target", "c")])
        legacy = [{k: v for k, v in t.items() if k != "detached"} for t in TARGETS]
        args = render_invocation(load_config(valid_env(TARGETS=json.dumps(legacy))), parsed())
        self.assertNotIn("--detached-target", args)
        self.assertEqual(args.count("--target"), 2)

    def test_yardr_target_runs_as_coder_with_placeholders_filled(self):
        args = render_invocation(load_config(valid_env()), parsed())
        j = len(args) - 1 - args[::-1].index("--target")
        self.assertEqual(args[j + 1:j + 5], ["yardr", "coder-example-yardr", "coder", "8"])
        message = args[-1]
        self.assertIn(INSTANCE, message)
        self.assertIn(EVENT_ID, message)
        self.assertIn("2026-10-18T12:02:00Z", message)  # time + 120s
        self.assertNotIn("{", message)
        self.assertLess(len(message.encode()), 4096, "yardr mail takes at most 4 KB")

    def test_unknown_or_broken_placeholders_rejected(self):
        for word in ("{secret}", "{instance_id", "{0}", "{instance_id.__class__}"):
            targets = [{"name": "t", "container": "coder-x", "argv": [word]}]
            config = load_config(valid_env(TARGETS=json.dumps(targets)))
            with self.assertRaises(ValidationError, msg=word):
                render_argv(config.targets[0], parsed())

    def test_quoting_survives_shell_metacharacters(self):
        nasty = ["sh", "-c", "echo $(id) `id` \"q\" 'q' ; rm -rf / & | > out \\ *"]
        targets = [{"name": "t", "container": "coder-x", "argv": nasty}]
        commands = render_commands(load_config(valid_env(TARGETS=json.dumps(targets))), parsed())
        invocation = commands[-2]
        self.assertTrue(invocation.startswith('bash "$tmp" '))
        words = shlex.split(invocation)
        self.assertEqual(words[words.index("--target") + 4:], ["3"] + nasty)

    def test_event_fields_are_quoted_not_interpreted(self):
        # Validation rejects such an id long before render; this pins that
        # even a hostile id would reach the host as one inert word.
        from datetime import datetime, timezone
        from spot_handoff.validation import InterruptionEvent
        evil_id = "x'; touch /pwned; echo '"
        evil = InterruptionEvent(event_id=evil_id, instance_id=INSTANCE, region=REGION, account=ACCOUNT,
                                 time=datetime(2026, 1, 1, tzinfo=timezone.utc))
        invocation = render_commands(load_config(valid_env()), evil)[-2]
        words = shlex.split(invocation)
        self.assertEqual(words[words.index("--event-id") + 1], evil_id)
        self.assertEqual(words.count("touch"), 0)

    def test_commands_structure(self):
        config = load_config(valid_env())
        commands = render_commands(config, parsed())
        self.assertEqual(commands[0], "set -u")
        self.assertIn(f"<<'{HEREDOC_DELIMITER}'", commands[2])
        self.assertEqual(commands[-3], HEREDOC_DELIMITER)
        self.assertTrue(commands[-1].startswith("rc=$?"))
        script = load_host_script()
        self.assertEqual(commands[3:3 + len(script)], script)
        self.assertLess(sum(len(c) + 1 for c in commands), 60 * 1024)

    def test_send_command_request(self):
        config = load_config(valid_env())
        request = render_send_command(config, parsed(), comment=f"spot-handoff {EVENT_ID}")
        self.assertEqual(request["DocumentName"], DOCUMENT_NAME)
        self.assertEqual(request["InstanceIds"], [INSTANCE])
        self.assertEqual(request["TimeoutSeconds"], 30)
        self.assertEqual(request["Parameters"]["executionTimeout"], ["90"])
        self.assertLessEqual(len(request["Comment"]), 100)
        self.assertEqual(set(request), {"DocumentName", "InstanceIds", "Parameters", "TimeoutSeconds", "Comment"})
        self.assertNotIn("Targets", request)

    def test_host_dry_run_flag(self):
        config = load_config(valid_env(DRY_RUN="host"))
        self.assertIn("--dry-run", render_invocation(config, parsed()))
        self.assertNotIn("--dry-run", render_invocation(load_config(valid_env()), parsed()))
        self.assertNotIn("--dry-run", render_invocation(load_config(valid_env(DRY_RUN="lambda")), parsed()))


if __name__ == "__main__":
    unittest.main()
