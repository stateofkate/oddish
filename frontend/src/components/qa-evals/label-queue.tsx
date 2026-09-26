"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { GroupChips } from "@/components/qa-evals/group-chips";
import { Pane, PaneState } from "@/components/qa-evals/pane";
import {
  LabelButtons,
  classificationForKey,
  useLabelKeys,
  useSaveLabel,
} from "@/components/qa-evals/trial-label";
import { TrialLink } from "@/components/qa-evals/trial-link";
import { fetcher } from "@/lib/api";
import {
  QA_QUEUE_GROUPS,
  queueUrl,
  type QAEvalsView,
  type QAQueue,
  type QAQueueTrial,
} from "@/lib/qa-evals";
import type { AnalysisClassification } from "@/lib/types";
import { cn } from "@/lib/utils";

function outcome(reward: number | null): string {
  if (reward === null) return "no reward";
  if (reward === 1) return "passed";
  return reward === 0 ? "failed" : `reward ${reward}`;
}

/** The queue's trials in one group at a time (failed by default), each with
 * production QA's reason; its answer is tagged on the label buttons. Keys:
 * ↑/↓ or j/k move, 1–5 label, Enter takes QA's answer, and a label moves
 * on to the next trial. */
export function LabelQueue({
  view,
  show,
}: {
  view: QAEvalsView;
  show: (patch: Partial<QAEvalsView>) => void;
}) {
  const list = useRef<HTMLUListElement>(null);
  const { saving, error, save } = useSaveLabel();
  const url = queueUrl(view);
  const queue = useSWR<QAQueue>(url, fetcher, { revalidateOnFocus: true });
  const [cursor, setCursor] = useState<string | null>(null);

  const all = queue.data?.trials ?? [];
  const shown =
    view.queue === "all" ? all : all.filter((t) => t.group === view.queue);
  const current = shown.find((t) => t.trial_id === cursor) ?? shown[0];

  useEffect(() => {
    list.current
      ?.querySelector("[aria-current]")
      ?.scrollIntoView({ block: "nearest" });
  }, [current?.trial_id]);

  const step = (from: QAQueueTrial, by: number) => {
    const index = shown.indexOf(from) + by;
    const next = shown[Math.min(Math.max(index, 0), shown.length - 1)];
    if (next) setCursor(next.trial_id);
  };

  const label = (
    trial: QAQueueTrial,
    classification: AnalysisClassification
  ) => {
    if (saving.has(trial.trial_id)) return;
    step(trial, 1);
    void save(trial.trial_id, { classification });
  };

  // Enter belongs to a focused control only when the user reached it with
  // Tab; a clicked control, or one focused as a menu closed, was not chosen
  // for Enter. (`:focus-visible` cannot tell: Chrome sets it on any control
  // as soon as a key is pressed.)
  const tabbed = useRef(false);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Tab") tabbed.current = true;
    };
    const onPointerDown = () => {
      tabbed.current = false;
    };
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("pointerdown", onPointerDown, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("pointerdown", onPointerDown, true);
    };
  }, []);

  useLabelKeys(list, (event) => {
    if (!current) return false;
    if (event.key === "ArrowDown" || event.key === "j") {
      step(current, 1);
      return true;
    }
    if (event.key === "ArrowUp" || event.key === "k") {
      step(current, -1);
      return true;
    }
    const onControl =
      tabbed.current &&
      event.target instanceof Element &&
      event.target.closest("button,a");
    const classification =
      event.key === "Enter"
        ? onControl
          ? null
          : current.qa_classification
        : classificationForKey(event.key);
    if (!classification) return false;
    // A held key labels one trial, not each one the cursor moves on to.
    if (!event.repeat) void label(current, classification);
    return true;
  });

  return (
    <Pane title={queue.data?.title ?? "Trials"}>
      <PaneState
        error={queue.error}
        loading={Boolean(url) && !queue.data}
        empty={!url && "Pick an experiment."}
      >
        <div className="flex flex-wrap items-center gap-1.5 px-3 py-2">
          <GroupChips
            groups={QA_QUEUE_GROUPS.map((group) => ({
              value: group,
              label: `${group} ${all.filter((t) => t.group === group).length}`,
            }))}
            total={all.length}
            value={view.queue}
            onChange={(group) => {
              show({ queue: group });
              // Regroups the trials labeled since the last fetch.
              void queue.mutate();
            }}
          />
        </div>
        {error && <p className="px-3 pb-2 text-xs text-red-500">{error}</p>}
        <ul
          ref={list}
          className="min-h-0 flex-1 divide-y divide-[color:var(--paper-line-2)] overflow-y-auto border-t border-[color:var(--paper-line-2)]"
        >
          {shown.length === 0 && (
            <li className="p-3 text-sm text-[color:var(--paper-ink-3)]">
              No trials.
            </li>
          )}
          {shown.map((trial) => (
            <li
              key={trial.trial_id}
              aria-current={trial === current || undefined}
              onClick={(event) => {
                if (!(event.target as Element).closest("button,a")) {
                  setCursor(trial.trial_id);
                }
              }}
              className={cn(
                "space-y-1.5 border-l-2 border-transparent px-3 py-2",
                trial === current &&
                  "border-[color:var(--paper-ink)] bg-[color:var(--paper-line-2)]/40"
              )}
            >
              <div className="flex items-baseline gap-2 text-xs">
                <TrialLink
                  taskId={trial.task_id}
                  trialId={trial.trial_id}
                  className="min-w-0 truncate font-medium hover:underline"
                  title="Open the trial in a new tab"
                >
                  {trial.task_name}
                </TrialLink>
                <span
                  className={
                    trial.reward === 1 ? "text-emerald-600" : "text-red-600"
                  }
                >
                  {outcome(trial.reward)}
                </span>
                <span className="ml-auto shrink-0 font-mono text-[11px] text-[color:var(--paper-ink-3)]">
                  {trial.trial_id}
                </span>
              </div>
              {(trial.qa_subtype || trial.qa_reason) && (
                <p
                  className="line-clamp-2 text-[11px] text-[color:var(--paper-ink-2)]"
                  title={trial.qa_reason ?? undefined}
                >
                  {trial.qa_subtype && (
                    <span className="font-medium">{trial.qa_subtype}: </span>
                  )}
                  {trial.qa_reason}
                </p>
              )}
              <LabelButtons
                golden={trial.golden}
                qaSaid={trial.qa_classification}
                disabled={saving.has(trial.trial_id)}
                numbered={trial === current}
                onPick={(classification) => void label(trial, classification)}
              />
            </li>
          ))}
        </ul>
      </PaneState>
    </Pane>
  );
}
