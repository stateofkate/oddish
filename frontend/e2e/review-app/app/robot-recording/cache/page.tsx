"use client";

import { useState } from "react";
import { useRunPlayback } from "@/lib/use-run-playback";
import { RunOutcome, RunPlayback } from "@/components/run-playback";

export default function CachePlaybackFixture() {
  const [revision, setRevision] = useState("cache-v1");
  const playback = useRunPlayback("/cache-playback", {
    manifest_path: "verifier/artifacts/playback.json", index_status: "ready",
    index_path: `_oddish/playback-index/${revision}.json`, index_revision: revision, index_error: null,
  });
  return <>
    <button onClick={async () => {
      await fetch("/cache-playback/control", { method: "POST", body: "cache-v2" });
      setRevision("cache-v2");
    }}>Publish new revision</button>
    <RunOutcome playback={playback} />
    <div className="h-[700px]">
      <RunPlayback playback={playback} active observationId={null} traceHref={() => "#"} onOpenTrace={() => {}} />
    </div>
  </>;
}
