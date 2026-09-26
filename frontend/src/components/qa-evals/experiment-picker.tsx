"use client";

import { useRef } from "react";
import useSWR from "swr";
import { MemberFilterTypeahead } from "@/components/member-filter-typeahead";
import { Pane, PaneState } from "@/components/qa-evals/pane";
import { Input } from "@/components/ui/input";
import { fetcher } from "@/lib/api";
import { formatTaskAuthor } from "@/lib/format";
import {
  experimentFromInput,
  labelExperimentsUrl,
  type QAEvalsView,
} from "@/lib/qa-evals";
import type { DashboardResponse } from "@/lib/types";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import { cn, formatRelativeTime } from "@/lib/utils";

/** The experiments to label trials from: the dashboard's list for a member
 * (you by default) and a search. Enter opens the experiment a pasted link,
 * id or name names. */
export function ExperimentPicker({
  view,
  show,
}: {
  view: QAEvalsView;
  show: (patch: Partial<QAEvalsView>) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const search = useDebouncedValue(view.search, 300);
  const list = useSWR<DashboardResponse>(
    labelExperimentsUrl({ ...view, search }),
    fetcher,
    { keepPreviousData: true }
  );
  const experiments = list.data?.experiments ?? [];

  return (
    <Pane title="Experiments">
      <form
        className="space-y-2 border-b border-[color:var(--paper-line-2)] p-2"
        onSubmit={(event) => {
          event.preventDefault();
          const experiment = experimentFromInput(view.search ?? "");
          if (!experiment) return;
          // The text named the experiment; keys now go to its queue.
          show({ experiment, review: null, search: null });
          input.current?.blur();
        }}
      >
        <MemberFilterTypeahead
          selectedUserId={view.member === "all" ? null : view.member}
          onSelect={(member) => show({ member: member ?? "all" })}
        />
        <Input
          ref={input}
          aria-label="Search experiments"
          value={view.search ?? ""}
          onChange={(event) => show({ search: event.target.value || null })}
          placeholder="Search, or paste a link or id"
          className="h-8 text-sm"
        />
      </form>
      <PaneState
        error={list.error}
        loading={!list.data}
        empty={experiments.length === 0 && "No experiments."}
      >
        <ul className="min-h-0 flex-1 divide-y divide-[color:var(--paper-line-2)] overflow-y-auto">
          {experiments.map((experiment) => {
            const selected =
              view.experiment === experiment.id ||
              view.experiment === experiment.name;
            return (
              <li key={experiment.id}>
                <button
                  type="button"
                  aria-pressed={selected}
                  onClick={() =>
                    show({ experiment: experiment.id, review: null })
                  }
                  className={cn(
                    "block w-full px-3 py-2 text-left hover:bg-[color:var(--paper-line-2)]/60",
                    selected && "bg-[color:var(--paper-line-2)]"
                  )}
                >
                  <span className="block truncate text-xs font-medium">
                    {experiment.name}
                  </span>
                  <span className="block truncate text-[11px] text-[color:var(--paper-ink-3)]">
                    {[
                      formatTaskAuthor(
                        experiment.author ?? experiment.last_author
                      ),
                      experiment.last_created_at &&
                        formatRelativeTime(experiment.last_created_at),
                      `${experiment.reward_total - experiment.reward_success} failed`,
                      `${experiment.total_trials} ${experiment.total_trials === 1 ? "trial" : "trials"}`,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </PaneState>
    </Pane>
  );
}
