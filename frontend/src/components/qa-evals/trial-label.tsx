"use client";

import { useEffect, useEffectEvent, useRef, useState } from "react";
import type { RefObject } from "react";
import useSWR, { useSWRConfig } from "swr";
import { Card, CardContent } from "@/components/ui/card";
import { fetcher } from "@/lib/api";
import {
  labelTrialRequest,
  trialGoldenUrl,
  type QAGolden,
  type QAGoldenRef,
  type QAQueue,
} from "@/lib/qa-evals";
import { EXECUTION_LABELS } from "@/lib/review";
import type { AnalysisClassification, Trial } from "@/lib/types";
import { isEditableTarget } from "@/lib/editable-target";
import { cn } from "@/lib/utils";

const CLASSIFICATIONS = Object.keys(
  EXECUTION_LABELS
) as AnalysisClassification[];

/** The classification a number key picks: 1–5, the buttons' order. */
export function classificationForKey(
  key: string
): AnalysisClassification | undefined {
  return CLASSIFICATIONS[Number(key) - 1];
}

/** Hands `onKey` each key press meant for the labeler in `container`: not
 * typed into a field, not a shortcut, not inside a dialog opened over it.
 * `onKey` returns whether it used the key. */
export function useLabelKeys(
  container: RefObject<HTMLElement | null>,
  onKey: (event: KeyboardEvent) => boolean
) {
  const handle = useEffectEvent(onKey);
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (isEditableTarget(event.target) || event.metaKey || event.ctrlKey)
        return;
      const dialog =
        event.target instanceof Element &&
        event.target.closest('[role="dialog"],[role="alertdialog"]');
      if (dialog && !dialog.contains(container.current)) return;
      if (handle(event)) event.preventDefault();
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [container]);
}

/** Saves labels, updates cached labels, and refreshes affected scores. */
export function useSaveLabel() {
  const { mutate } = useSWRConfig();
  const [saving, setSaving] = useState<ReadonlySet<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const mark = (trialId: string, on: boolean) =>
    setSaving((ids) => {
      const next = new Set(ids);
      if (on) next.add(trialId);
      else next.delete(trialId);
      return next;
    });
  const save = async (
    trialId: string,
    body: Parameters<typeof labelTrialRequest>[1]
  ) => {
    mark(trialId, true);
    setError(null);
    try {
      const golden = await fetcher<QAGolden>(
        ...labelTrialRequest(trialId, body)
      );
      await mutate(trialGoldenUrl(trialId), [golden], { revalidate: false });
      // Keep queue positions while labeling; a fresh read regroups them.
      await mutate<QAQueue>(
        (key) =>
          typeof key === "string" && key.startsWith("/api/qa-goldens/trials?"),
        (data) =>
          data && {
            ...data,
            trials: data.trials.map((trial) =>
              trial.trial_id === trialId ? { ...trial, golden } : trial
            ),
          },
        { revalidate: false }
      );
      void mutate(
        (key) =>
          typeof key === "string" &&
          (key.startsWith("/api/qa-evals/status?") ||
            key.startsWith("/api/qa-evals/runs/"))
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the label");
    } finally {
      mark(trialId, false);
    }
  };
  return { saving, error, save };
}

/** The five classifications as buttons. The golden's label is filled while
 * it counts, dashed while it waits for review or its task changed; QA's
 * answer is tagged. `numbered` shows the 1–5 keys. */
export function LabelButtons({
  golden,
  qaSaid,
  disabled,
  numbered = false,
  onPick,
}: {
  golden: QAGoldenRef | null | undefined;
  qaSaid: AnalysisClassification | null | undefined;
  disabled: boolean;
  numbered?: boolean;
  onPick: (classification: AnalysisClassification) => void;
}) {
  const saved = golden?.expected.classification;
  return (
    <div className="flex flex-wrap gap-1.5">
      {CLASSIFICATIONS.map((classification, index) => {
        const mine = saved === classification;
        return (
          <button
            key={classification}
            type="button"
            disabled={disabled}
            aria-pressed={mine}
            onClick={() => onPick(classification)}
            title={
              mine && golden?.state === "stale"
                ? "Task changed since labeling: click to confirm"
                : mine && golden?.state === "waiting"
                  ? "Imported, waiting for review: click to confirm"
                  : numbered
                    ? `Key ${index + 1}`
                    : undefined
            }
            className={cn(
              "rounded border px-2 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-50",
              !mine
                ? "hover:bg-muted"
                : golden?.state === "counted"
                  ? "border-foreground bg-foreground text-background"
                  : "border-foreground border-dashed"
            )}
          >
            {numbered && (
              <span className="text-muted-foreground mr-1 font-mono">
                {index + 1}
              </span>
            )}
            {EXECUTION_LABELS[classification]}
            {qaSaid === classification && (
              <span className="ml-1 rounded bg-blue-500/15 px-1 text-[10px] text-blue-600">
                QA
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/** The trial drawer's card for labeling a finished agent trial with the
 * classification QA should give it: the golden replays are scored against.
 * Keys 1–5 and clicks save the label, with the note. */
export function TrialLabel({ trial }: { trial: Trial }) {
  const card = useRef<HTMLDivElement>(null);
  const note = useRef<HTMLInputElement>(null);
  const { saving, error, save } = useSaveLabel();
  const goldens = useSWR<QAGolden[]>(trialGoldenUrl(trial.id), fetcher, {
    revalidateOnFocus: true,
  });
  const golden = goldens.data?.[0];
  const busy = !goldens.data || saving.size > 0;

  const label = (classification: AnalysisClassification) => {
    if (busy) return;
    void save(trial.id, {
      classification,
      note: note.current?.value ?? "",
    });
  };

  useLabelKeys(card, (event) => {
    const classification = classificationForKey(event.key);
    if (!classification || busy) return false;
    void label(classification);
    return true;
  });

  const saved = golden?.expected.classification;
  return (
    <Card ref={card}>
      <CardContent className="space-y-2 px-4 py-3 text-xs">
        <span className="font-semibold">Correct label</span>
        <LabelButtons
          golden={golden}
          qaSaid={trial.analysis?.classification}
          disabled={busy}
          numbered
          onPick={(classification) => void label(classification)}
        />
        <input
          key={golden?.id}
          ref={note}
          aria-label="Label note"
          defaultValue={golden?.note}
          onKeyDown={(event) => {
            if (event.key === "Enter" && saved) void label(saved);
          }}
          placeholder="Note: why this label is right (Enter saves)"
          className="bg-background h-7 w-full rounded border px-2"
        />
        {(error || goldens.error) && (
          <p className="text-red-500">{error ?? goldens.error.message}</p>
        )}
      </CardContent>
    </Card>
  );
}
