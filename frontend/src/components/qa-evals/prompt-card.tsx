"use client";

import { useMemo } from "react";
import useSWR from "swr";
import { parseDiffFromFile } from "@pierre/diffs";
import { FileDiff } from "@pierre/diffs/react";
import { Pane, PaneState } from "@/components/qa-evals/pane";
import { VersionPicker } from "@/components/qa-evals/version-picker";
import { useDiffOptions } from "@/components/renderers/use-diff-options";
import { formatDate } from "@/lib/format";
import { fetcher } from "@/lib/api";
import {
  shortSha,
  versionTextUrl,
  type QAPromptVersion,
  type QAStage,
} from "@/lib/qa-evals";

const GITHUB_COMMIT = "https://github.com/abundant-ai/oddish/commit/";

/** The shown prompt version against its base: line and word diff with the
 * added and removed line counts, and a picker for each. */
export function PromptCard({
  stage,
  versions,
  shown,
  base,
  onPick,
}: {
  stage: QAStage;
  versions: QAPromptVersion[];
  shown: QAPromptVersion | undefined;
  base: QAPromptVersion | undefined;
  onPick: (pick: { version?: string; base_version?: string }) => void;
}) {
  const textOf = (version: QAPromptVersion | undefined) =>
    version?.text_available ? versionTextUrl(stage, version.sha256) : null;
  const after = useSWR<{ text: string }>(textOf(shown), fetcher);
  const before = useSWR<{ text: string }>(textOf(base), fetcher);
  const afterText = after.data?.text;
  // With no base, the whole text shows as added.
  const beforeText = base ? before.data?.text : "";

  const shared = useDiffOptions();
  // Prompts are prose: wrap long lines; the pickers replace the file header.
  const options = useMemo(
    () => ({ ...shared, overflow: "wrap" as const, disableFileHeader: true }),
    [shared]
  );
  const diff = useMemo(
    () =>
      afterText === undefined ||
      beforeText === undefined ||
      afterText === beforeText
        ? null
        : parseDiffFromFile(
            { name: "prompt.txt", contents: beforeText },
            { name: "prompt.txt", contents: afterText }
          ),
    [beforeText, afterText]
  );
  const count = (key: "additionLines" | "deletionLines") =>
    diff?.hunks.reduce((sum, hunk) => sum + hunk[key], 0) ?? 0;

  return (
    <Pane
      title={
        <>
          Prompt change
          {shown && (
            <span className="font-normal text-[color:var(--paper-ink-3)]">
              {shown.name} · {shown.author ?? "unknown"} ·{" "}
              {formatDate(shown.first_seen_at)}
              {shown.git_commit && (
                <>
                  {" · "}
                  <a
                    className="font-mono hover:underline"
                    href={`${GITHUB_COMMIT}${shown.git_commit}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    commit {shortSha(shown.git_commit)}
                  </a>
                </>
              )}
            </span>
          )}
        </>
      }
    >
      <div className="min-h-0 flex-1 overflow-y-auto">
        <PaneState empty={!shown && "No prompt versions."}>
          <div className="sticky top-0 z-10 flex flex-wrap items-center gap-2 border-b border-[color:var(--paper-line-2)] bg-[color:var(--paper-surface)] px-4 py-2 text-xs">
            <span className="text-[color:var(--paper-ink-3)]">Base</span>
            <VersionPicker
              label="Base version"
              versions={versions}
              value={base}
              exclude={shown?.sha256}
              onChange={(sha) => onPick({ base_version: sha })}
            />
            <span aria-hidden>→</span>
            <VersionPicker
              label="Shown version"
              versions={versions}
              value={shown}
              onChange={(sha) => onPick({ version: sha })}
            />
            {diff && (
              <span className="ml-auto font-mono tabular-nums">
                <span className="text-red-600">−{count("deletionLines")}</span>{" "}
                <span className="text-emerald-600">
                  +{count("additionLines")}
                </span>
              </span>
            )}
          </div>
          <PaneState
            error={after.error ?? before.error}
            loading={
              shown?.text_available &&
              (afterText === undefined || beforeText === undefined)
            }
            empty={
              shown?.text_available
                ? !diff && "Same text."
                : "No recorded text."
            }
          >
            {diff && <FileDiff fileDiff={diff} options={options} />}
          </PaneState>
        </PaneState>
      </div>
    </Pane>
  );
}
