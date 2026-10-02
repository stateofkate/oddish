"use client";

import { RobotTrialPreview } from "../../../robot-trial";

export default function InvalidRobotRecording() {
  return <RobotTrialPreview playback={{
    manifest_path: "verifier/artifacts/playback.json",
    index_status: "invalid",
    index_path: null,
    index_revision: null,
    index_error: "missing_reference",
  }} />;
}
