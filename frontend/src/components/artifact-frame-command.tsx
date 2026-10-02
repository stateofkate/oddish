"use client";

import { Button } from "@/components/ui/button";
import { isEditableTarget } from "@/lib/editable-target";
import type {
  PlaybackManifest,
  PlaybackObservation,
  RecordedCommand,
} from "@/lib/run-playback";
import { toolInput } from "@/lib/trajectory-view";

type Episode = Extract<PlaybackManifest, { version: 2 }>;

/**
 * The recorded command that returned one observation, with a link to its
 * trace step. Run and Files pass the indexed command; this component reads
 * nothing itself.
 */
export function ArtifactFrameCommand({
  episode,
  observation,
  command,
  traceHref,
  imageUrl,
  onSelectObservation,
  onOpenTrace,
}: {
  episode: Episode;
  observation: PlaybackObservation;
  command: RecordedCommand | null;
  /** Trace link for a step; the observation is kept for the return trip. */
  traceHref: (step: number, observationId: string) => string;
  /** Files only: thumbnails and previous/next selection. */
  imageUrl?: (image: string) => string;
  onSelectObservation?: (observationId: string) => void;
  onOpenTrace?: (step: number, observationId: string) => void;
}) {
  const { observations, actions } = episode;
  const index = observations.findIndex(item => item.id === observation.id);
  function selectOffset(offset: number) {
    const next = observations[index + offset];
    if (next) onSelectObservation?.(next.id);
  }
  const action = actions.find(item => item.result_observation_id === observation.id);
  // The existing trace formatter, applied to the arguments the indexer copied.
  const input = command ? toolInput(command.function_name, command.arguments) : null;
  const text = input ? input.copyText ?? input.code : null;
  const step = command?.step_id;

  return (
    <section
      aria-label="Command for recorded frame"
      className="border-border space-y-2 border-t px-3 py-2 text-xs"
      tabIndex={onSelectObservation ? 0 : undefined}
      onKeyDown={event => {
        if (!onSelectObservation || isEditableTarget(event.target) || event.altKey || event.ctrlKey || event.metaKey) return;
        if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
          event.preventDefault();
          selectOffset(event.key === "ArrowLeft" ? -1 : 1);
        }
      }}
    >
      {onSelectObservation && <nav aria-label="Observation navigation" className="flex items-center gap-2">
        <Button size="sm" variant="outline" aria-label="Previous observation image" disabled={index === 0} onClick={() => selectOffset(-1)}>←</Button>
        <span className="tabular-nums">Observation {index + 1} of {observations.length}</span>
        <Button size="sm" variant="outline" aria-label="Next observation image" disabled={index === observations.length - 1} onClick={() => selectOffset(1)}>→</Button>
      </nav>}
      {onSelectObservation && imageUrl && <details>
        <summary className="text-muted-foreground cursor-pointer">Thumbnails</summary>
        <div className="flex gap-1 overflow-x-auto py-2">
          {observations.map((item, i) => <button key={item.id}
            aria-label={`Observation ${i + 1}`} aria-current={item.id === observation.id ? "true" : undefined}
            className={`w-20 shrink-0 rounded border p-1 ${item.id === observation.id ? "border-primary" : "border-transparent"}`}
            onClick={() => onSelectObservation(item.id)}>
            <img src={imageUrl(item.image)} alt="" loading="lazy" className="h-12 w-full object-contain" />
            <span>{i + 1}</span>
          </button>)}
        </div>
      </details>}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-medium">
          {action
            ? `Action ${action.id}`
            : index === 0
              ? "Initial observation"
              : "Observation"}
        </h3>
        <span className="text-muted-foreground">
          {observation.id} · {observation.time.toFixed(1)}s
        </span>
      </div>
      {action && (
        <p className="text-muted-foreground text-sm">
          {action.label} · {action.observation_id} → {observation.id}
        </p>
      )}
      {text ? (
        <>
          <pre className="bg-muted/50 max-h-24 overflow-auto rounded-md border p-2 text-xs break-words whitespace-pre-wrap">
            <code>{text}</code>
          </pre>
          {typeof step === "number" && <a
            href={traceHref(step, observation.id)}
            onClick={onOpenTrace ? event => {
              if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
              event.preventDefault();
              onOpenTrace(step, observation.id);
            } : undefined}
            className="text-xs underline underline-offset-4"
          >
            View agent trace · step {step}
          </a>}
        </>
      ) : (
        <p className="text-muted-foreground text-sm">
          No agent command was recorded for this observation.
        </p>
      )}
    </section>
  );
}
