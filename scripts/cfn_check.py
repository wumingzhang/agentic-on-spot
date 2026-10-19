#!/usr/bin/env python3
"""Offline checks of cloudformation/spot-handoff.json against a parameter set.

    python3 scripts/cfn_check.py [--parameters cloudformation/parameters.example.json]
                                 [--template cloudformation/spot-handoff.json]
                                 [--region us-east-2] [--account 123456789012] [--show]

Terraform validates targets, tags and the timeout budget at plan time and
renders the IAM policy for review (`terraform output lambda_policy_json`).
CloudFormation can do neither (its Rules have no arithmetic and cannot look
inside a JSON string), so this script does it before anything is uploaded:

  1. parameters: every required one given, constraints (AllowedPattern,
     AllowedValues, Min/Max) and the template's Rules satisfied;
  2. the Lambda environment the template would deploy, evaluated with the
     same intrinsics CloudFormation would apply, passes the Lambda's own
     load_config() and renders a SendCommand for a sample event: the exact
     checks the function performs on its first event, including forbidden
     containers, placeholders and the timeout budget;
  3. parity with terraform/: the IAM policies grant only the four known
     statements, the SSM document is the AWS-owned one, the tag-conditioned
     statement carries exactly the tags the Lambda enforces, the EventBridge
     pattern is terminate-only, the invoke permission is scoped to the rule,
     and the three TTL bounds agree.

Only Ref, Fn::Sub, Fn::Join, Fn::If, Fn::Equals, Fn::Not, Fn::And, Fn::Or
and Fn::GetAtt (for the resources in this template) are understood; that is
all the template uses, and an unknown function is an error rather than a
guess. Nothing here talks to AWS; `aws cloudformation validate-template` and
cfn-lint (scripts/check.sh runs it when installed) check the template's
syntax, this checks its meaning.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lambda"))

from spot_handoff.config import load_config  # noqa: E402
from spot_handoff.render import DOCUMENT_NAME, render_send_command  # noqa: E402
from spot_handoff.validation import ValidationError, validate_event  # noqa: E402

TEMPLATE_PATH = REPO_ROOT / "cloudformation" / "spot-handoff.json"
EXAMPLE_PARAMETERS_PATH = REPO_ROOT / "cloudformation" / "parameters.example.json"

LAMBDA_ENV_LIMIT = 4096  # bytes, all variables together (Lambda quota)
EXPECTED_SIDS = {"WriteOwnLogGroup", "DescribeInstancesForTagCheck", "SendRunShellScriptDocument", "SendCommandToTaggedInstances"}
ALLOWED_ACTIONS = {"logs:CreateLogStream", "logs:PutLogEvents", "ec2:DescribeInstances", "ssm:SendCommand"}
TERMINATE_ONLY_PATTERN = {
    "source": ["aws.ec2"],
    "detail-type": ["EC2 Spot Instance Interruption Warning"],
    "detail": {"instance-action": ["terminate"]},
}


class TemplateError(Exception):
    """The template uses something this evaluator does not understand."""


def partition_for(region: str) -> str:
    """AWS::Partition as the stack would see it, so --show prints real ARNs."""
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    return "aws"


class _NoValue:
    """AWS::NoValue: the property disappears."""


NO_VALUE = _NoValue()


def load_parameters(path: Path) -> dict[str, str]:
    """Accept the CLI's [{ParameterKey, ParameterValue}] list or a plain object."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if not isinstance(item, dict) or "ParameterKey" not in item:
                raise ValueError("parameter list entries need ParameterKey and ParameterValue")
            out[item["ParameterKey"]] = str(item.get("ParameterValue", ""))
        return out
    if isinstance(raw, dict):
        return {k: str(v) for k, v in raw.items()}
    raise ValueError("parameters must be a list or an object")


