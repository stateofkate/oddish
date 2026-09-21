"""Startup dependencies must be copied into every deployed backend image."""

import pytest

from oddish.core.harbor_source import HARBOR_VARIANTS


@pytest.mark.parametrize("variant", [None, *HARBOR_VARIANTS.values()])
def test_image_contains_org_approval_module(monkeypatch, variant):
    import modal_app

    copied_modules = set()
    original = modal_app.modal.Image.add_local_python_source

    def capture_sources(image, *modules, **kwargs):
        if kwargs.get("copy"):
            copied_modules.update(modules)
        return original(image, *modules, **kwargs)

    monkeypatch.setattr(
        modal_app.modal.Image, "add_local_python_source", capture_sources
    )
    modal_app._build_worker_image(variant)

    # Both API auth and worker startup import this top-level module. uv_sync
    # installs dependencies only, so setuptools py-modules cannot supply it.
    assert {"org_access", "endpoint_health", "endpoint_health_worker"} <= copied_modules


@pytest.mark.parametrize("variant", [None, *HARBOR_VARIANTS.values()])
def test_dependency_layers_do_not_include_source_or_deployment_settings(
    monkeypatch, variant
):
    import modal_app

    operations = []
    for name in ("env", "add_local_dir", "add_local_file", "uv_sync", "run_commands"):
        original = getattr(modal_app.modal.Image, name)

        def capture(image, *args, _name=name, _original=original, **kwargs):
            operations.append((_name, args, kwargs))
            return _original(image, *args, **kwargs)

        monkeypatch.setattr(modal_app.modal.Image, name, capture)

    monkeypatch.setitem(modal_app.ENV_VARS, "MODAL_APP_NAME", "oddish-pr-new")
    modal_app._build_worker_image(variant)
    sync = next(i for i, op in enumerate(operations) if op[0] == "uv_sync")
    assert operations[sync][2]["extra_options"] == "--no-install-package oddish"
    assert not any(op[0] == "add_local_dir" for op in operations[:sync])
    assert [op[1][0] for op in operations[:sync] if op[0] == "env"] == [
        {"UV_LINK_MODE": "copy"}
    ]
    # Copy mode must already apply to the dependency sync, Harbor override,
    # and editable Oddish install. Deployment-specific settings stay later.
    effective_env = {}
    for name, args, _kwargs in operations:
        if name == "env":
            effective_env.update(args[0])
        elif name == "uv_sync" or (
            name == "run_commands" and any("uv pip install" in cmd for cmd in args)
        ):
            assert effective_env["UV_LINK_MODE"] == "copy"
            assert "MODAL_APP_NAME" not in effective_env
    assert all(
        "pyproject.toml" in op[1][0]
        for op in operations[:sync]
        if op[0] == "add_local_file"
    )
    source = next(i for i, op in enumerate(operations) if op[0] == "add_local_dir")
    install = next(
        i
        for i, op in enumerate(operations)
        if op[0] == "run_commands" and "--no-deps -e /oddish" in op[1][0]
    )
    settings = next(
        i
        for i, op in enumerate(operations)
        if op[0] == "env" and "MODAL_APP_NAME" in op[1][0]
    )
    assert sync < source < install < settings
    if variant is not None:
        override = next(
            i
            for i, op in enumerate(operations)
            if op[0] == "run_commands"
            and "uv pip install" in op[1][0]
            and "--no-deps" not in op[1][0]
        )
        assert sync < override < source
