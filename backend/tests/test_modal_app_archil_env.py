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
    assert _baked_value({**os.environ, "ODDISH_ARCHIL_TRAFFIC_PERCENT": "20"}) == [
        "modal",
        "20",
        "20",
    ]


def test_unset_archil_traffic_percent_defaults_to_zero() -> None:
    env = {**os.environ}
    env.pop("ODDISH_ARCHIL_TRAFFIC_PERCENT", None)
    # A developer's backend/.env may set it; only the deploy env is under test.
    if "ODDISH_ARCHIL_TRAFFIC_PERCENT" not in modal_app.LOCAL_DOTENV_VARS:
        assert _baked_value(env) == ["modal", "0", "0"]


_ARCHIL_NAMES = ("ARCHIL_REGIONS", "ARCHIL_FORWARD_ZONES", "ARCHIL_MAX_THROTTLE_WAIT")
_ARCHIL_CODE = """
import json
import sys
import modal
captured = []
original = modal.Secret.from_dict
def capture(values, *args, **kwargs):
    captured.append(values)
    return original(values, *args, **kwargs)
modal.Secret.from_dict = capture
import modal_app
pin = next(values for values in captured if "ODDISH_HARBOR_ENVIRONMENT" in values)
names = sys.argv[1:]
print(json.dumps({
    "image": [modal_app.ENV_VARS.get(name) for name in names],
    "pinned": [pin.get(name) for name in names],
}))
"""


def _archil_settings(**overrides: str) -> dict[str, object]:
    env = {**os.environ}
    for name in _ARCHIL_NAMES:
        env.pop(name, None)
    env.update(overrides)
    result = subprocess.run(
        [sys.executable, "-c", _ARCHIL_CODE, *_ARCHIL_NAMES],
        cwd=Path(modal_app.__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_forward_zones_and_capacity_wait_are_baked_but_regions_are_not() -> None:
    if any(name in modal_app.LOCAL_DOTENV_VARS for name in _ARCHIL_NAMES):
        return
    unset = _archil_settings()
    deployed = _archil_settings(
        ARCHIL_REGIONS="aws-us-west-2=60,aws-us-east-1=40",
        ARCHIL_FORWARD_ZONES="aws-us-east-1=prod-aws-us-east-1-green.archil.app",
        ARCHIL_MAX_THROTTLE_WAIT="300",
    )

    assert unset["image"] == unset["pinned"] == [None, "", "120"]
    # Baking ARCHIL_REGIONS would shadow oddish-prod, where it is edited live.
    expected = [None, "aws-us-east-1=prod-aws-us-east-1-green.archil.app", "300"]
    assert deployed["image"] == deployed["pinned"] == expected