class Evaluator:
    """Resolves the template's intrinsics for one parameter set.

    Resource attributes (GetAtt) are synthesised from the evaluated properties
    the way AWS would name them, so a policy Resource compares against the
    real ARN shape rather than a placeholder.
    """

    def __init__(self, template: dict, parameters: dict[str, str], *, region: str, account: str,
                 partition: str = "aws", stack_name: str = "spot-handoff"):
        self.template = template
        self.pseudo = {
            "AWS::Region": region, "AWS::AccountId": account, "AWS::Partition": partition,
            "AWS::StackName": stack_name, "AWS::NoValue": NO_VALUE,
        }
        self.params = {}
        for name, spec in template.get("Parameters", {}).items():
            if name in parameters:
                self.params[name] = parameters[name]
            elif "Default" in spec:
                self.params[name] = str(spec["Default"])
        self._conditions: dict[str, bool] = {}
        self._attrs: dict[str, str] = {}
        self._refs: dict[str, str] = {}
        self._define_resources(region, account, partition)

    # --- resources -----------------------------------------------------------

    def _define_resources(self, region: str, account: str, partition: str) -> None:
        resources = self.template["Resources"]
        name = self.params.get("Name", "")
        log_group = self.eval(resources["LogGroup"]["Properties"]["LogGroupName"])
        self._refs["LogGroup"] = log_group
        self._attrs["LogGroup.Arn"] = f"arn:{partition}:logs:{region}:{account}:log-group:{log_group}:*"
        self._refs["LambdaRole"] = f"{name}-LambdaRole-GENERATED"
        self._attrs["LambdaRole.Arn"] = f"arn:{partition}:iam::{account}:role/{self._refs['LambdaRole']}"
        fn = self.eval(resources["LambdaFunction"]["Properties"]["FunctionName"])
        self._refs["LambdaFunction"] = fn
        self._attrs["LambdaFunction.Arn"] = f"arn:{partition}:lambda:{region}:{account}:function:{fn}"
        rule = self.eval(resources["SpotInterruptionRule"]["Properties"]["Name"])
        self._refs["SpotInterruptionRule"] = rule
        self._attrs["SpotInterruptionRule.Arn"] = f"arn:{partition}:events:{region}:{account}:rule/{rule}"

    # --- intrinsics ----------------------------------------------------------

    def condition(self, name: str) -> bool:
        if name not in self._conditions:
            expr = self.template.get("Conditions", {})[name]
            self._conditions[name] = bool(self.eval(expr))
        return self._conditions[name]

    def ref(self, name: str):
        if name in self.pseudo:
            return self.pseudo[name]
        if name in self.params:
            return self.params[name]
        if name in self._refs:
            return self._refs[name]
        if name in self.template.get("Parameters", {}):
            raise TemplateError(f"parameter {name} has no value and no default")
        raise TemplateError(f"Ref to unknown {name}")

    def get_att(self, resource: str, attr: str) -> str:
        key = f"{resource}.{attr}"
        if key not in self._attrs:
            raise TemplateError(f"GetAtt {key} not modelled")
        return self._attrs[key]

    def sub(self, spec) -> str:
        if isinstance(spec, list):
            text, local = spec[0], {k: self.eval(v) for k, v in spec[1].items()}
        else:
            text, local = spec, {}

        def replace(match: re.Match) -> str:
            var = match.group(1)
            if var.startswith("!"):
                return "${" + var[1:] + "}"
            if var in local:
                return str(local[var])
            if "." in var:
                return self.get_att(*var.split(".", 1))
            value = self.ref(var)
            if value is NO_VALUE:
                raise TemplateError("AWS::NoValue inside Fn::Sub")
            return str(value)

        return re.sub(r"\$\{([^}]+)\}", replace, text)

    def eval(self, node):
        if isinstance(node, list):
            return [v for v in (self.eval(x) for x in node) if v is not NO_VALUE]
        if not isinstance(node, dict):
            return node
        if len(node) == 1:
            (key, arg), = node.items()
            if key == "Ref":
                return self.ref(arg)
            if key == "Condition":
                return self.condition(arg)
            if key == "Fn::Sub":
                return self.sub(arg)
            if key == "Fn::Join":
                delimiter, parts = arg
                return delimiter.join(str(p) for p in self.eval(parts))
            if key == "Fn::If":
                cond, yes, no = arg
                return self.eval(yes if self.condition(cond) else no)
            if key == "Fn::Equals":
                a, b = (self.eval(x) for x in arg)
                return str(a) == str(b)
            if key == "Fn::Not":
                return not self.eval(arg[0])
            if key == "Fn::And":
                return all(self.eval(x) for x in arg)
            if key == "Fn::Or":
                return any(self.eval(x) for x in arg)
            if key == "Fn::GetAtt":
                resource, attr = arg if isinstance(arg, list) else arg.split(".", 1)
                return self.get_att(resource, attr)
            if key.startswith("Fn::"):
                raise TemplateError(f"{key} is not supported by this checker")
        return {k: v for k, v in ((k, self.eval(v)) for k, v in node.items()) if v is not NO_VALUE}

    # --- views of the evaluated template -------------------------------------

    def properties(self, resource: str) -> dict:
        return self.eval(self.template["Resources"][resource]["Properties"])

    def lambda_env(self) -> dict[str, str]:
        variables = self.properties("LambdaFunction")["Environment"]["Variables"]
        return {k: str(v) for k, v in variables.items()}

    def policy_documents(self) -> dict[str, dict]:
        """Inline role policies by name; a string-form document is parsed."""
        out = {}
        for policy in self.properties("LambdaRole")["Policies"]:
            doc = policy["PolicyDocument"]
            if isinstance(doc, str):
                try:
                    doc = json.loads(doc)
                except json.JSONDecodeError as exc:
                    raise TemplateError(f"policy {policy['PolicyName']} is not valid JSON once rendered: {exc.msg}") from exc
            out[policy["PolicyName"]] = doc
        return out

    def statements(self) -> dict[str, dict]:
        return {s["Sid"]: s for doc in self.policy_documents().values() for s in doc["Statement"]}


