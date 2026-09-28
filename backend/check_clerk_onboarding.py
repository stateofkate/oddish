"""Read-only check of Clerk's organization onboarding settings; no credentials needed."""

import argparse
import json
import urllib.request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain", help="Clerk API hostname, e.g. clerk.oddish.app")
    args = parser.parse_args(argv)

    try:
        with urllib.request.urlopen(
            f"https://{args.domain}/v1/environment", timeout=15
        ) as response:
            environment = json.load(response)
        display = environment["display_config"]
        organizations = environment["organization_settings"]
        checks = [
            ("Organizations enabled", organizations["enabled"], True),
            (
                "Membership required",
                organizations["force_organization_selection"],
                True,
            ),
            (
                "Allow user-created organizations",
                environment["user_settings"]["actions"]["create_organization"],
                False,
            ),
            (
                "Create first organization automatically",
                organizations["organization_creation_defaults"][
                    "automatic_organization_creation"
                ]["enabled"],
                False,
            ),
        ]
        print(
            f"{display['application_name']} "
            f"({display['instance_environment_type']}) at {args.domain}"
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Could not verify Clerk onboarding at {args.domain}: {error}")
        return 2

    for label, actual, expected in checks:
        status = "PASS" if actual is expected else "FAIL"
        print(f"{status}: {label} = {actual!r}; expected {expected!r}")
    return 0 if all(actual is expected for _, actual, expected in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
