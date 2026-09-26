"use client";

import { useMemo } from "react";
import { useIsDark } from "./use-is-dark";
import { PIERRE_THEME, PIERRE_UNSAFE_CSS } from "./pierre-options";

/** @pierre/diffs options every diff view shares: unified hunks, word-level
 * changes, and the app's theme in light or dark. */
export function useDiffOptions() {
  const isDark = useIsDark();
  return useMemo(
    () => ({
      theme: PIERRE_THEME,
      themeType: (isDark ? "dark" : "light") as "dark" | "light",
      diffStyle: "unified" as const,
      diffIndicators: "bars" as const,
      lineDiffType: "word" as const,
      overflow: "scroll" as const,
      unsafeCSS: PIERRE_UNSAFE_CSS,
    }),
    [isDark]
  );
}