# --- checks ------------------------------------------------------------------

def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def check_parameters(template: dict, parameters: dict[str, str]) -> list[str]:
    problems = []
    specs = template.get("Parameters", {})
    for name in parameters:
        if name not in specs:
            problems.append(f"parameter {name} is not in the template")
    for name, spec in specs.items():
        if name not in parameters:
            if "Default" not in spec:
                problems.append(f"parameter {name} is required (no default) and not given")
            continue
        value = parameters[name]
        if spec["Type"] == "Number":
            try:
                number = float(value)
            except ValueError:
                problems.append(f"parameter {name}={value!r} is not a number")
                continue
            if "MinValue" in spec and number < spec["MinValue"]:
                problems.append(f"parameter {name}={value} is below MinValue {spec['MinValue']}")
            if "MaxValue" in spec and number > spec["MaxValue"]:
                problems.append(f"parameter {name}={value} is above MaxValue {spec['MaxValue']}")
            if "AllowedValues" in spec and number not in [float(v) for v in spec["AllowedValues"]]:
                problems.append(f"parameter {name}={value} is not one of its AllowedValues")
        else:
            if "MinLength" in spec and len(value) < spec["MinLength"]:
                problems.append(f"parameter {name} is shorter than MinLength {spec['MinLength']}")
            if "MaxLength" in spec and len(value) > spec["MaxLength"]:
                problems.append(f"parameter {name} is longer than MaxLength {spec['MaxLength']}")
            if "AllowedPattern" in spec and not re.fullmatch(spec["AllowedPattern"], value):
                problems.append(f"parameter {name}={value!r} does not match its AllowedPattern ({spec.get('ConstraintDescription', spec['AllowedPattern'])})")
            if "AllowedValues" in spec and value not in spec["AllowedValues"]:
                problems.append(f"parameter {name}={value!r} is not one of {spec['AllowedValues']}")
    return problems


def check_rules(ev: Evaluator) -> list[str]:
    problems = []
    for name, rule in ev.template.get("Rules", {}).items():
        if "RuleCondition" in rule and not ev.eval(rule["RuleCondition"]):
            continue
        for assertion in rule["Assertions"]:
            if not ev.eval(assertion["Assert"]):
                problems.append(f"rule {name}: {assertion['AssertDescription']}")
    return problems


def sample_event(ev: Evaluator) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "version": "0", "id": "00000000-0000-4000-8000-000000000000",
        "detail-type": "EC2 Spot Instance Interruption Warning", "source": "aws.ec2",
        "account": ev.pseudo["AWS::AccountId"], "time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "region": ev.pseudo["AWS::Region"],
        "resources": [f"arn:aws:ec2:{ev.pseudo['AWS::Region']}a:instance/i-0123456789abcdef0"],
        "detail": {"instance-id": "i-0123456789abcdef0", "instance-action": "terminate"},
    }


