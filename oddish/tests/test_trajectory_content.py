import json

import pytest
from harbor.agents.installed.codex import Codex

from oddish.core.trajectory_content import normalize_codex_trajectory
from oddish.workers.agents.codex import OddishCodex

BLOCKS = [
    {
        "type": "input_text",
        "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
    },
    {"type": "input_text", "text": '{"output": "ok\\n", "exit_code": 0}'},
]
PARTS = [{"type": "text", "text": block["text"]} for block in BLOCKS]


@pytest.mark.parametrize(
    "raw",
    [BLOCKS, json.dumps(BLOCKS), {"output": BLOCKS, "metadata": {"exit_code": 0}}],
)
def test_codex_text_blocks_stay_typed(raw):
    content, metadata = OddishCodex._parse_output_blob(raw)
    assert content == PARTS
    assert metadata == ({"exit_code": 0} if isinstance(raw, dict) else None)


@pytest.mark.parametrize("raw", ["plain output", '{"output": "text"}', [{"x": 1}]])
def test_other_codex_outputs_keep_harbor_parsing(raw):
    assert OddishCodex._parse_output_blob(raw) == Codex._parse_output_blob(raw)


def test_codex_session_converts_exec_output_to_text_parts(tmp_path):
    events = [
        {"type": "session_meta", "payload": {"id": "s", "cli_version": "test"}},
        {
            "type": "response_item",
            "timestamp": "2026-09-22T18:00:01Z",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "exec-1",
                "name": "exec",
                "input": 'text(await tools.exec_command({cmd: "ls"}));',
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-22T18:00:02Z",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "exec-1",
                "output": BLOCKS,
            },
        },
    ]
    (tmp_path / "session.jsonl").write_text("\n".join(map(json.dumps, events)))
    agent = OddishCodex(logs_dir=tmp_path, model_name="example-model")
    trajectory = agent._convert_events_to_trajectory(tmp_path).to_json_dict()
    [result] = trajectory["steps"][-1]["observation"]["results"]
    assert result == {"source_call_id": "exec-1", "content": PARTS}


def _stored(content, agent="codex"):
    return {
        "agent": {"name": agent},
        "steps": [{"observation": {"results": [{"content": content}]}}],
    }


def test_stored_reprs_are_repaired_on_read():
    repaired = normalize_codex_trajectory(_stored(str(BLOCKS)))
    assert repaired == _stored(PARTS)


@pytest.mark.parametrize(
    "trajectory",
    [
        _stored(str(BLOCKS), agent="claude-code"),
        _stored("[{'type': 'input_text', 'text': 'cut off"),
        _stored(str([{"type": "input_image", "image_url": "data:"}])),
        _stored("[{} is not a literal"),
        None,
    ],
)
def test_other_stored_content_is_unchanged(trajectory):
    before = json.dumps(trajectory)
    assert json.dumps(normalize_codex_trajectory(trajectory)) == before
