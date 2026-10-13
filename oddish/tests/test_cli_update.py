"""``oddish update`` for ``uv pip install oddish``."""

from __future__ import annotations

import pytest
from oddish.cli import app
from oddish.cli._package import InstallInfo, PackageError, upgrade_command
from typer.testing import CliRunner

runner = CliRunner()


def _info(**overrides) -> InstallInfo:
    payload = {
        "version": "0.1.13",
        "source": "pypi",
        "manager": "uv-pip",
        "installer": "uv",
    }
    payload.update(overrides)
    return InstallInfo(**payload)


def _which_uv(name: str) -> str | None:
    return "/usr/bin/uv" if name == "uv" else None


def test_upgrade_command_uv_and_pip():
    assert upgrade_command(
        _info(manager="uv-pip"), executable="/opt/venv/bin/python", which=_which_uv
    ) == ["uv", "pip", "install", "--python", "/opt/venv/bin/python", "--upgrade", "oddish"]
    assert upgrade_command(
        _info(manager="pip"), executable="/opt/venv/bin/python", which=lambda _n: None
    ) == ["/opt/venv/bin/python", "-m", "pip", "install", "--upgrade", "oddish"]


def test_upgrade_command_refusals():
    with pytest.raises(PackageError, match="editable"):
        upgrade_command(_info(source="editable", editable_path="/tmp/oddish"), which=_which_uv)
    with pytest.raises(PackageError, match="uv pip install oddish"):
        upgrade_command(_info(source="other"), which=_which_uv)
    with pytest.raises(PackageError, match="uv"):
        upgrade_command(_info(manager="uv-pip"), which=lambda _n: None)


def test_upgrade_command_force_same_version_reinstalls_without_deps():
    """Same-version --force refreshes oddish only (pip --no-deps / uv reinstall-package)."""
    uv_command = upgrade_command(
        _info(manager="uv-pip"),
        executable="/opt/venv/bin/python",
        which=_which_uv,
        force=True,
        pin_version="0.1.13",
    )
    assert uv_command == [
        "uv",
        "pip",
        "install",
        "--python",
        "/opt/venv/bin/python",
        "--reinstall-package",
        "oddish",
        "--upgrade",
        "oddish==0.1.13",
    ]
    pip_command = upgrade_command(
        _info(manager="pip"),
        executable="/opt/venv/bin/python",
        which=lambda _n: None,
        force=True,
        pin_version="0.1.13",
    )
    assert pip_command == [
        "/opt/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        "--upgrade",
        "oddish==0.1.13",
    ]
    assert "--no-deps" in pip_command


def test_upgrade_command_force_upgrade_resolves_pip_deps():
    """Forced upgrade must not pass pip --no-deps so new requires are installed."""
    pip_upgrade = upgrade_command(
        _info(manager="pip", version="0.1.13"),
        executable="/opt/venv/bin/python",
        which=lambda _n: None,
        force=True,
    )
    assert pip_upgrade == [
        "/opt/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "oddish",
    ]
    assert "--no-deps" not in pip_upgrade

    pip_pinned_newer = upgrade_command(
        _info(manager="pip", version="0.1.13"),
        executable="/opt/venv/bin/python",
        which=lambda _n: None,
        force=True,
        pin_version="0.2.0",
    )
    assert pip_pinned_newer == [
        "/opt/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "oddish==0.2.0",
    ]
    assert "--no-deps" not in pip_pinned_newer

    uv_upgrade = upgrade_command(
        _info(manager="uv-pip", version="0.1.13"),
        executable="/opt/venv/bin/python",
        which=_which_uv,
        force=True,
    )
    assert uv_upgrade == [
        "uv",
        "pip",
        "install",
        "--python",
        "/opt/venv/bin/python",
        "--reinstall-package",
        "oddish",
        "--upgrade",
        "oddish",
    ]


