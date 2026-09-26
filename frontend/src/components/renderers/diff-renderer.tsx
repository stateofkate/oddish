"use client";

import { useMemo } from "react";
import { parsePatchFiles } from "@pierre/diffs";
import type { FileDiffMetadata } from "@pierre/diffs";
import { FileDiff } from "@pierre/diffs/react";
import { CodeRenderer } from "./code-renderer";
import { useDiffOptions } from "./use-diff-options";

interface DiffRendererProps {
  content: string;
  fileName: string;
}

/**
 * Renders `.diff` / `.patch` files with @pierre/diffs — parsed hunks,
 * word-level line diffs, one section per file for multi-file patches.
 * Content that doesn't parse as a unified diff falls back to the plain
 * code view.
 */
export function DiffRenderer({ content, fileName }: DiffRendererProps) {
  const options = useDiffOptions();

  // parsePatchFiles tolerates malformed sections (logs and skips them), and
  // returns one entry per file for multi-file git patches.
  const fileDiffs = useMemo<FileDiffMetadata[]>(() => {
    try {
      return parsePatchFiles(content).flatMap((patch) => patch.files);
    } catch {
      return [];
    }
  }, [content]);

  if (fileDiffs.length === 0) {
    return <CodeRenderer content={content} fileName={fileName} />;
  }

  return (
    <div className="h-full space-y-3 overflow-auto p-2">
      {fileDiffs.map((fileDiff, index) => (
        <FileDiff key={index} fileDiff={fileDiff} options={options} />
      ))}
    </div>
  );
}