def check_lambda(ev: Evaluator) -> tuple[list[str], dict]:
    """Run the Lambda's own configuration and rendering code on the deployed environment."""
    problems = []
    props = ev.properties("LambdaFunction")
    env = ev.lambda_env()
    if props.get("Handler") != "spot_handoff.handler.lambda_handler":
        problems.append(f"Lambda handler is {props.get('Handler')!r}")
    if props.get("LoggingConfig", {}).get("LogGroup") != ev.ref("LogGroup"):
        problems.append("Lambda does not log to the template's log group")
    if props.get("Code", {}).get("S3Bucket") != ev.params.get("LambdaCodeS3Bucket"):
        problems.append("Lambda code bucket is not the LambdaCodeS3Bucket parameter")
    env_bytes = sum(len(k.encode()) + len(v.encode()) + 2 for k, v in env.items())
    if env_bytes > LAMBDA_ENV_LIMIT:
        problems.append(f"Lambda environment is {env_bytes} bytes; the quota for all variables is {LAMBDA_ENV_LIMIT}")
    if env.get("EXPECTED_ACCOUNT_ID") != ev.pseudo["AWS::AccountId"]:
        problems.append("EXPECTED_ACCOUNT_ID is not the stack's account")
    for key, param in (("MAX_EVENT_AGE", "MaxEventAgeSeconds"), ("DRY_RUN", "DryRun"), ("TARGETS", "Targets")):
        if env.get(key) != ev.params.get(param):
            problems.append(f"environment {key} is not the {param} parameter")
    try:
        config = load_config({**env, "AWS_REGION": ev.pseudo["AWS::Region"]})
        parsed = validate_event(sample_event(ev), expected_region=ev.pseudo["AWS::Region"], expected_account=ev.pseudo["AWS::AccountId"])
        request = render_send_command(config, parsed, comment="cfn-check")
    except ValidationError as exc:
        problems.append(f"the Lambda would refuse this configuration: {exc}")
        return problems, env
    size = sum(len(c) + 1 for c in request["Parameters"]["commands"])
    if size > 60 * 1024:
        problems.append(f"rendered SSM command is {size} bytes, over the 64 KB document limit with margin")
    if request["DocumentName"] != DOCUMENT_NAME:
        problems.append(f"rendered document is {request['DocumentName']}")
    return problems, env


def check_iam(ev: Evaluator, env: dict[str, str]) -> list[str]:
    problems = []
    region, account, partition = (ev.pseudo[k] for k in ("AWS::Region", "AWS::AccountId", "AWS::Partition"))
    assume = ev.properties("LambdaRole")["AssumeRolePolicyDocument"]
    for statement in assume["Statement"]:
        if statement.get("Principal") != {"Service": "lambda.amazonaws.com"}:
            problems.append("role trust policy names a principal other than lambda.amazonaws.com")
        if statement.get("Condition", {}).get("StringEquals", {}).get("aws:SourceAccount") != account:
            problems.append("role trust policy lacks the aws:SourceAccount confused-deputy guard")
    try:
        statements = ev.statements()
    except TemplateError as exc:
        return problems + [str(exc)]
    if set(statements) != EXPECTED_SIDS:
        problems.append(f"policy statements are {sorted(statements)}, expected {sorted(EXPECTED_SIDS)}")
    actions = {a for s in statements.values() for a in _as_list(s.get("Action", []))}
    if not actions <= ALLOWED_ACTIONS:
        problems.append(f"policy grants actions outside the known set: {sorted(actions - ALLOWED_ACTIONS)}")
    for sid, statement in statements.items():
        if statement.get("Effect") != "Allow" or "NotAction" in statement or "NotResource" in statement:
            problems.append(f"statement {sid} is not a plain Allow")
    for sid, statement in statements.items():
        resources = _as_list(statement.get("Resource", []))
        actions = _as_list(statement.get("Action", []))
        if "*" in resources and actions != ["ec2:DescribeInstances"]:
            problems.append(f"statement {sid} grants {actions} on *; only ec2:DescribeInstances may")
    logs = statements.get("WriteOwnLogGroup", {})
    if _as_list(logs.get("Resource")) != [ev.get_att("LogGroup", "Arn")]:
        problems.append(f"log statement resource is {logs.get('Resource')!r}, not the template's own log group")
    doc = statements.get("SendRunShellScriptDocument", {})
    if _as_list(doc.get("Resource")) != [f"arn:{partition}:ssm:{region}::document/{DOCUMENT_NAME}"]:
        problems.append(f"document statement resource is {doc.get('Resource')!r}, not the AWS-owned {DOCUMENT_NAME}")
    tagged = statements.get("SendCommandToTaggedInstances", {})
    if _as_list(tagged.get("Resource")) != [f"arn:{partition}:ec2:{region}:{account}:instance/*"]:
        problems.append(f"tagged-instance statement resource is {tagged.get('Resource')!r}")
    condition = tagged.get("Condition", {})
    if set(condition) != {"StringEquals"}:
        problems.append(f"tagged-instance statement condition operators are {sorted(condition)}, expected StringEquals only")
    expected_tags = json.loads(env.get("EXPECTED_TAGS", "{}"))
    wanted = {f"ssm:resourceTag/{k}": v for k, v in expected_tags.items()}
    if condition.get("StringEquals") != wanted:
        problems.append(f"IAM tag conditions {condition.get('StringEquals')} differ from the Lambda's EXPECTED_TAGS {expected_tags}")
    if not wanted:
        problems.append("no tag condition at all: ssm:SendCommand would be allowed on every instance")
    return problems


