"""Generate the tiny synthetic playback fixtures with rerun-sdk==0.38.1.

Real MuJoCo tasks and robot meshes are uploaded through Oddish, not committed.
robot-embedded-recording.rrd embeds the scene-only layout the robot task's
record_rerun.py writes (manifest viewer_layout="embedded_scene") and the agent
camera JPGs.
"""
import json
from pathlib import Path

import numpy as np
from PIL import Image
import rerun as rr
import rerun.blueprint as rrb

root = Path(__file__).parent


def record(application, name, blueprint, timeline, start, end, frames):
    stream = rr.RecordingStream(application)
    stream.save(root / name, default_blueprint=blueprint)
    stream.log("world", rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)
    stream.log("world/tray", rr.Boxes3D(half_sizes=[.2, .2, .02],
        centers=[.5, 0, 0], colors=[70, 110, 80], fill_mode="solid"), static=True)
    stream.log("world/block", rr.Boxes3D(half_sizes=[.04, .04, .04],
        colors=[210, 50, 45], fill_mode="solid"), static=True)
    for index in range(frames + 1):
        stream.set_time(timeline, duration=start + (end - start) * index / frames)
        stream.log("world/block", rr.Transform3D(translation=[.5 * index / frames, 0, .06]))
    stream.flush()
    stream.disconnect()


episode = json.loads((root / "robot-embedded-playback.json").read_text())
record("oddish-embedded-playback-test", "robot-embedded-recording.rrd",
    rrb.Blueprint(
        rrb.Spatial3DView(name="Reviewer: complete scene", origin="world",
            contents="world/**", background=[30, 35, 42], line_grid=False),
        rrb.TopPanel(state="hidden"), rrb.TimePanel(state="hidden"),
        rrb.BlueprintPanel(state="hidden"), rrb.SelectionPanel(state="hidden"),
        auto_layout=False, auto_views=False),
    episode["timeline"], episode["start_time"], episode["duration_seconds"], 120)
for i, observation in enumerate(episode["observations"]):
    pixels = np.full((48, 64, 3), 210, dtype=np.uint8)
    pixels[20:30, 10 + i * 10:20 + i * 10] = [210, 50, 45]
    Image.fromarray(pixels).save(root / observation["image"], quality=85)
