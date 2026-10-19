"""The documented render command works on the example files, in-process.

README Testing stage 1 tells an operator to run

    python3 -m spot_handoff render --event ../examples/basic/sample-event.json \
        --env ../examples/basic/render-env.example.json --region us-east-2 \
        --account 123456789012 --shell

The example env file writes TARGETS and EXPECTED_TAGS as JSON, not as
pre-encoded strings, so __main__ must turn them into the string variables
Lambda would see. These tests drive main() with those real files and pin the
rendered request to the fixtures in support.py.
"""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  puts lambda/ on sys.path
from spot_handoff.__main__ import env_from_file, main
from spot_handoff.config import load_config
from support import ACCOUNT, HERMES_ARGV, HERMES_PROMPT, HERMES_USER, INSTANCE, REGION, TAGS, TARGETS

EXAMPLES = Path(support.__file__).resolve().parent.parent / "examples" / "basic"
EVENT = EXAMPLES / "sample-event.json"
RENDER_ENV = EXAMPLES / "render-env.example.json"


def run_render(*extra: str, env: Path = RENDER_ENV) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(["render", "--event", str(EVENT), "--env", str(env), "--region", REGION, "--account", ACCOUNT, *extra])
    return rc, out.getvalue(), err.getvalue()


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="spot-handoff-cli."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write_env(self, obj) -> Path:
        path = self.tmp / "env.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return path

    def test_example_env_file_loads_as_the_configured_targets_and_tags(self):
        env = env_from_file(str(RENDER_ENV))
        self.assertTrue(all(isinstance(v, str) for v in env.values()), env)
        config = load_config({**env, "AWS_REGION": REGION})
        self.assertEqual(config.expected_tags, TAGS)
        # The Hermes target word for word (test_examples.py pins every
        # example to it); the yardr target by name, container, user and
        # binary, as its message wording is the example's own.
        hermes, yardr = config.targets
        self.assertEqual((hermes.name, hermes.container, hermes.user, hermes.detached, list(hermes.argv)),
                         ("hermes", "coder-example-hermes", HERMES_USER, True, HERMES_ARGV))
        self.assertEqual((yardr.name, yardr.container, yardr.user, yardr.detached, yardr.argv[0]),
                         ("yardr", "coder-example-yardr", "coder", False, "/home/coder/.local/bin/yardr"))
        self.assertEqual(config.dry_run, "host")

    def test_pre_encoded_strings_pass_through_unchanged(self):
        # An env file written the way Lambda stores variables, every value a
        # string, is used as written: no double encoding.
        pre = {"TARGETS": json.dumps(TARGETS), "EXPECTED_TAGS": json.dumps(TAGS), "EXECUTION_TIMEOUT": "120"}
        self.assertEqual(env_from_file(str(self.write_env(pre))), pre)
        # The non-string spelling of the same numbers and flags encodes to the
        # text load_config parses, not to a Python repr.
        self.assertEqual(env_from_file(str(self.write_env({"EXECUTION_TIMEOUT": 120, "EXPECTED_TAGS": {"A": "b"}}))),
                         {"EXECUTION_TIMEOUT": "120", "EXPECTED_TAGS": '{"A": "b"}'})

    def test_malformed_pre_encoded_targets_fail_with_the_reason(self):
        rc, out, err = run_render(env=self.write_env({"TARGETS": "[{'name': 'x'}]", "EXPECTED_TAGS": json.dumps(TAGS)}))
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("TARGETS/EXPECTED_TAGS are not valid JSON", err)

    def test_env_file_that_is_not_an_object_fails_with_the_reason(self):
        # Through the CLI, as the other failures: a reason on stderr and exit
        # 2, not a traceback.
        path = self.write_env([1, 2])
        rc, out, err = run_render(env=path)
        self.assertEqual((rc, out), (2, ""))
        self.assertIn(f"render: {path}: --env must be a JSON object", err)


class RenderCommandTest(unittest.TestCase):
    def test_documented_shell_render_carries_both_example_targets(self):
        rc, out, err = run_render("--shell")
        self.assertEqual((rc, err), (0, ""), err)
        invocation = [line for line in out.splitlines() if line.startswith('bash "$tmp" ')]
        self.assertEqual(len(invocation), 1, out[-500:])
        line = invocation[0]
        self.assertIn(f"--instance-id {INSTANCE} ", line)
        # DRY_RUN=host in the example env makes the host script only print.
        self.assertIn(" --dry-run ", line)
        # Hermes detached, yardr synchronous: the keyword is the only difference.
        self.assertIn(f"--detached-target hermes coder-example-hermes {HERMES_USER} {len(HERMES_ARGV)} "
                      + " ".join(HERMES_ARGV[:-1]) + f" '{HERMES_PROMPT}'", line)
        self.assertIn("--target yardr coder-example-yardr coder 8 /home/coder/.local/bin/yardr ", line)
        self.assertEqual(line.count("--detached-target"), 1)

    def test_documented_json_render_is_the_send_command_request(self):
        rc, out, err = run_render()
        self.assertEqual((rc, err), (0, ""), err)
        request = json.loads(out)
        self.assertEqual(request["DocumentName"], "AWS-RunShellScript")
        self.assertEqual(request["InstanceIds"], [INSTANCE])
        self.assertEqual(request["Parameters"]["executionTimeout"], ["90"])
        self.assertIn(HERMES_PROMPT, "\n".join(request["Parameters"]["commands"]))


if __name__ == "__main__":
    unittest.main()