def _patch_update(monkeypatch, info: InstallInfo, latest: str = "0.1.14") -> None:
    monkeypatch.setattr("oddish.cli.update.inspect_install", lambda: info)
    monkeypatch.setattr("oddish.cli.update.fetch_pypi_latest", lambda: latest)
    monkeypatch.setattr(
        "oddish.cli.update.upgrade_command",
        lambda _info, **_kwargs: ["echo", "uv pip install --upgrade oddish"],
    )


def test_update_dry_run_and_already_latest(monkeypatch):
    _patch_update(monkeypatch, _info())
    dry = runner.invoke(app, ["update", "--dry-run"])
    assert dry.exit_code == 0 and "uv pip install" in dry.stdout
    _patch_update(monkeypatch, _info(), latest="0.1.13")
    latest = runner.invoke(app, ["update"])
    assert latest.exit_code == 0 and "already up to date" in latest.stdout


def test_update_force_pins_when_current(monkeypatch):
    captured: list[dict] = []

    def _capture(_info, **kwargs):
        captured.append(kwargs)
        return ["uv", "pip", "install", "--reinstall-package", "oddish", "--upgrade", "oddish==0.1.13"]

    monkeypatch.setattr("oddish.cli.update.inspect_install", lambda: _info())
    monkeypatch.setattr("oddish.cli.update.fetch_pypi_latest", lambda: "0.1.13")
    monkeypatch.setattr("oddish.cli.update.upgrade_command", _capture)
    result = runner.invoke(app, ["update", "--force", "--dry-run"])
    assert result.exit_code == 0
    assert captured == [{"force": True, "pin_version": "0.1.13"}]
    monkeypatch.setattr("oddish.cli.update.fetch_pypi_latest", lambda: "0.2.0")
    captured.clear()
    result = runner.invoke(app, ["update", "--force", "--dry-run"])
    assert result.exit_code == 0
    assert captured == [{"force": True, "pin_version": None}]


def test_update_force_pins_when_pypi_unreachable(monkeypatch):
    captured: list[dict] = []

    def _capture(_info, **kwargs):
        captured.append(kwargs)
        return ["echo", "force-reinstall"]

    def _fail_pypi() -> str:
        raise PackageError("Could not reach PyPI")

    monkeypatch.setattr("oddish.cli.update.inspect_install", lambda: _info())
    monkeypatch.setattr("oddish.cli.update.fetch_pypi_latest", _fail_pypi)
    monkeypatch.setattr("oddish.cli.update.upgrade_command", _capture)
    result = runner.invoke(app, ["update", "--force", "--dry-run"])
    assert result.exit_code == 0
    assert captured == [{"force": True, "pin_version": "0.1.13"}]


def test_update_check_and_editable(monkeypatch):
    _patch_update(monkeypatch, _info(), latest="0.2.0")
    check = runner.invoke(app, ["update", "--check", "--json"])
    assert check.exit_code == 1 and '"update_available": true' in check.stdout
    monkeypatch.setattr(
        "oddish.cli.update.inspect_install",
        lambda: _info(source="editable", editable_path="/tmp/oddish"),
    )
    monkeypatch.setattr("oddish.cli.update.upgrade_command", upgrade_command)
    refused = runner.invoke(app, ["update"])
    assert refused.exit_code == 1 and "editable" in refused.output.lower()


def test_update_runs_and_names_pip(monkeypatch):
    _patch_update(monkeypatch, _info(manager="pip"))
    monkeypatch.setattr(
        "oddish.cli.update.upgrade_command", lambda _info, **_kwargs: ["echo", "upgrade"]
    )

    class _Result:
        returncode = 0

    monkeypatch.setattr(
        "oddish.cli.update.subprocess.run", lambda command, check=False, **_k: _Result()
    )
    monkeypatch.setattr("oddish.cli.update.query_installed_version", lambda _exe: "0.1.14")
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "via pip" in result.output and "via uv pip" not in result.output
    assert "0.1.14" in result.stdout