def check_events(ev: Evaluator) -> list[str]:
    problems = []
    rule = ev.properties("SpotInterruptionRule")
    if rule.get("EventPattern") != TERMINATE_ONLY_PATTERN:
        problems.append(f"event pattern is {rule.get('EventPattern')}, not terminate-only")
    if rule.get("State") != "ENABLED":
        problems.append("rule is not ENABLED")
    targets = rule.get("Targets", [])
    if len(targets) != 1 or targets[0].get("Arn") != ev.get_att("LambdaFunction", "Arn"):
        problems.append("rule does not target exactly the template's Lambda")
    age = ev.params["MaxEventAgeSeconds"]
    if str(targets[0].get("RetryPolicy", {}).get("MaximumEventAgeInSeconds")) != age:
        problems.append("EventBridge retry age differs from MaxEventAgeSeconds")
    invoke = ev.properties("LambdaEventInvokeConfig")
    if str(invoke.get("MaximumEventAgeInSeconds")) != age or str(invoke.get("MaximumRetryAttempts")) != ev.params["MaxRetryAttempts"]:
        problems.append("Lambda async retry policy differs from MaxEventAgeSeconds/MaxRetryAttempts")
    if ev.lambda_env().get("MAX_EVENT_AGE") != age:
        problems.append("Lambda-side TTL differs from MaxEventAgeSeconds")
    permission = ev.properties("EventBridgeInvokePermission")
    if (permission.get("Principal"), permission.get("Action")) != ("events.amazonaws.com", "lambda:InvokeFunction"):
        problems.append("invoke permission is not events.amazonaws.com / lambda:InvokeFunction")
    if permission.get("SourceArn") != ev.get_att("SpotInterruptionRule", "Arn"):
        problems.append("invoke permission is not scoped to the rule's ARN")
    if permission.get("FunctionName") != ev.ref("LambdaFunction"):
        problems.append("invoke permission names another function")
    log_group = ev.properties("LogGroup")
    if str(log_group.get("RetentionInDays")) != ev.params["LogRetentionDays"]:
        problems.append("log group retention differs from LogRetentionDays")
    return problems


def run_checks(template: dict, parameters: dict[str, str], *, region: str, account: str) -> tuple[list[str], Evaluator | None]:
    """All problems found, and the evaluator when the parameters were usable."""
    problems = check_parameters(template, parameters)
    if any("required" in p or "not a number" in p for p in problems):
        return problems, None
    ev = Evaluator(template, parameters, region=region, account=account, partition=partition_for(region))
    problems += check_rules(ev)
    lambda_problems, env = check_lambda(ev)
    problems += lambda_problems
    problems += check_iam(ev, env)
    problems += check_events(ev)
    return problems, ev


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", type=Path, default=TEMPLATE_PATH)
    parser.add_argument("--parameters", type=Path, default=EXAMPLE_PARAMETERS_PATH,
                        help="JSON: the CLI's [{ParameterKey, ParameterValue}] list or a {key: value} object")
    parser.add_argument("--region", default="us-east-2", help="region the stack will be created in (for ARNs; default us-east-2)")
    parser.add_argument("--account", default="123456789012", help="account id the stack will be created in (for ARNs)")
    parser.add_argument("--show", action="store_true", help="print the rendered IAM policies and Lambda environment")
    args = parser.parse_args(argv)

    template = json.loads(args.template.read_text(encoding="utf-8"))
    parameters = load_parameters(args.parameters)
    problems, ev = run_checks(template, parameters, region=args.region, account=args.account)
    if args.show and ev is not None:
        print("# IAM policies as deployed")
        print(json.dumps(ev.policy_documents(), indent=2))
        print("# Lambda environment as deployed")
        print(json.dumps(ev.lambda_env(), indent=2))
    for problem in problems:
        print(f"FAIL  {problem}")
    if problems:
        return 1
    tags = json.loads(ev.lambda_env()["EXPECTED_TAGS"])
    print(f"PASS  {args.template.name} with {args.parameters.name}: {len(load_config({**ev.lambda_env(), 'AWS_REGION': args.region}).targets)} targets, "
          f"{len(tags)} expected tags, dry_run={ev.lambda_env()['DRY_RUN']}, {len(ev.statements())} IAM statements")
    return 0


if __name__ == "__main__":
    sys.exit(main())
