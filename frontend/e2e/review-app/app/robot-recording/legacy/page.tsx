"use client";

import { RobotTrialPreview } from "../../../robot-trial";

/** A trial stored before playback indexing: it has no descriptor at all. */
export default function LegacyTrialPreview() {
  return <RobotTrialPreview playback={null} />;
}
