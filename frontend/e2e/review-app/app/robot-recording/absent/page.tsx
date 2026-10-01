"use client";

import { RobotTrialPreview } from "../../../robot-trial";

export default function OrdinaryTrialPreview() {
  return <RobotTrialPreview playback={{
    manifest_path: null,
    index_status: "absent",
    index_path: null,
    index_revision: null,
    index_error: null,
  }} />;
}