def test_update_help_lists_options():
    result = runner.invoke(app, ["update", "--help"])
    assert result.exit_code == 0
    assert all(flag in result.output for flag in ("--dry-run", "--check", "--force"))


def test_homebrew_update_never_queries_or_installs_from_pypi(monkeypatch):
    import importlib
    module = importlib.import_module('oddish.cli.update')
    monkeypatch.setattr(module, 'inspect_install', lambda: _info(source='homebrew', manager='brew'))
    monkeypatch.setattr(module, 'fetch_pypi_latest', lambda: pytest.fail('Homebrew must not query PyPI'))
    result = runner.invoke(app, ['update', '--dry-run', '--json'])
    assert result.exit_code == 1
    assert 'brew upgrade abundant-ai/tap/oddish' in result.stdout


def test_homebrew_upgrade_command_refuses_to_modify_managed_environment():
    with pytest.raises(PackageError, match='brew upgrade'):
        upgrade_command(_info(source='homebrew', manager='brew'))


def _patch_script_install(monkeypatch, latest: str = "0.1.20") -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def _latest(api_url: str, api_key: str) -> str:
        calls.append((api_url, api_key))
        return latest

    monkeypatch.setattr(
        "oddish.cli.update.inspect_install", lambda: _info(source="script", manager="script")
    )
    monkeypatch.setattr(
        "oddish.cli.update.fetch_pypi_latest",
        lambda: pytest.fail("script installs must not query PyPI"),
    )
    monkeypatch.setattr("oddish.cli.update.fetch_api_latest", _latest)
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example.test")
    return calls


def test_script_install_update_needs_api_key(monkeypatch):
    _patch_script_install(monkeypatch)
    monkeypatch.delenv("ODDISH_API_KEY", raising=False)
    result = runner.invoke(app, ["update", "--json"])
    assert result.exit_code == 1
    assert "ODDISH_API_KEY" in result.stdout
    with pytest.raises(PackageError, match="Oddish API"):
        upgrade_command(_info(source="script", manager="script"))


def test_script_install_update_checks_and_reruns_install_script(monkeypatch):
    calls = _patch_script_install(monkeypatch)
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    check = runner.invoke(app, ["update", "--check", "--json"])
    assert check.exit_code == 1 and '"update_available": true' in check.stdout
    assert calls == [("https://api.example.test", "ok_test")]

    ran: list[tuple[list[str], dict[str, str]]] = []

    class _Result:
        returncode = 0

    def _run(command, check=False, env=None, **_kwargs):
        ran.append((command, env))
        return _Result()

    monkeypatch.setattr("oddish.cli.update.subprocess.run", _run)
    monkeypatch.setattr("oddish.cli.update.query_installed_version", lambda _exe: "0.1.20")
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert "via install.sh" in result.output and "0.1.20" in result.output
    [(command, env)] = ran
    assert command == [
        "sh",
        "-c",
        "curl -fsSL https://api.example.test/cli/install.sh | sh",
    ]
    assert env["ODDISH_API_KEY"] == "ok_test"
    assert env["ODDISH_API_URL"] == "https://api.example.test"


def test_script_install_already_latest_skips_install(monkeypatch):
    _patch_script_install(monkeypatch, latest="0.1.13")
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setattr(
        "oddish.cli.update.subprocess.run",
        lambda *_a, **_k: pytest.fail("already-latest must not reinstall"),
    )
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0 and "already up to date" in result.stdout


@pytest.mark.parametrize(
    "body", [b"<html>502</html>", b"", b"[]", b'"0.1.20"', b'{"version": 1}', b"\xff"]
)
def test_fetch_api_latest_bad_body_is_package_error(monkeypatch, body):
    import httpx
    from oddish.cli._package import fetch_api_latest

    monkeypatch.setattr(
        "oddish.cli._package.httpx.get",
        lambda *_a, **_k: httpx.Response(200, content=body),
    )
    with pytest.raises(PackageError, match="did not return a CLI version"):
        fetch_api_latest("https://api.example.test", "ok_test")
