"use client";

import { useEffect, useEffectEvent, useState } from "react";
import type { RefObject } from "react";
import { useSWRConfig } from "swr";
import { fetcher } from "@/lib/api";
import {
  labelTrialRequest,
  trialGoldenUrl,
  type QAGolden,
  type QAGoldenRef,
  type QAQueue,
} from "@/lib/qa-evals";
import { EXECUTION_LABELS } from "@/lib/review";
import type { AnalysisClassification } from "@/lib/types";
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
