"""The Archil rollout percentage set by a deploy workflow reaches the image
env; an unset one leaves the code default (0) alone.

Imports modal_app in a subprocess rather than reloading it here, because a
reload leaves other modules holding the old module's secret objects."""

import json
import os
import subprocess
import sys
from pathlib import Path

import modal_app

_CODE = """
import json
import modal_app
print(json.dumps(modal_app.ENV_VARS.get("ODDISH_ARCHIL_TRAFFIC_PERCENT")))
"""


def _baked_value(env: dict[str, str]) -> str | None:
    result = subprocess.run(
        [sys.executable, "-c", _CODE],
        cwd=Path(modal_app.__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_archil_traffic_percent_is_baked_into_the_image() -> None:
    assert _baked_value({**os.environ, "ODDISH_ARCHIL_TRAFFIC_PERCENT": "1"}) == "1"


def test_unset_archil_traffic_percent_is_not_baked() -> None:
    env = {**os.environ}
    env.pop("ODDISH_ARCHIL_TRAFFIC_PERCENT", None)
    # A developer's backend/.env may set it; only the deploy env is under test.
    if "ODDISH_ARCHIL_TRAFFIC_PERCENT" not in modal_app.LOCAL_DOTENV_VARS:
        assert _baked_value(env) is None
