"""The builder follows the Dockerfile, on the warm-up prebuild and the trial build.

Regression: QA40 task 30 (Cabal, classic Dockerfile) reconfigured and refused
after a BuildKit export; tasks 33 and 36 (``# syntax=``, ``RUN --network=none``)
cannot be built by the classic builder at all.
"""

from __future__ import annotations

import pytest

from oddish.mon import builder, task_image

CLASSIC = "FROM haskell:9.6\nRUN cabal update && cabal build\nCOPY . /app\n"
SYNTAX = "# syntax=docker/dockerfile:1.7\nFROM node:22\nRUN --network=none codex --version\n"


@pytest.mark.parametrize(
    "text",
    [
        SYNTAX,
        "FROM x\nRUN --network=none true\n",
        "FROM x\nRUN --mount=type=cache,target=/root/.m2 mvn -B package\n",
        "FROM x\nRUN --mount=type=secret,id=tok --network=none true\n",
        "FROM x\nCOPY --link a b\n",
        "FROM x\nADD --checksum=sha256:abc https://e/x /x\n",
        "FROM x\nRUN <<EOF\napt-get update\nEOF\n",
    ],
)
def test_buildkit_only_syntax_selects_buildkit(text):
    assert builder.uses_buildkit(text)


@pytest.mark.parametrize(
    "text",
    [
        CLASSIC,
        "FROM x\nRUN echo --network=none\n",  # not an instruction flag
        "FROM x\nCOPY --chown=0:0 repo/ /app/\nRUN cat file << /dev/null\n",
        "# a comment about syntax=\nFROM x\n",
    ],
)
def test_classic_dockerfiles_keep_the_classic_builder(text):
    assert not builder.uses_buildkit(text)


def test_env_follows_the_task_dockerfile(tmp_path):
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment" / "Dockerfile").write_text(CLASSIC)
    assert builder.env_for(tmp_path) == {"DOCKER_BUILDKIT": "0", "COMPOSE_DOCKER_CLI_BUILD": "0"}
    (tmp_path / "environment" / "Dockerfile").write_text(SYNTAX)
    assert builder.env_for(tmp_path) == {"DOCKER_BUILDKIT": "1", "COMPOSE_DOCKER_CLI_BUILD": "1"}


@pytest.mark.asyncio
async def test_prebuild_runs_with_the_selected_builder(tmp_path, monkeypatch):
    (tmp_path / "task.toml").write_text("[environment]\n")
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment" / "Dockerfile").write_text(CLASSIC)
    monkeypatch.setenv("DOCKER_BUILDKIT", "1")
    seen = {}

    async def execute(*args, **kwargs):
        seen.update(kwargs.get("env") or {})

        async def communicate():
            return b"", None

        class P:
            returncode = 0

        P.communicate = staticmethod(communicate)
        return P()

    monkeypatch.setattr(task_image.asyncio, "create_subprocess_exec", execute)
    assert await task_image.build(tmp_path)
    assert seen["DOCKER_BUILDKIT"] == "0" and seen["COMPOSE_DOCKER_CLI_BUILD"] == "0"
