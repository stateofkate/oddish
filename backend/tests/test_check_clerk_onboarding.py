"""The operator check must report live policy drift without changing Clerk."""

import io
import json
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch

from check_clerk_onboarding import main


def environment(*, user_creation=False, automatic_creation=False):
    return {
        "display_config": {
            "application_name": "Oddish test",
            "instance_environment_type": "development",
        },
        "user_settings": {"actions": {"create_organization": user_creation}},
        "organization_settings": {
            "enabled": True,
            "force_organization_selection": True,
            "organization_creation_defaults": {
                "automatic_organization_creation": {"enabled": automatic_creation}
            },
        },
    }


class ClerkOnboardingCheckTest(unittest.TestCase):
    def run_check(self, data):
        output = io.StringIO()
        with (
            patch(
                "urllib.request.urlopen",
                return_value=io.BytesIO(json.dumps(data).encode()),
            ) as request,
            redirect_stdout(output),
        ):
            result = main(["clerk.example.com"])
        # One public GET, with no credentials or write request.
        request.assert_called_once_with(
            "https://clerk.example.com/v1/environment", timeout=15
        )
        return result, output.getvalue()

    def test_accepts_required_membership_without_organization_creation(self):
        result, output = self.run_check(environment())
        self.assertEqual(result, 0)
        self.assertIn("Oddish test (development) at clerk.example.com", output)
        self.assertNotIn("FAIL", output)

    def test_reports_the_observed_demo_and_production_creation_settings(self):
        for automatic in (False, True):
            with self.subTest(automatic_creation=automatic):
                result, output = self.run_check(
                    environment(user_creation=True, automatic_creation=automatic)
                )
                self.assertEqual(result, 1)
                self.assertIn("FAIL: Allow user-created organizations = True", output)
                self.assertEqual(
                    "FAIL: Create first organization automatically" in output, automatic
                )

    def test_membership_must_still_be_required(self):
        data = environment()
        data["organization_settings"]["force_organization_selection"] = False
        result, output = self.run_check(data)
        self.assertEqual(result, 1)
        self.assertIn("FAIL: Membership required = False", output)

    def test_missing_or_non_boolean_settings_never_pass(self):
        result, output = self.run_check({})
        self.assertEqual(result, 2)
        self.assertIn("Could not verify", output)
        for value in (None, 0, "false"):
            with self.subTest(value=value):
                result, _ = self.run_check(environment(user_creation=value))
                self.assertEqual(result, 1)

    def test_network_failure_is_not_reported_as_a_successful_check(self):
        with (
            patch(
                "urllib.request.urlopen", side_effect=urllib.error.URLError("offline")
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(main(["clerk.example.com"]), 2)
        self.assertIn("Could not verify", output.getvalue())


if __name__ == "__main__":
    unittest.main()
