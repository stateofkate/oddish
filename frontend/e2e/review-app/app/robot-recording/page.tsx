"use client";

import { READY_PLAYBACK, RobotTrialPreview } from "../../robot-trial";

export default function RobotRecordingPreview() {
  return <RobotTrialPreview playback={READY_PLAYBACK} />;
}
