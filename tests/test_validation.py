import unittest
from datetime import datetime, timezone

import support  # noqa: F401,I001  sets sys.path for spot_handoff
from spot_handoff.validation import (
    NOTICE_WINDOW,
    ValidationError,
    check_instance_tags,
    validate_event,
    validate_tags,
)
from support import ACCOUNT, INSTANCE, REGION, TAGS, valid_event


def validate(event):
    return validate_event(event, expected_region=REGION, expected_account=ACCOUNT)


class EventValidationTest(unittest.TestCase):
    def test_accepts_documented_event_shape(self):
        parsed = validate(valid_event())
        self.assertEqual(parsed.instance_id, INSTANCE)
        self.assertEqual(parsed.event_id, valid_event()["id"])
        self.assertEqual(parsed.time, datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(parsed.deadline, parsed.time + NOTICE_WINDOW)

    def test_rejects_non_object(self):
        for bad in (None, "x", [], 1):
            with self.assertRaises(ValidationError):
                validate(bad)

    def test_rejects_other_sources_and_types(self):
        with self.assertRaisesRegex(ValidationError, "source"):
            validate(valid_event(source="aws.ec2.evil"))
        with self.assertRaisesRegex(ValidationError, "detail-type"):
            validate(valid_event(**{"detail-type": "EC2 Instance Rebalance Recommendation"}))

    def test_rejects_non_terminate_actions(self):
        for action in ("stop", "hibernate", "TERMINATE", "", None):
            with self.assertRaises(ValidationError):
                validate(valid_event(detail={"instance-id": INSTANCE, "instance-action": action}))

    def test_rejects_malformed_instance_ids(self):
        for bad in ("", "i-", "i-123", "i-0123456789abcdefg", "i-0123456789abcdef0; rm -rf /",
                    "I-0123456789ABCDEF0", "mi-0123456789abcdef0", 42, None, ["i-0123456789abcdef0"]):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                validate(valid_event(detail={"instance-id": bad, "instance-action": "terminate"}))

    def test_accepts_short_legacy_instance_id(self):
        event = valid_event(detail={"instance-id": "i-1234abcd", "instance-action": "terminate"}, resources=[])
        self.assertEqual(validate(event).instance_id, "i-1234abcd")

    def test_rejects_missing_detail(self):
        event = valid_event()
        del event["detail"]
        with self.assertRaises(ValidationError):
            validate(event)
        with self.assertRaises(ValidationError):
            validate(valid_event(detail="terminate"))

    def test_rejects_wrong_region_or_account(self):
        with self.assertRaisesRegex(ValidationError, "region"):
            validate(valid_event(region="us-east-1"))
        with self.assertRaisesRegex(ValidationError, "account"):
            validate(valid_event(account="999999999999"))
        for key in ("region", "account", "id", "time"):
            event = valid_event()
            del event[key]
            with self.assertRaises(ValidationError, msg=key):
                validate(event)

    def test_rejects_bad_event_id(self):
        for bad in ("", "a" * 129, "id with space", "id;rm", "ид"):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                validate(valid_event(id=bad))

    def test_resources_must_name_the_instance(self):
        with self.assertRaisesRegex(ValidationError, "resources"):
            validate(valid_event(resources=[f"arn:aws:ec2:{REGION}b:instance/i-0fffffffffffffff0"]))
        with self.assertRaisesRegex(ValidationError, "resources"):
            validate(valid_event(resources="not-a-list"))
        # The documented ARN carries the AZ, not the region+account; both pass.
        validate(valid_event(resources=[f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"]))
        validate(valid_event(resources=[]))

    def test_rejects_bad_time(self):
        with self.assertRaisesRegex(ValidationError, "time"):
            validate(valid_event(time="yesterday"))


class TagValidationTest(unittest.TestCase):
    def test_expected_tags_must_be_non_empty_object(self):
        for bad in ({}, [], "Role=x", None, {"Role": 1}, {"": "x"}, {"Role": "a\nb"}):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                validate_tags(bad)
        self.assertEqual(validate_tags(TAGS), TAGS)

    def test_all_tags_must_match_exactly(self):
        instance = {"Tags": [{"Key": k, "Value": v} for k, v in TAGS.items()]}
        check_instance_tags(instance, TAGS)
        with self.assertRaisesRegex(ValidationError, "lacks"):
            check_instance_tags({"Tags": [{"Key": "Role", "Value": TAGS["Role"]}]}, TAGS)
        with self.assertRaisesRegex(ValidationError, "differ"):
            wrong = {"Tags": [{"Key": "Role", "Value": "other"}, {"Key": "Environment", "Value": "example"}]}
            check_instance_tags(wrong, TAGS)
        with self.assertRaisesRegex(ValidationError, "differ"):
            # Case matters: tag values are compared exactly.
            check_instance_tags({"Tags": [{"Key": "Role", "Value": TAGS["Role"].upper()},
                                          {"Key": "Environment", "Value": "example"}]}, TAGS)
        with self.assertRaises(ValidationError):
            check_instance_tags({}, TAGS)
        with self.assertRaises(ValidationError):
            check_instance_tags({"Tags": "garbage"}, TAGS)


if __name__ == "__main__":
    unittest.main()
