import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

import support  # noqa: F401,I001  sets sys.path for spot_handoff
from spot_handoff import handler
from spot_handoff.validation import ValidationError
from support import (
    ACCOUNT,
    EVENT_ID,
    INSTANCE,
    TAGS,
    FakeClientError,
    FakeContext,
    FakeEC2,
    FakeSSM,
    instance_description,
    valid_env,
    valid_event,
)


EVENT_TIME = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)  # the fixture event's `time`


def run(event=None, env=None, ec2=None, ssm=None, context=None, now=None):
    ec2 = FakeEC2() if ec2 is None else ec2
    ssm = FakeSSM() if ssm is None else ssm
    out = io.StringIO()
    with redirect_stdout(out):
        result = handler.handle(valid_event() if event is None else event, context or FakeContext(),
                                env=valid_env() if env is None else env, clients=(ec2, ssm),
                                now=EVENT_TIME + timedelta(seconds=5) if now is None else now)
    logs = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    return result, logs, ec2, ssm


class HandlerTest(unittest.TestCase):
    def test_submits_for_valid_event(self):
        result, logs, ec2, ssm = run()
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["command_id"], "cmd-0001")
        self.assertEqual(ec2.calls, [{"InstanceIds": [INSTANCE]}])
        self.assertEqual(len(ssm.calls), 1)
        request = ssm.calls[0]
        self.assertEqual(request["InstanceIds"], [INSTANCE])
        self.assertEqual(request["DocumentName"], "AWS-RunShellScript")
        self.assertEqual(request["TimeoutSeconds"], 30)
        self.assertEqual(request["Parameters"]["executionTimeout"], ["90"])
        accepted = [l for l in logs if l["msg"].startswith("SendCommand accepted")]
        self.assertEqual(len(accepted), 1)
        self.assertEqual((accepted[0]["event_id"], accepted[0]["instance_id"], accepted[0]["command_id"]),
                         (EVENT_ID, INSTANCE, "cmd-0001"))
        self.assertIn("not confirmed", accepted[0]["msg"])

    def test_ignores_invalid_events_without_calling_aws(self):
        cases = [
            valid_event(detail={"instance-id": INSTANCE, "instance-action": "stop"}),
            valid_event(source="custom.app"),
            valid_event(detail={"instance-id": "i-nope", "instance-action": "terminate"}),
            valid_event(region="us-west-2"),
            valid_event(account="000000000000"),
            {"Records": []},
            "garbage",
        ]
        for event in cases:
            result, logs, ec2, ssm = run(event=event)
            self.assertEqual(result["status"], "ignored", event)
            self.assertEqual(ec2.calls, [], event)
            self.assertEqual(ssm.calls, [], event)
            self.assertTrue(any(l["msg"] == "event ignored" for l in logs))

    def test_event_ttl_missing_stale_future_and_fresh(self):
        # missing time: rejected at shape validation, nothing called
        event = valid_event()
        del event["time"]
        result, _, ec2, ssm = run(event=event)
        self.assertEqual(result["status"], "ignored")
        self.assertEqual((ec2.calls, ssm.calls), ([], []))
        # stale: older than the 180s TTL (a replayed or long-delayed notice)
        result, logs, ec2, ssm = run(now=EVENT_TIME + timedelta(seconds=181))
        self.assertEqual(result["status"], "ignored")
        self.assertIn("stale", result["reason"])
        self.assertEqual((ec2.calls, ssm.calls), ([], []))
        self.assertTrue(any(l["msg"] == "event ignored" and "stale" in l["reason"] for l in logs))
        # exactly at the TTL still counts
        self.assertEqual(run(now=EVENT_TIME + timedelta(seconds=180))[0]["status"], "submitted")
        # slightly in the future (clock skew): accepted
        self.assertEqual(run(now=EVENT_TIME - timedelta(seconds=120))[0]["status"], "submitted")
        # far in the future: ignored
        result, _, ec2, ssm = run(now=EVENT_TIME - timedelta(seconds=301))
        self.assertEqual(result["status"], "ignored")
        self.assertIn("future", result["reason"])
        self.assertEqual(ssm.calls, [])
        # TTL configurable, 0 disables
        self.assertEqual(run(env=valid_env(MAX_EVENT_AGE="60"), now=EVENT_TIME + timedelta(seconds=61))[0]["status"], "ignored")
        self.assertEqual(run(env=valid_env(MAX_EVENT_AGE="0"), now=EVENT_TIME + timedelta(days=2))[0]["status"], "submitted")
        with self.assertRaises(ValidationError):
            run(env=valid_env(MAX_EVENT_AGE="-1"))

    def test_rejects_instance_with_missing_or_wrong_tags(self):
        for tags in ({}, {"Role": TAGS["Role"]}, {**TAGS, "Role": "other"}):
            result, logs, _, ssm = run(ec2=FakeEC2(instance_description(tags=tags)))
            self.assertEqual(result["status"], "rejected", tags)
            self.assertEqual(ssm.calls, [], tags)
            self.assertTrue(any(l["msg"] == "instance rejected" for l in logs))

    def test_rejects_when_instance_not_found_or_wrong_instance_returned(self):
        result, _, _, ssm = run(ec2=FakeEC2({"Reservations": []}))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(ssm.calls, [])
        result, _, _, ssm = run(ec2=FakeEC2(instance_description(instance_id="i-0fffffffffffffff0")))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(ssm.calls, [])

    def test_describe_failure_raises_and_sends_nothing(self):
        for exc in (FakeClientError("RequestLimitExceeded"), TimeoutError("read timed out"),
                    FakeClientError("InvalidInstanceID.NotFound")):
            with self.assertRaises(type(exc)):
                run(ec2=FakeEC2(exc))

    def test_send_command_failure_raises_after_logging(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(FakeClientError):
            handler.handle(valid_event(), FakeContext(), env=valid_env(), now=EVENT_TIME,
                           clients=(FakeEC2(), FakeSSM(error=FakeClientError("InvalidInstanceId", "not managed"))))
        logs = [json.loads(l) for l in out.getvalue().splitlines()]
        failed = [l for l in logs if l["msg"] == "SendCommand failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error"], "FakeClientError")
        self.assertEqual(failed[0]["instance_id"], INSTANCE)

    def test_lambda_dry_run_never_sends(self):
        result, logs, ec2, ssm = run(env=valid_env(DRY_RUN="lambda"))
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(ec2.calls, [{"InstanceIds": [INSTANCE]}])
        self.assertEqual(ssm.calls, [])
        self.assertEqual(result["request"]["InstanceIds"], [INSTANCE])

    def test_host_dry_run_sends_with_flag(self):
        _, _, _, ssm = run(env=valid_env(DRY_RUN="host"))
        self.assertIn(" --dry-run", ssm.calls[0]["Parameters"]["commands"][-2])

    def test_bad_configuration_raises_before_any_call(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(ValidationError):
            handler.handle(valid_event(), FakeContext(), env=valid_env(TARGETS="[]"), now=EVENT_TIME,
                           clients=(FakeEC2(), FakeSSM()))

    def test_account_from_context_or_env(self):
        result, *_ = run(context=FakeContext(account="999999999999"))
        self.assertEqual(result["status"], "ignored")
        result, *_ = run(context=FakeContext(account="999999999999"), env=valid_env(EXPECTED_ACCOUNT_ID=ACCOUNT))
        self.assertEqual(result["status"], "submitted")

        class NoArn:
            aws_request_id = "r"

        with self.assertRaises(RuntimeError):
            run(context=NoArn())

    def test_logs_are_structured_and_bounded_and_secret_free(self):
        env = valid_env(POSTGRES_PASSWORD="hunter2-not-for-logs", AWS_SECRET_ACCESS_KEY="AKIA-SECRET")
        _, logs, _, _ = run(env=env)
        text = json.dumps(logs)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("AKIA-SECRET", text)
        self.assertNotIn("Put all tasks into handoff mode", text, "command text is not logged")
        for record in logs:
            self.assertIn("level", record)
            self.assertIn("msg", record)
            for value in record.values():
                self.assertLessEqual(len(str(value)), 600)

    def test_long_error_detail_is_truncated(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(FakeClientError):
            handler.handle(valid_event(), FakeContext(), env=valid_env(), now=EVENT_TIME,
                           clients=(FakeEC2(FakeClientError("Throttling", "x" * 5000)), FakeSSM()))
        record = [json.loads(l) for l in out.getvalue().splitlines() if "DescribeInstances failed" in l][0]
        self.assertLess(len(record["detail"]), 600)
        self.assertTrue(record["detail"].endswith("(truncated)"))


if __name__ == "__main__":
    unittest.main()
