from __future__ import annotations

import json
from pathlib import Path

import pytest

from oddish.workers.agents.codex_stdout_trajectory import write_trajectory_if_richer


def _write_stdout(
    path: Path, *, reasoning: str = "Choosing an edit", command: str = "ls"
) -> None:
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "reasoning", "text": reasoning},
        },
        {
            "type": "item.started",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": command,
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": command,
                "aggregated_output": "README.md\n",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "item_2", "type": "agent_message", "text": "Done."},
        },
    ]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def _write_saved(path: Path, steps: list[dict]) -> str:
    original = json.dumps(
        {
            "schema_version": "ATIF-v1.5",
            "agent": {"name": "codex", "version": "test"},
            "steps": steps,
        }
    )
    path.write_text(original)
    return original


def _fallback(tmp_path: Path):
    return write_trajectory_if_richer(
        existing_trajectory_path=tmp_path / "trajectory.json",
        output_path=tmp_path / "codex.txt",
        agent_version="test",
        model_name="test-model",
    )


def _shell_call(call_id: str = "call_1") -> dict:
    return {
        "tool_call_id": call_id,
        "function_name": "shell_command",
        "arguments": {"command": "ls"},
    }


@pytest.mark.parametrize("saved_detail", ["both", "reasoning", "tool"])
def test_stdout_cannot_replace_saved_reasoning_or_tools(tmp_path, saved_detail):
    steps = [
        {
            "step_id": 1,
            "source": "agent",
            "message": "",
            "tool_calls": [_shell_call()],
        },
        {"step_id": 2, "source": "agent", "message": "Done."},
    ]
    if saved_detail in {"both", "reasoning"}:
        steps[0][
            "reasoning_content"
        ] = "I read the task, compared the rules, and chose this edit."
    if saved_detail in {"both", "tool"}:
        steps[1]["tool_calls"] = [
            {
                "tool_call_id": "call_2",
                "function_name": "apply_patch",
                "arguments": {"patch": "update file"},
            }
        ]
    original = _write_saved(tmp_path / "trajectory.json", steps)
    _write_stdout(tmp_path / "codex.txt")

    assert _fallback(tmp_path) is None
    assert (tmp_path / "trajectory.json").read_text() == original


def test_stdout_start_and_end_do_not_count_as_two_saved_tool_calls(tmp_path):
    steps = [
        {
            "step_id": index + 1,
            "source": "agent",
            "message": "",
            "tool_calls": [_shell_call(f"call_{index}")],
        }
        for index in range(2)
    ]
    original = _write_saved(tmp_path / "trajectory.json", steps)
    _write_stdout(tmp_path / "codex.txt")

    assert _fallback(tmp_path) is None
    assert (tmp_path / "trajectory.json").read_text() == original


@pytest.mark.parametrize("command_key", ["command", "cmd", "script", "shell_command"])
def test_stdout_can_add_work_when_saved_details_survive(tmp_path, command_key):
    call = _shell_call()
    call["arguments"] = {command_key: "ls"}
    _write_saved(
        tmp_path / "trajectory.json",
        [
            {
                "step_id": 1,
                "source": "agent",
                "message": "",
                "reasoning_content": "Choosing\n\nan edit",
                "tool_calls": [call],
            }
        ],
    )
    _write_stdout(tmp_path / "codex.txt")

    trajectory = _fallback(tmp_path)

    assert trajectory is not None
    assert len(trajectory.steps) == 4
    assert trajectory.steps[0].reasoning_content == "Choosing an edit"
    assert trajectory.steps[-1].message == "Done."


def test_stdout_restores_a_sparse_trace(tmp_path):
    _write_saved(
        tmp_path / "trajectory.json",
        [{"step_id": 1, "source": "system", "message": "setup"}],
    )
    _write_stdout(tmp_path / "codex.txt")

    trajectory = _fallback(tmp_path)

    assert trajectory is not None
    assert len(trajectory.steps) == 4
    assert trajectory.steps[0].reasoning_content == "Choosing an edit"


@pytest.mark.parametrize(
    "saved_command,stdout_command",
    [
        ("ls", "/bin/bash -lc ls"),
        ("/bin/bash -lc 'ls'", "ls"),
        (["bash", "-lc", "ls"], "ls"),
        (["ls"], "ls"),
    ],
)
def test_stdout_recognizes_saved_shell_command_forms(
    tmp_path, saved_command, stdout_command
):
    call = _shell_call()
    call["arguments"]["command"] = saved_command
    _write_saved(
        tmp_path / "trajectory.json",
        [{"step_id": 1, "source": "agent", "message": "", "tool_calls": [call]}],
    )
    _write_stdout(tmp_path / "codex.txt", command=stdout_command)

    assert _fallback(tmp_path) is not None


def test_stdout_cannot_drop_extra_shell_arguments(tmp_path):
    call = _shell_call()
    call["arguments"]["workdir"] = "/workspace"
    original = _write_saved(
        tmp_path / "trajectory.json",
        [{"step_id": 1, "source": "agent", "message": "", "tool_calls": [call]}],
    )
    _write_stdout(tmp_path / "codex.txt")

    assert _fallback(tmp_path) is None
    assert (tmp_path / "trajectory.json").read_text() == original
