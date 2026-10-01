"use client";

import { useState } from "react";
import { TrajectoryViewer } from "@/components/trajectory-viewer";
import { LiveTranscriptPanel } from "@/components/live-transcript-panel";

export default function TrajectoryPreview() {
  const [width, setWidth] = useState<number | null>(null);
  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <h1 className="text-xl font-semibold">Trajectory rendering</h1>
      <p className="text-muted-foreground text-sm">
        Production trajectory components with recorded-shape fixture data.
      </p>
      <div className="flex gap-2 text-sm">
        {[null, 420].map((value) => (
          <button
            key={String(value)}
            type="button"
            className="rounded border px-2 py-1"
            onClick={() => setWidth(value)}
          >
            {value ? `${value}px panel` : "Full width"}
          </button>
        ))}
      </div>
      <div data-testid="trajectory-panel" style={{ width: width ?? undefined }}>
        <TrajectoryViewer trialId="trajectory-fixture" hasTrajectory />
      </div>
      <div data-testid="live-panel" className="h-96 rounded border">
        <LiveTranscriptPanel trialId="trajectory-fixture" agent="claude-code" />
      </div>
    </div>
  );
}
