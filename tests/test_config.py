import json
import unittest

import support  # noqa: F401,I001  sets sys.path for spot_handoff
from spot_handoff.config import load_config
from spot_handoff.validation import ValidationError
from support import HERMES_ARGV, TARGETS, valid_env


class ConfigTest(unittest.TestCase):
    def test_loads_targets_and_defaults(self):
        config = load_config(valid_env())
        self.assertEqual([t.name for t in config.targets], ["hermes", "yardr"])
        self.assertEqual(config.targets[0].argv, tuple(HERMES_ARGV))
        self.assertEqual(config.targets[0].user, "coder")
        self.assertEqual(config.targets[1].user, "coder")
        self.assertTrue(config.targets[0].detached)
        self.assertFalse(config.targets[1].detached)
        no_user = {k: v for k, v in TARGETS[0].items() if k != "user"}
        self.assertIsNone(load_config(valid_env(TARGETS=json.dumps([no_user]))).targets[0].user)
        self.assertEqual(config.state_dir, "/data/spot-handoff")
        self.assertEqual((config.execution_timeout, config.submission_timeout), (90, 30))
        self.assertEqual(config.dry_run, "off")

    def test_requires_targets_and_tags(self):
        with self.assertRaises(ValidationError):
            load_config(valid_env(TARGETS="[]"))
        with self.assertRaises(ValidationError):
            load_config(valid_env(TARGETS="not json"))
        with self.assertRaises(ValidationError):
            load_config(valid_env(TARGETS='{"name":"x"}'))
        with self.assertRaises(ValidationError):
            load_config(valid_env(EXPECTED_TAGS="{}"))
        env = valid_env()
        del env["EXPECTED_TAGS"]
        with self.assertRaises(ValidationError):
            load_config(env)

    def test_target_fields_are_validated(self):
        base = {"name": "t", "container": "coder-x", "argv": ["true"]}
        bad_variants = [
            {**base, "name": "bad name"},
            {**base, "name": ""},
            {**base, "container": "-leading-dash"},
            {**base, "container": "name with space"},
            {**base, "container": "x;rm -rf /"},
            {**base, "container": ""},
            {**base, "user": "root; id"},
            {**base, "user": ""},
            {**base, "argv": []},
            {**base, "argv": "hermes -c hi"},
            {**base, "argv": ["a\nb"]},
            {**base, "argv": ["a\x1fb"]},
            {**base, "argv": [""]},
            {**base, "argv": ["x"] * 65},
            {**base, "argv": ["x" * 2049]},
            {**base, "command": "docker exec ..."},
            "just a string",
        ]
        for variant in bad_variants:
            with self.assertRaises(ValidationError, msg=repr(variant)[:80]):
                load_config(valid_env(TARGETS=json.dumps([variant])))

    def test_detached_is_optional_boolean_default_false(self):
        base = {"name": "t", "container": "coder-x", "argv": ["true"]}
        # Absent (every config written before the key existed) and false both
        # mean the synchronous exec; only a JSON true opts a target in.
        self.assertFalse(load_config(valid_env(TARGETS=json.dumps([base]))).targets[0].detached)
        self.assertFalse(load_config(valid_env(TARGETS=json.dumps([{**base, "detached": False}]))).targets[0].detached)
        self.assertTrue(load_config(valid_env(TARGETS=json.dumps([{**base, "detached": True}]))).targets[0].detached)
        for bad in ("true", "yes", 1, 0, None, [True], {"on": True}):
            with self.assertRaisesRegex(ValidationError, "detached must be true or false", msg=repr(bad)):
                load_config(valid_env(TARGETS=json.dumps([{**base, "detached": bad}])))

    def test_duplicate_target_names_rejected(self):
        two = [dict(TARGETS[0]), dict(TARGETS[0])]
        with self.assertRaisesRegex(ValidationError, "unique"):
            load_config(valid_env(TARGETS=json.dumps(two)))

    def test_control_plane_container_rejected(self):
        bad = [{"name": "ctl", "container": "coder", "argv": ["true"]}]
        with self.assertRaisesRegex(ValidationError, "control-plane"):
            load_config(valid_env(TARGETS=json.dumps(bad)))
        # The list is configurable; an operator may extend it, or empty it.
        load_config(valid_env(TARGETS=json.dumps(bad), FORBIDDEN_CONTAINERS=""))
        with self.assertRaises(ValidationError):
            load_config(valid_env(FORBIDDEN_CONTAINERS="coder,coder-example-yardr"))

    def test_timeouts_must_fit_the_window(self):
        with self.assertRaisesRegex(ValidationError, "EXECUTION_TIMEOUT"):
            load_config(valid_env(PER_TARGET_TIMEOUT="80"))
        with self.assertRaisesRegex(ValidationError, "TimeoutSeconds"):
            load_config(valid_env(SUBMISSION_TIMEOUT="10"))
        with self.assertRaises(ValidationError):
            load_config(valid_env(PER_TARGET_TIMEOUT="soon"))
        with self.assertRaises(ValidationError):
            load_config(valid_env(PER_TARGET_TIMEOUT="0"))
        config = load_config(valid_env(EXECUTION_TIMEOUT="120", PER_TARGET_TIMEOUT="80"))
        self.assertEqual(config.per_target_timeout, 80)

    def test_dry_run_modes(self):
        for mode in ("off", "lambda", "host", "HOST"):
            self.assertEqual(load_config(valid_env(DRY_RUN=mode)).dry_run, mode.lower())
        with self.assertRaises(ValidationError):
            load_config(valid_env(DRY_RUN="yes"))

    def test_expected_account_id_must_be_an_account_id(self):
        self.assertEqual(load_config(valid_env(EXPECTED_ACCOUNT_ID="123456789012")).expected_account, "123456789012")
        self.assertIsNone(load_config(valid_env(EXPECTED_ACCOUNT_ID="")).expected_account)
        for bad in ("12345678901", "1234567890123", "123456789012 ", "arn:aws:iam::123456789012:root"):
            with self.assertRaisesRegex(ValidationError, "EXPECTED_ACCOUNT_ID", msg=bad):
                load_config(valid_env(EXPECTED_ACCOUNT_ID=bad))

    def test_state_dir_must_be_plain_absolute_path(self):
        for bad in ("relative/dir", "/data/with space", "/data/it's", "/data/$(x)"):
            with self.assertRaises(ValidationError, msg=bad):
                load_config(valid_env(STATE_DIR=bad))


if __name__ == "__main__":
    unittest.main()
