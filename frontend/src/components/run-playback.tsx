"use client";

import { useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import useSWR from "swr";
import { CheckCircle2, XCircle, CircleHelp } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AgentElapsedTime, type AgentTiming } from "@/components/agent-elapsed-time";
import { ArtifactFrameCommand } from "@/components/artifact-frame-command";
import { PlaybackResources } from "@/components/renderers/observation-images";
import { fetcher } from "@/lib/api";
import type {
  RunPlayback as RunPlaybackState,
  RunPlaybackSource,
} from "@/lib/use-run-playback";

const RerunRenderer = dynamic(
  () => import("./renderers/rerun-renderer").then((m) => m.RerunRenderer),
  { ssr: false }
);

interface TraceLinks {
  traceHref: (step: number, observationId: string) => string;
  onOpenTrace: (step: number, observationId: string) => void;
}

/**
 * The Run tab's scene and agent images. The drawer keeps this mounted while
 * other views cover it (`active` false), so returning reuses the viewer.
 */
export function RunPlayback({
  playback,
  active,
  observationId,
  agentTiming,
  ...links
}: TraceLinks & {
  playback: RunPlaybackState;
  active: boolean;
  /** Observation to show, e.g. when returning from its trace step. */
  observationId: string | null;
  agentTiming?: AgentTiming | null;
}) {
  const data = playback.data;
  if (playback.isLoading && !playback.error)
    return (
      <p role="status" className="p-6 text-sm">
        Loading playback…
      </p>
    );
  if (playback.error || !data)
    return (
      <div className="space-y-3 p-6 text-sm">
        <p role="alert">
          {playback.error?.message ?? (playback.preparation === "unchecked"
            ? "Playback has not been prepared for this run."
            : "This run has no playback description.")}
        </p>
        {playback.error && playback.retry && <Button variant="secondary"
          disabled={playback.isRetrying} onClick={() => void playback.retry?.()}>
          Retry loading playback
        </Button>}
      </div>
    );
  return (
    <div className="h-full min-h-0">
      <section
        aria-label="Simulation playback"
        className="flex h-full min-h-0 min-w-0 flex-col"
      >
        <div className="border-border flex shrink-0 items-center gap-2 border-b px-3 py-1 text-xs">
          <span>{data.manifest.actions.length} actions</span>
          <AgentElapsedTime timing={agentTiming} />
        </div>
        <div className="min-h-0 flex-1">
          {/* One viewer and download set per index revision. */}
          <EpisodeViewer key={data.revision} data={data} active={active}
            observationId={observationId} {...links} />
        </div>
      </section>
    </div>
  );
}

function EpisodeViewer({
  data,
  active,
  observationId,
  traceHref,
  onOpenTrace,
}: TraceLinks & {
  data: RunPlaybackSource;
  active: boolean;
  observationId: string | null;
}) {
  const { manifest } = data;
  // Start the recording download and first image decode before the viewer
  // module finishes loading; both are released when this episode unmounts.
  const initial = useRef({ data, observationId });
  const [resources, setResources] = useState<PlaybackResources | null>(null);
  useEffect(() => {
    const { data, observationId } = initial.current;
    const observations = data.manifest.observations;
    const created = new PlaybackResources(
      data.recordingUrl,
      observations.map(o => data.imageUrl(o.image)),
      Math.max(0, observations.findIndex(o => o.id === observationId))
    );
    setResources(created);
    return () => {
      created.dispose();
      setResources(null);
    };
  }, []);
  if (!resources)
    return <p role="status" className="p-4 text-sm">Loading recording…</p>;
  return (
    <RerunRenderer
      url={data.recordingUrl}
      fileName={manifest.recording}
      playback={manifest}
      resources={resources}
      active={active}
      observationId={observationId}
      renderObservation={observation => <ArtifactFrameCommand
        episode={manifest}
        observation={observation}
        command={data.commands[observation.id] ?? null}
        traceHref={traceHref}
        onOpenTrace={onOpenTrace}
      />}
    />
  );
}

const CHECK_STATES = new Map<unknown, [typeof CheckCircle2, string, string]>([
  [true, [CheckCircle2, "text-emerald-500", "Passed: "]],
  [false, [XCircle, "text-red-500", "Failed: "]],
]);

/** The goal and final checks; they describe the finished attempt only. */
export function RunOutcome({ playback }: { playback: RunPlaybackState }) {
  const data = playback.data;
  const outcome = useSWR<{ checks?: Record<string, unknown>; error?: string }>(
    data?.outcomeUrl ?? null,
    fetcher,
    { revalidateOnFocus: false }
  );
  if (!data) return null;
  return (
    <aside className="border-border space-y-4 rounded-lg border p-4">
      <section>
        <h3 className="mb-2 text-sm font-medium">Goal</h3>
        <p className="text-muted-foreground text-sm leading-relaxed">{data.manifest.goal}</p>
      </section>
      <details>
        <summary className="mb-3 cursor-pointer text-sm font-medium">Final checks</summary>
        {outcome.error ? (
          <p role="alert" className="text-sm">Could not load verification results.</p>
        ) : outcome.isLoading ? (
          <p role="status">Loading checks…</p>
        ) : (
          <ul className="space-y-3">
            {data.manifest.checks.map(check => {
              const [Icon, tone, label] = CHECK_STATES.get(outcome.data?.checks?.[check.key])
                ?? [CircleHelp, "text-muted-foreground", "Not evaluated: "];
              return (
                <li key={check.key} className="flex items-start gap-2 text-sm">
                  <Icon aria-hidden className={`mt-0.5 size-4 shrink-0 ${tone}`} />
                  <span><span className="sr-only">{label}</span>{check.label}</span>
                </li>
              );
            })}
          </ul>
        )}
        {typeof outcome.data?.error === "string" && (
          <p role="alert" className="mt-3 text-sm text-red-500">{outcome.data.error}</p>
        )}
      </details>
    </aside>
  );
}
