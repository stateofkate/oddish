"use client";

import { useState, useEffect, useMemo, useCallback } from "react";
import { Copy, Check } from "lucide-react";
import { Button } from "@/components/ui/button";

let shikiPromise: Promise<typeof import("shiki")> | null = null;
const HIGHLIGHT_MAX_CHARS = 20_000;
// A collapsed preview also stops at this many characters, so one enormous
// line cannot stretch it.
const COLLAPSED_MAX_CHARS = 4_000;

function getShiki() {
  if (!shikiPromise) {
    shikiPromise = import("shiki");
  }
  return shikiPromise;
}

interface CodeBlockProps {
  code: string;
  /** Exact text for Copy when `code` is a reformatted view of it. */
  copyText?: string;
  language?: string;
  className?: string;
  /** CSS max-height value. "none" disables the constraint (fills parent). Default: "16rem". */
  maxHeight?: string;
  /** Max character count before truncation. 0 disables truncation. Default: 50000. */
  truncateAt?: number;
  /** Show this many lines with a "Show all" toggle instead of a scrolling
   *  height cap; the full text is one click away. */
  collapseAfter?: number;
  showCopyButton?: boolean;
}

export function CodeBlock({
  code,
  copyText,
  language = "text",
  className,
  maxHeight = "16rem",
  truncateAt = 50000,
  collapseAfter,
  showCopyButton = true,
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [highlightedHtml, setHighlightedHtml] = useState<string | null>(null);

  // Collapsible blocks drop trailing blank lines from the view (Copy keeps them).
  const shown = collapseAfter ? code.trimEnd() : code;
  const preview = useMemo(
    () =>
      collapseAfter
        ? shown
            .slice(0, COLLAPSED_MAX_CHARS)
            .split("\n", collapseAfter)
            .join("\n")
        : shown,
    [shown, collapseAfter]
  );
  const collapsed = !expanded && preview.length < shown.length;
  const truncatedCode = useMemo(() => {
    if (collapseAfter) return collapsed ? preview : shown;
    if (truncateAt > 0 && code.length > truncateAt) {
      return code.slice(0, truncateAt) + "\n\n... (truncated)";
    }
    return code;
  }, [code, shown, truncateAt, collapseAfter, collapsed, preview]);
  const shouldHighlight = useMemo(
    () => language !== "text" && truncatedCode.length <= HIGHLIGHT_MAX_CHARS,
    [language, truncatedCode],
  );

  useEffect(() => {
    if (!shouldHighlight) {
      setHighlightedHtml(null);
      return;
    }

    let cancelled = false;

    async function highlight() {
      try {
        const shiki = await getShiki();
        const lang = language === "text" ? "text" : language;
        const html = await shiki.codeToHtml(truncatedCode, {
          lang,
          themes: {
            light: "github-light",
            dark: "github-dark-default",
          },
        });
        if (!cancelled) setHighlightedHtml(html);
      } catch {
        if (!cancelled) setHighlightedHtml(null);
      }
    }

    highlight();
    return () => {
      cancelled = true;
    };
  }, [truncatedCode, language, shouldHighlight]);

  const handleCopy = useCallback(async () => {
    await navigator.clipboard.writeText(copyText ?? code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [code, copyText]);

  const heightStyle = collapseAfter
    ? undefined
    : maxHeight === "none"
      ? { height: "100%" }
      : { maxHeight };
  const lineCount = collapseAfter ? shown.split("\n").length : 0;

  return (
    <div className={`group relative min-w-0 ${className || ""}`}>
      {showCopyButton && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={handleCopy}
          className="absolute right-1.5 top-1.5 z-10 h-6 w-6 rounded bg-muted/80 opacity-0 transition-opacity hover:bg-muted group-hover:opacity-100 focus-visible:opacity-100"
          title="Copy to clipboard"
          aria-label="Copy to clipboard"
        >
          {copied ? (
            <Check className="h-3 w-3 text-green-600 dark:text-green-400" />
          ) : (
            <Copy className="h-3 w-3 text-muted-foreground" />
          )}
        </Button>
      )}
      {highlightedHtml ? (
        <div
          className="overflow-x-auto overflow-y-auto rounded border border-border text-xs [&>pre]:m-0 [&>pre]:overflow-x-auto [&>pre]:whitespace-pre-wrap [&>pre]:wrap-break-word [&>pre]:p-3"
          style={heightStyle}
          dangerouslySetInnerHTML={{ __html: highlightedHtml }}
        />
      ) : (
        <pre
          className="overflow-x-auto overflow-y-auto whitespace-pre-wrap wrap-break-word rounded border border-border bg-muted/50 p-3 text-xs text-foreground"
          style={heightStyle}
        >
          {truncatedCode}
        </pre>
      )}
      {collapseAfter && preview.length < shown.length && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
          className="text-muted-foreground h-6 w-full rounded-t-none text-[11px]"
        >
          {expanded
            ? "Show less"
            : lineCount > collapseAfter
              ? `Show all ${lineCount.toLocaleString()} lines`
              : "Show all"}
        </Button>
      )}
    </div>
  );
}
