import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github/scripts/staging/publish_runtime_db.py"
spec = importlib.util.spec_from_file_location("publish_runtime_db", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
DIRECT = "postgresql+asyncpg://neondb_owner:p%40ss%3Aword@ep-staging.us-east-1.aws.neon.tech:5432/neondb"
POOLED = DIRECT.replace("ep-staging.", "ep-staging-pooler.")


class RuntimeDatabaseTests(unittest.TestCase):
    def test_deploy_stops_on_failure_and_publishes_after_both_migrations(self):
        for failure in ("none", "core", "backend", "publish"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                temp = Path(tmp)
                command_log = temp / "commands.log"
                uv = temp / "uv"
                uv.write_text("""#!/usr/bin/env bash
printf '%s|%s\\n' "${PWD##*/}" "$*" >> "$COMMAND_LOG"
if [[ "$*" == 'run alembic upgrade head' ]]; then
  if [[ "$FAILURE" == 'core' && "${PWD##*/}" == 'oddish' ]]; then exit 1; fi
  if [[ "$FAILURE" == 'backend' && "${PWD##*/}" == 'backend' ]]; then exit 1; fi
fi
if [[ "$FAILURE" == 'publish' && "$*" == *publish_runtime_db.py ]]; then exit 1; fi
""")
                uv.chmod(0o755)
                result = subprocess.run(
                    ["bash", str(REPO / ".github/scripts/staging/deploy_staging.sh")],
                    env={
                        "PATH": str(temp) + os.pathsep + os.environ["PATH"],
                        "GITHUB_WORKSPACE": str(REPO),
                        "COMMAND_LOG": str(command_log),
                        "FAILURE": failure,
                        "ODDISH_DATABASE_URL": DIRECT,
                        "ODDISH_RUNTIME_DATABASE_URL": POOLED,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                commands = command_log.read_text().splitlines()
                if failure != "none":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(any("modal deploy" in cmd for cmd in commands))
                    if failure != "publish":
                        self.assertFalse(
                            any("publish_runtime_db.py" in cmd for cmd in commands)
                        )
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        [cmd for cmd in commands if "alembic upgrade" in cmd],
                        [
                            "oddish|run alembic upgrade head",
                            "backend|run alembic upgrade head",
                        ],
                    )
                    self.assertTrue(commands[-2].endswith("publish_runtime_db.py"))
                    self.assertEqual(commands[-1], "backend|run modal deploy deploy.py")

    def test_keeps_the_pooled_api_url(self):
        self.assertEqual(module.runtime_url(DIRECT, POOLED), POOLED)
        self.assertEqual(
            module.runtime_url(
                DIRECT.replace(":5432", ""), POOLED.replace(":5432", "")
            ),
            POOLED.replace(":5432", ""),
        )

    def test_rejects_wrong_provider_branch_database_or_credentials(self):
        for url in (
            "",
            DIRECT,
            POOLED.replace(".neon.tech", ".supabase.co"),
            POOLED.replace(":5432/", ":9999/"),
            POOLED.replace("/neondb", "/other"),
            POOLED.replace("ep-staging", "ep-other"),
            POOLED.replace("postgresql+asyncpg", "https"),
            POOLED.replace(":p%40ss%3Aword", ":different"),
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                module.runtime_url(DIRECT, url)
        with self.assertRaises(ValueError):
            module.runtime_url(POOLED, POOLED)

    def test_publishes_only_staging_with_existing_runtime_settings(self):
        with (
            patch.dict(
                os.environ,
                {"ODDISH_DATABASE_URL": DIRECT, "ODDISH_RUNTIME_DATABASE_URL": POOLED},
            ),
            patch.object(module.subprocess, "run") as run,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            module.main()
        args = run.call_args.args[0]
        self.assertEqual(
            args[:9],
            [
                "uv",
                "run",
                "modal",
                "secret",
                "create",
                "--env",
                "staging",
                "--force",
                "oddish-staging-db",
            ],
        )
        self.assertEqual(
            set(args[9:]),
            {
                "ODDISH_DATABASE_URL=" + POOLED,
                "ODDISH_DASHBOARD_URL=https://staging.oddish.app",
                "CORS_ALLOWED_ORIGINS=https://staging.oddish.app",
                "LOGFIRE_ENVIRONMENT=staging",
            },
        )
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(output.getvalue(), "::add-mask::" + POOLED + "\n")

    def test_invalid_url_never_publishes(self):
        with (
            patch.dict(
                os.environ,
                {
                    "ODDISH_DATABASE_URL": DIRECT,
                    "ODDISH_RUNTIME_DATABASE_URL": "invalid",
                },
            ),
            patch.object(module.subprocess, "run") as run,
            self.assertRaises(ValueError),
        ):
            module.main()
        run.assert_not_called()

    def test_publish_failure_stops_deployment(self):
        with (
            patch.dict(
                os.environ,
                {"ODDISH_DATABASE_URL": DIRECT, "ODDISH_RUNTIME_DATABASE_URL": POOLED},
            ),
            patch.object(
                module.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, "modal"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            module.main()


if __name__ == "__main__":
    unittest.main()
