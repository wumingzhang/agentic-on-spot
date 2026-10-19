"""The CloudFormation template keeps parity with the Terraform module.

Parity is checked by evaluating the template's intrinsics for a parameter
set (scripts/cfn_check.py) and running the result through the Lambda's own
load_config/render, exactly what the deployed function would do with that
environment, plus structural assertions on IAM, EventBridge and the TTL
bounds. The packager is checked for reproducibility and content.
"""

import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import support  # noqa: F401  puts lambda/ on sys.path

REPO_ROOT = Path(support.__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cfn_check  # noqa: E402

REGION, ACCOUNT = "us-east-2", "123456789012"


def load_packager():
    # The file name carries a hyphen, so import it by path.
    spec = importlib.util.spec_from_file_location("package_lambda", SCRIPTS / "package-lambda.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def template() -> dict:
    return json.loads(cfn_check.TEMPLATE_PATH.read_text(encoding="utf-8"))


def example_parameters(**overrides) -> dict:
    params = cfn_check.load_parameters(cfn_check.EXAMPLE_PARAMETERS_PATH)
    params.update(overrides)
    return params


def check(**overrides):
    return cfn_check.run_checks(template(), example_parameters(**overrides), region=REGION, account=ACCOUNT)


class TemplateTest(unittest.TestCase):
    def test_example_parameters_pass_every_check(self):
        problems, ev = check()
        self.assertEqual(problems, [])
        self.assertIsNotNone(ev)

    def test_defaults_are_the_safe_rollout_start(self):
        params = template()["Parameters"]
        self.assertEqual(params["DryRun"]["Default"], "lambda")
        self.assertEqual(params["ForbiddenContainers"]["Default"], "coder")
        self.assertEqual(params["StateDir"]["Default"], "/data/spot-handoff")
        self.assertEqual((params["ExecutionTimeout"]["Default"], params["SubmissionTimeout"]["Default"],
                          params["PerTargetTimeout"]["Default"], params["InspectTimeout"]["Default"]), (90, 30, 60, 10))
        self.assertEqual((params["MaxEventAgeSeconds"]["Default"], params["MaxRetryAttempts"]["Default"],
                          params["LogRetentionDays"]["Default"]), (180, 2, 30))
        self.assertEqual(params["SubmissionTimeout"]["MinValue"], 30)
        # The parameters an owner must type: nothing deployment-specific has a default.
        required = sorted(name for name, spec in params.items() if "Default" not in spec)
        self.assertEqual(required, ["ExpectedTag1Key", "ExpectedTag1Value", "LambdaCodeS3Bucket", "Targets"])

    def test_nothing_private_in_the_public_tree(self):
        for path in (cfn_check.TEMPLATE_PATH, cfn_check.EXAMPLE_PARAMETERS_PATH):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\b\d{12}\b", f"{path.name} carries a 12-digit number")
            self.assertNotRegex(text, r"coder-(?!OWNER|example)[a-z0-9-]+", f"{path.name} names a non-example container")

    def test_lambda_env_round_trips_through_the_lambdas_own_config(self):
        _, ev = check()
        config = cfn_check.load_config({**ev.lambda_env(), "AWS_REGION": REGION})
        self.assertEqual([t.name for t in config.targets], ["hermes", "yardr"])
        self.assertEqual(config.targets[0].user, "coder")
        self.assertEqual(config.targets[0].argv, tuple(support.HERMES_ARGV))
        self.assertTrue(config.targets[0].detached)
        self.assertEqual(config.targets[1].user, "coder")
        self.assertFalse(config.targets[1].detached)
        self.assertEqual(config.expected_tags, {"Role": "example-workspace-host", "Environment": "example"})
        self.assertEqual(config.dry_run, "lambda")
        self.assertEqual(config.expected_account, ACCOUNT)
        self.assertEqual((config.execution_timeout, config.max_event_age), (90, 180))

    def test_iam_conditions_follow_the_tag_slots(self):
        # One tag, two tags, four tags: the IAM StringEquals block and the
        # Lambda's EXPECTED_TAGS always carry exactly the same keys and values.
        cases = [
            (1, {"ExpectedTag2Key": "", "ExpectedTag2Value": ""}),
            (2, {}),
            (4, {"ExpectedTag3Key": "Team", "ExpectedTag3Value": "platform", "ExpectedTag4Key": "Tier", "ExpectedTag4Value": ""}),
        ]
        for count, overrides in cases:
            problems, ev = check(**overrides)
            self.assertEqual(problems, [], overrides)
            tags = json.loads(ev.lambda_env()["EXPECTED_TAGS"])
            condition = ev.statements()["SendCommandToTaggedInstances"]["Condition"]["StringEquals"]
            self.assertEqual(len(tags), count, overrides)
            self.assertEqual(condition, {f"ssm:resourceTag/{k}": v for k, v in tags.items()}, overrides)

    def test_tag_slot_rules(self):
        problems, _ = check(ExpectedTag3Value="orphan")
        self.assertTrue(any("ExpectedTag3Value is set but ExpectedTag3Key is empty" in p for p in problems), problems)
        problems, _ = check(ExpectedTag2Key="Role")
        self.assertTrue(any("same key" in p for p in problems), problems)
        problems, _ = check(ExpectedTag3Key="Environment", ExpectedTag3Value="x")
        self.assertTrue(any("ExpectedTag2Key and ExpectedTag3Key" in p for p in problems), problems)

    def test_tag_values_cannot_break_out_of_the_json(self):
        for bad in ('x"}', "a\\b", "${AWS::AccountId}", "it's"):
            problems, _ = check(ExpectedTag1Value=bad)
            self.assertTrue(any("ExpectedTag1Value" in p and "AllowedPattern" in p for p in problems), (bad, problems))

    def test_timeout_budget_is_refused_before_deployment(self):
        problems, _ = check(PerTargetTimeout="80")
        self.assertTrue(any("EXECUTION_TIMEOUT" in p for p in problems), problems)
        problems, _ = check(PerTargetTimeout="80", ExecutionTimeout="120")
        self.assertEqual(problems, [])
        problems, _ = check(SubmissionTimeout="10")
        self.assertTrue(any("MinValue" in p for p in problems), problems)

    def test_forbidden_container_and_bad_targets_are_refused(self):
        targets = json.loads(example_parameters()["Targets"])
        targets[0]["container"] = "coder"
        problems, _ = check(Targets=json.dumps(targets))
        self.assertTrue(any("control-plane" in p for p in problems), problems)
        targets = json.loads(example_parameters()["Targets"])
        targets[1]["argv"][-1] = "{secret} {instance_id}"
        problems, _ = check(Targets=json.dumps(targets))
        self.assertTrue(any("placeholder" in p for p in problems), problems)
        targets = json.loads(example_parameters()["Targets"])
        targets[0]["detached"] = "true"
        problems, _ = check(Targets=json.dumps(targets))
        self.assertTrue(any("detached must be true or false" in p for p in problems), problems)
        problems, _ = check(Targets="not json")
        self.assertTrue(any("refuse" in p for p in problems), problems)

    def test_environment_quota_is_checked(self):
        targets = json.loads(example_parameters()["Targets"])
        targets[0]["argv"].append("x" * 2000)
        targets[1]["argv"].append("y" * 1500)
        problems, _ = check(Targets=json.dumps(targets))
        self.assertTrue(any("4096" in p for p in problems), problems)

    def test_missing_required_and_unknown_parameters(self):
        params = example_parameters()
        del params["LambdaCodeS3Bucket"]
        problems, ev = cfn_check.run_checks(template(), params, region=REGION, account=ACCOUNT)
        self.assertTrue(any("LambdaCodeS3Bucket is required" in p for p in problems), problems)
        self.assertIsNone(ev)
        problems, _ = check(Bogus="1", DryRun="yes")
        self.assertTrue(any("Bogus" in p for p in problems), problems)
        self.assertTrue(any("DryRun" in p for p in problems), problems)

    def test_code_object_version_is_optional(self):
        _, ev = check()
        self.assertNotIn("S3ObjectVersion", ev.properties("LambdaFunction")["Code"])
        _, ev = check(LambdaCodeS3ObjectVersion="abc123")
        self.assertEqual(ev.properties("LambdaFunction")["Code"]["S3ObjectVersion"], "abc123")

    def test_structural_parity_with_terraform(self):
        _, ev = check()
        statements = ev.statements()
        self.assertEqual(set(statements), cfn_check.EXPECTED_SIDS)
        self.assertEqual(statements["SendRunShellScriptDocument"]["Resource"], "arn:aws:ssm:us-east-2::document/AWS-RunShellScript")
        self.assertEqual(statements["SendCommandToTaggedInstances"]["Resource"], f"arn:aws:ec2:us-east-2:{ACCOUNT}:instance/*")
        self.assertEqual(statements["WriteOwnLogGroup"]["Resource"], f"arn:aws:logs:us-east-2:{ACCOUNT}:log-group:/aws/lambda/spot-handoff:*")
        self.assertEqual(ev.properties("SpotInterruptionRule")["EventPattern"], cfn_check.TERMINATE_ONLY_PATTERN)
        permission = ev.properties("EventBridgeInvokePermission")
        self.assertEqual(permission["SourceArn"], f"arn:aws:events:us-east-2:{ACCOUNT}:rule/spot-handoff")
        fn = ev.properties("LambdaFunction")
        self.assertEqual(fn["Handler"], "spot_handoff.handler.lambda_handler")
        self.assertEqual(fn["Runtime"], "python3.12")
        self.assertEqual(fn["Architectures"], ["arm64"])
        self.assertEqual(fn["LoggingConfig"], {"LogFormat": "Text", "LogGroup": "/aws/lambda/spot-handoff"})
        # Tampering is caught: drop the tag condition and the check fails.
        bad = template()
        policies = bad["Resources"]["LambdaRole"]["Properties"]["Policies"]
        policies[1]["PolicyDocument"] = {"Version": "2012-10-17", "Statement": [{
            "Sid": "SendCommandToTaggedInstances", "Effect": "Allow", "Action": "ssm:SendCommand",
            "Resource": {"Fn::Sub": "arn:${AWS::Partition}:ec2:${AWS::Region}:${AWS::AccountId}:instance/*"}}]}
        problems, _ = cfn_check.run_checks(bad, example_parameters(), region=REGION, account=ACCOUNT)
        self.assertTrue(any("tag condition" in p or "differ from the Lambda" in p for p in problems), problems)
        bad = template()
        bad["Resources"]["SpotInterruptionRule"]["Properties"]["EventPattern"]["detail"]["instance-action"] = ["terminate", "stop"]
        problems, _ = cfn_check.run_checks(bad, example_parameters(), region=REGION, account=ACCOUNT)
        self.assertTrue(any("terminate-only" in p for p in problems), problems)

    def test_partition_follows_the_region(self):
        # AWS::Partition is a pseudo parameter the stack resolves itself; the
        # checker must resolve it the same way or --show prints ARNs no
        # GovCloud or China stack would ever deploy.
        for region, partition in (("us-east-2", "aws"), ("us-gov-west-1", "aws-us-gov"), ("cn-north-1", "aws-cn")):
            problems, ev = cfn_check.run_checks(template(), example_parameters(), region=region, account=ACCOUNT)
            self.assertEqual(problems, [], region)
            statements = ev.statements()
            self.assertEqual(statements["SendRunShellScriptDocument"]["Resource"], f"arn:{partition}:ssm:{region}::document/AWS-RunShellScript")
            self.assertEqual(statements["SendCommandToTaggedInstances"]["Resource"], f"arn:{partition}:ec2:{region}:{ACCOUNT}:instance/*")

    def test_unknown_intrinsic_is_an_error_not_a_guess(self):
        bad = template()
        bad["Resources"]["LambdaFunction"]["Properties"]["Environment"]["Variables"]["DRY_RUN"] = {"Fn::Select": [0, ["off"]]}
        with self.assertRaises(cfn_check.TemplateError):
            cfn_check.run_checks(bad, example_parameters(), region=REGION, account=ACCOUNT)

    def test_cli_passes_on_example_and_fails_on_bad_parameters(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cfn_check.main(["--show"]), 0)
        self.assertIn("PASS", out.getvalue())
        self.assertIn("ssm:resourceTag/Role", out.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "params.json"
            bad.write_text(json.dumps(example_parameters(PerTargetTimeout="85")))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cfn_check.main(["--parameters", str(bad)]), 1)
            self.assertIn("FAIL", out.getvalue())


class PackagerTest(unittest.TestCase):
    def test_zip_is_reproducible_and_complete(self):
        packager = load_packager()
        first, second = packager.build_zip(), packager.build_zip()
        self.assertEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest())
        with zipfile.ZipFile(io.BytesIO(first)) as zf:
            names = zf.namelist()
            self.assertIn("spot_handoff/handler.py", names)
            self.assertIn("spot_handoff/spot-handoff-notify.sh", names)
            self.assertEqual(names, sorted(names))
            self.assertFalse(any("__pycache__" in n or n.endswith(".pyc") for n in names))
            for info in zf.infolist():
                self.assertEqual(info.date_time, packager.FIXED_DATE_TIME)
                self.assertEqual(info.external_attr >> 16, 0o644)
            # Shipped bytes are the working tree's bytes.
            self.assertEqual(zf.read("spot_handoff/spot-handoff-notify.sh"),
                             (REPO_ROOT / "lambda" / "spot_handoff" / "spot-handoff-notify.sh").read_bytes())
        self.assertLess(len(first), 50 * 1024 * 1024, "direct upload limit")

    def test_cli_writes_zip_and_prints_hashes(self):
        packager = load_packager()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "lambda.zip"
            text = io.StringIO()
            with redirect_stdout(text):
                self.assertEqual(packager.main(["--out", str(out)]), 0)
            self.assertTrue(out.exists())
            digest = hashlib.sha256(out.read_bytes()).hexdigest()
            self.assertIn(digest, text.getvalue())
            self.assertIn("CodeSha256", text.getvalue())

    def test_refuses_a_tree_without_the_handler(self):
        packager = load_packager()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "spot_handoff").mkdir()
            (Path(tmp) / "spot_handoff" / "__init__.py").write_text("")
            with self.assertRaises(FileNotFoundError):
                packager.build_zip(Path(tmp))


if __name__ == "__main__":
    unittest.main()
