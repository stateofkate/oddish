"use client";

import { useState } from "react";
import { RerunRenderer } from "@/components/renderers/rerun-renderer";
import { parsePlayback } from "@/lib/run-playback";
import recording from "../../../../fixtures/robot-embedded-playback.json";

const manifest = parsePlayback(recording)!;

export default function RecordingResetFixture() {
  const [observationId, setObservationId] = useState("o0");
  const [playback, setPlayback] = useState(manifest);
  // A new key stands for another trial attempt or index revision.
  const [generation, setGeneration] = useState(0);
  return <>
    <button onClick={() => setObservationId("o2")}>Change resume frame</button>
    <button onClick={() => setPlayback(current => ({ ...current, observations: [...current.observations] }))}>Refresh observations</button>
    <button onClick={() => setGeneration(generation + 1)}>Replace recording</button>
    <div className="h-[700px]">
      <RerunRenderer key={generation} url="/api/trials/robot-0/files/verifier/artifacts/recording.rrd"
        fileName="recording.rrd" playback={playback} observationId={observationId} />
    </div>
  </>;
}
