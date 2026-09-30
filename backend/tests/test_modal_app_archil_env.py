"""Archil rollout settings are baked without changing the Modal default."""

import json
import os
import subprocess
import sys
from pathlib import Path

import modal_app

_CODE = """
import json
import modal
captured = []
original = modal.Secret.from_dict
def capture(values, *args, **kwargs):
    captured.append(values)
    return original(values, *args, **kwargs)
modal.Secret.from_dict = capture
import modal_app
pin = next(values for values in captured if "ODDISH_HARBOR_ENVIRONMENT" in values)
print(json.dumps([modal_app.ENV_VARS["ODDISH_HARBOR_ENVIRONMENT"], modal_app.ENV_VARS["ODDISH_ARCHIL_TRAFFIC_PERCENT"], pin["ODDISH_ARCHIL_TRAFFIC_PERCENT"]]))
"""


def _baked_value(env: dict[str, str]) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", _CODE],
        cwd=Path(modal_app.__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_archil_traffic_percent_is_baked_with_modal_default() -> None:
    assert _baked_value({**os.environ, "ODDISH_ARCHIL_TRAFFIC_PERCENT": "40"}) == [
        "modal",
        "40",
        "40",
    ]


def test_unset_archil_traffic_percent_defaults_to_zero() -> None:
    env = {**os.environ}
    env.pop("ODDISH_ARCHIL_TRAFFIC_PERCENT", None)
    # A developer's backend/.env may set it; only the deploy env is under test.
    if "ODDISH_ARCHIL_TRAFFIC_PERCENT" not in modal_app.LOCAL_DOTENV_VARS:
        assert _baked_value(env) == ["modal", "0", "0"]
