"""The example configurations carry the agreed commands, and deliver them.

Every example in the repository (Terraform tfvars, the render env file, the
CloudFormation parameters, the README's manual host run) is a hand-edited
copy of the same Hermes and yardr commands. These tests pin them to the
fixtures in support.py and then push both machine-readable formats through
the real path: load_config, render_invocation and the host script under a
fake docker, asserting what `docker exec` would receive, word for word.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  puts lambda/ on sys.path
from spot_handoff.__main__ import env_from_file
from spot_handoff.config import load_config
from spot_handoff.render import render_invocation
from spot_handoff.validation import validate_event
from support import ACCOUNT, HERMES_ARGV, HERMES_BIN, HERMES_DETACHED, HERMES_PROMPT, HERMES_RUN_BUDGET, HERMES_USER, REGION, valid_event
from test_host_script import FAKE_DOCKER, SCRIPT

REPO_ROOT = Path(support.__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cfn_check  # noqa: E402

TFVARS = REPO_ROOT / "examples" / "basic" / "terraform.tfvars.example"
RENDER_ENV = REPO_ROOT / "examples" / "basic" / "render-env.example.json"
README = REPO_ROOT / "README.md"


def terraform_env() -> dict:
    """The Lambda environment the Terraform example would deploy, from the
    render env file, which carries the same targets as the tfvars."""
    return env_from_file(str(RENDER_ENV))


def cloudformation_env() -> dict:
    """The Lambda environment the CloudFormation example parameters deploy."""
    problems, ev = cfn_check.run_checks(
        json.loads(cfn_check.TEMPLATE_PATH.read_text(encoding="utf-8")),
        cfn_check.load_parameters(cfn_check.EXAMPLE_PARAMETERS_PATH), region=REGION, account=ACCOUNT)
    assert problems == [], problems
    return ev.lambda_env()


class ExampleFilesTest(unittest.TestCase):
    def test_every_example_carries_the_fixture_hermes_command(self):
        # The tfvars line is written in the JSON list spelling, which HCL
        # shares, so the whole argv can be pinned without an HCL parser.
        self.assertIn(f"user      = {json.dumps(HERMES_USER)}", TFVARS.read_text(encoding="utf-8"))
        self.assertIn(f"detached  = {json.dumps(HERMES_DETACHED)}", TFVARS.read_text(encoding="utf-8"))
        self.assertIn(f"argv      = {json.dumps(HERMES_ARGV)}", TFVARS.read_text(encoding="utf-8"))
        self.assertIn(HERMES_PROMPT, README.read_text(encoding="utf-8"))
        self.assertIn(" ".join(HERMES_ARGV[:-1]) + f" '{HERMES_PROMPT}'", README.read_text(encoding="utf-8"),
                      "the README's manual host run does not quote the prompt as one argument")
        # The manual host run opts Hermes in the same way the deployed configs do.
        self.assertIn(f"--detached-target hermes coder-example-hermes {HERMES_USER} {len(HERMES_ARGV)} {HERMES_BIN} ",
                      README.read_text(encoding="utf-8"))
        self.assertTrue(HERMES_BIN.startswith("/"), "the Hermes binary is named by absolute path inside the container")

    def test_prompt_is_one_single_line_word(self):
        # The prompt was supplied as several lines and travels joined with
        # spaces: the validation rejects control characters on purpose, and
        # the host script counts argv words.
        self.assertNotRegex(HERMES_PROMPT, r"[\x00-\x1f\x7f]")
        self.assertEqual(HERMES_ARGV.count(HERMES_PROMPT), 1)
        self.assertEqual(HERMES_ARGV[HERMES_ARGV.index("-q") + 1], HERMES_PROMPT)
        self.assertEqual(len(HERMES_ARGV), 7)

    def test_both_formats_load_the_same_hermes_target(self):
        for label, env in (("terraform", terraform_env()), ("cloudformation", cloudformation_env())):
            config = load_config({**env, "AWS_REGION": REGION})
            hermes = config.targets[0]
            self.assertEqual((hermes.name, hermes.container, hermes.user), ("hermes", "coder-example-hermes", "coder"), label)
            self.assertEqual(hermes.argv, tuple(HERMES_ARGV), label)
            # Hermes is launched detached: the host does not wait for the
            # oneshot, so --run-budget is Hermes's own bound and the host's
            # per-target timeout bounds only the launch. yardr stays
            # synchronous, its exit status being the evidence of the mail.
            self.assertTrue(hermes.detached, label)
            self.assertFalse(config.targets[1].detached, label)
            self.assertEqual(int(hermes.argv[hermes.argv.index("--run-budget") + 1]), HERMES_RUN_BUDGET, label)


class ExampleDeliveryTest(unittest.TestCase):
    """The rendered invocation of each example, run through the host script
    with a fake docker that records every exec it receives."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="spot-handoff-examples."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.exec_log = self.tmp / "exec.log"

    def deliver(self, env: dict) -> dict[str, list[str]]:
        config = load_config({**env, "AWS_REGION": REGION, "STATE_DIR": str(self.tmp / "state"), "DRY_RUN": "off"})
        event = validate_event(valid_event(), expected_region=REGION, expected_account=ACCOUNT)
        args = render_invocation(config, event)
        system = ":".join(p for p in ("/usr/bin", "/bin") if os.path.isdir(p))
        proc = subprocess.run(["bash", str(SCRIPT), "--retention-days", "0", *args], capture_output=True, text=True, timeout=60,
                              env={"PATH": f"{self.bin}:{system}", "HOME": str(self.tmp), "TMPDIR": str(self.tmp),
                                   "FAKE_LOG": str(self.exec_log),
                                   "FAKE_RUNNING": ",".join(t.container for t in config.targets)})
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.lines = [json.loads(l) for l in proc.stdout.splitlines() if l.startswith("{")]
        records = [[w.decode() for w in f.read_bytes().split(b"\0") if w]
                   for f in sorted(Path(str(self.exec_log) + ".d").glob("*.rec"))]
        return {r[2]: r for r in records}

    def assert_hermes_exec(self, execs: dict[str, list[str]], label: str):
        # user=coder, detached, the container, then exactly the seven argv
        # words: the prompt reaches Hermes as one argument, nothing added,
        # nothing split. yardr runs attached, as before.
        self.assertEqual(execs["coder-example-hermes"], [f"user={HERMES_USER}", "detached=1", "coder-example-hermes", *HERMES_ARGV], label)
        self.assertEqual(execs["coder-example-hermes"][-1], HERMES_PROMPT, label)
        self.assertEqual(len(execs["coder-example-hermes"]), 3 + 7, label)
        self.assertEqual(execs["coder-example-yardr"][:4], ["user=coder", "detached=0", "coder-example-yardr", "/home/coder/.local/bin/yardr"], label)

    def assert_truthful_report(self, label: str):
        # Hermes is reported launched, yardr delivered; the summary keeps them apart.
        status = {l["target"]: l["status"] for l in self.lines if l.get("type") == "target"}
        self.assertEqual(status, {"hermes": "launched", "yardr": "delivered"}, label)
        summary = next(l for l in self.lines if l.get("type") == "summary")
        self.assertEqual((summary["delivered"], summary["launched"], summary["failed"]), (1, 1, 0), label)

    def test_terraform_example_launches_the_hermes_oneshot_detached_as_coder(self):
        self.assert_hermes_exec(self.deliver(terraform_env()), "terraform")
        self.assert_truthful_report("terraform")

    def test_cloudformation_example_launches_the_hermes_oneshot_detached_as_coder(self):
        self.assert_hermes_exec(self.deliver(cloudformation_env()), "cloudformation")
        self.assert_truthful_report("cloudformation")


if __name__ == "__main__":
    unittest.main()
