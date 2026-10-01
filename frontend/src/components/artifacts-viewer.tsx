"use client";

import { useCallback, useEffect, useEffectEvent, useMemo, useRef, useState, type ReactNode } from "react";
import useSWRInfinite from "swr/infinite";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Code, Eye, Loader2, Package, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { FileTreePane } from "@/components/file-tree-pane";
import { ArtifactFrameCommand } from "@/components/artifact-frame-command";
import {
  FileRenderer,
  isBinaryRendererFile,
} from "@/components/renderers/file-renderer";
import { fetcher } from "@/lib/api";
import { firstFilePath } from "@/lib/file-tree-order";
import { formatFileSize } from "@/lib/format";
import { sameFilePath } from "@/lib/file-path";
import { recordClientError } from "@/lib/observability";
import type { LineRange } from "@/lib/line-range";
import { runFileUrl } from "@/lib/run-playback";
import type { RunPlayback } from "@/lib/use-run-playback";

// Preview truncation threshold; matches TaskFilesPanel.
const TRUNCATE_THRESHOLD = 100 * 1024;

interface ArtifactFile {
  path: string;
  key?: string;
  size?: number;
  url?: string;
}

interface ArtifactsListing {
  cursor?: string | null;
  files?: ArtifactFile[];
}

// Harbor writes artifacts inside the per-trial subdirectory of the job dir,
// so the real S3 layout served by /trials/{id}/files is:
//   <trial_name>/artifacts/...                     (single-step)
//   <trial_name>/steps/<step_name>/artifacts/...   (multi-step)
// Treat any file with an `artifacts` segment anywhere in its path as an
// artifact, not just paths that literally begin with "artifacts/".
function isArtifactPath(path: string): boolean {
  return path.split("/").includes("artifacts");
}

interface ArtifactsViewerProps {
  filesUrl: string;
  /** Show only artifact files; the selection survives when still eligible. */
  artifactsOnly: boolean;
  onArtifactsOnlyChange?: (value: boolean) => void;
  trialId?: string;
  successfulAnalysisTrial?: boolean;
  /**
   * File addressed by the URL (``?file=``). A full storage path is read
   * directly; an older shortened link resolves to the one full path that
   * ends with it, across every listing page.
   */
  filePath?: string | null;
  /** Line range to highlight in the selected file (``?lines=``). */
  selectedLines?: LineRange | null;
  onSelectLinesChange?: (range: LineRange | null) => void;
  /** Reports the selected full storage path, or null when none is selected. */
  onSelectedFileChange?: (fullPath: string | null, options?: { preserveLines: boolean }) => void;
  /** The trial's playback, for observation captions and navigation. */
  playback?: RunPlayback;
  onOpenScene?: () => void;
  traceHref?: (step: number, observationId: string) => string;
  onOpenTrace?: (step: number, observationId: string) => void;
}

export function ArtifactsViewer({
  filesUrl,
  artifactsOnly,
  onArtifactsOnlyChange,
  trialId,
  successfulAnalysisTrial = false,
  filePath,
  selectedLines,
  onSelectLinesChange,
  onSelectedFileChange,
  playback,
  onOpenScene,
  traceHref,
  onOpenTrace,
}: ArtifactsViewerProps) {
  const { data: pages, isLoading, isValidating, error, mutate, size, setSize } =
    useSWRInfinite<ArtifactsListing>(
      (index, previous: ArtifactsListing | null) => {
        if (index > 0 && !previous?.cursor) return null;
        return `${filesUrl}?recursive=1${previous?.cursor ? `&cursor=${encodeURIComponent(previous.cursor)}` : ""}`;
      },
      fetcher,
      { revalidateOnFocus: false, revalidateFirstPage: false }
    );
  const files = useMemo(() => pages?.flatMap(page => page.files ?? []) ?? [], [pages]);
  const hasMore = !!pages?.at(-1)?.cursor;
  const errorStatus = (error as { status?: number } | undefined)?.status;
  const reportedIntegrityFailureRef = useRef<{
    trialId: string | undefined;
    filesUrl: string;
  } | null>(null);
  useEffect(() => {
    const reportedFailure = reportedIntegrityFailureRef.current;
    if (
      errorStatus !== 404 ||
      !successfulAnalysisTrial ||
      (reportedFailure !== null &&
        reportedFailure.trialId === trialId &&
        reportedFailure.filesUrl === filesUrl)
    ) {
      return;
    }
    reportedIntegrityFailureRef.current = { trialId, filesUrl };
    recordClientError("artifact_integrity_failure", {
      trial_id: trialId ?? "unknown",
      files_url: filesUrl,
      http_status: 404,
    });
  }, [errorStatus, filesUrl, successfulAnalysisTrial, trialId]);

  // Full storage paths are the tree keys; the tree shortens only labels.
  const entries = useMemo(
    () => new Map(files.map(file => [file.path, file])),
    [files]
  );
  // Identity must only change with the listing or filter — FileTreePane
  // rebuilds (and re-expands) the tree on any new array.
  const treePaths = useMemo(
    () => [...entries.keys()].filter(path => !artifactsOnly || isArtifactPath(path)),
    [entries, artifactsOnly]
  );

  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [viewMode, setViewMode] = useState<"rendered" | "raw">("rendered");
  // The drawer owns selection, including an explicit null from Back/Forward.
  const selected = filePath ?? null;
  const initialized = useRef(!!filePath);
  const publishSelection = useEffectEvent((path: string, preserveLines = false) => {
    onSelectedFileChange?.(path, { preserveLines });
  });
  function select(path: string | null, preserveLines = false) {
    initialized.current = true;
    onSelectedFileChange?.(path, { preserveLines });
  }

  const knownPaths = playback?.data?.observationPaths;
  const listed = !!selected && (entries.has(selected) || knownPaths?.[selected] !== undefined);
  // Only asynchronous lookup status belongs here; every result names its path.
  const [probe, setProbe] = useState<{ path: string; exists: boolean } | null>(null);
  useEffect(() => {
    if (!selected || listed) return;
    const abort = new AbortController();
    void fetch(runFileUrl(filesUrl, selected), { signal: abort.signal, credentials: "include" })
      .then(response => {
        if (!abort.signal.aborted) setProbe({ path: selected, exists: response.ok });
        abort.abort();
      })
      .catch(() => {
        if (!abort.signal.aborted) setProbe({ path: selected, exists: false });
      });
    return () => abort.abort();
  }, [selected, listed, filesUrl]);

  const exact = listed || (probe?.path === selected && probe.exists);
  const probedMissing = probe?.path === selected && !probe.exists;
  // Exact stored paths win. Short links wait for all pages before deciding
  // whether they are unique, including the old step-name/artifact-name form.
  const matches = useMemo(() => {
    if (!selected || exact || !probedMissing || !pages || hasMore || error) return null;
    return [...entries.keys()].filter(path => {
      const step = path.match(/(?:^|\/)steps\/([^/]+)\/artifacts\/(.+)$/);
      return sameFilePath(path, selected) || (step !== null && `${step[1]}/${step[2]}` === selected);
    });
  }, [selected, exact, probedMissing, pages, hasMore, error, entries]);
  const candidates = matches && matches.length > 1 ? matches : null;
  const resolving = !!selected && !exact && matches === null;

  useEffect(() => {
    if (!selected || exact || !probedMissing || !pages) return;
    if (hasMore) {
      if (!error && pages.length === size) void setSize(size + 1);
    } else if (matches?.length === 1) {
      publishSelection(matches[0], true);
    }
  }, [selected, exact, probedMissing, pages, hasMore, error, size, setSize, matches]);

  // Initialize once. Later clearing file= must leave the content area empty.
  useEffect(() => {
    if (selected) initialized.current = true;
    if (initialized.current || !treePaths.length) return;
    initialized.current = true;
    const first = firstFilePath(treePaths);
    if (first) publishSelection(first);
  }, [selected, treePaths]);

  const selectedFile = selected ? entries.get(selected) : undefined;

  // Captions for the agent's observation images, from the cached index.
  const source = playback?.data ?? null;
  const episode = source?.manifest.version === 2 ? source.manifest : null;
  const observationId = selected ? source?.observationPaths[selected] : undefined;
  const observation = observationId ? episode?.observations.find(o => o.id === observationId) : undefined;
  const frameCommand = episode && observation && selected && traceHref ? (
    <ArtifactFrameCommand
      episode={episode}
      observation={observation}
      command={source!.commands[observation.id] ?? null}
      traceHref={traceHref}
      onOpenTrace={onOpenTrace}
      imageUrl={image => runFileUrl(filesUrl, selected.slice(0, selected.lastIndexOf("/") + 1) + image)}
      onSelectObservation={id => {
        // Stay in the selected copy's directory (verifier or collected).
        const directory = selected.slice(0, selected.lastIndexOf("/") + 1);
        const paths = Object.entries(source!.observationPaths).filter(([, obs]) => obs === id).map(([path]) => path);
        select(paths.find(path => path.startsWith(directory) && !path.slice(directory.length).includes("/")) ?? paths[0] ?? null);
      }}
    />
  ) : null;

  if (isLoading && !selected) {
    return (
      <div className="space-y-2 p-4">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-3/4" />
      </div>
    );
  }

  if (error && !files.length && !selected) {
    if (errorStatus === 404) {
      return (
        <div className="p-6 text-center">
          <Package className="text-muted-foreground/50 mx-auto mb-2 h-8 w-8" />
          <p className="text-muted-foreground text-sm">
            Artifacts are unavailable for this attempt.
          </p>
          {successfulAnalysisTrial && (
            <p className="mt-1 text-xs text-red-500">
              This successful analysis run promised durable artifacts; an
              integrity alert was recorded.
            </p>
          )}
        </div>
      );
    }
    if (errorStatus === 403) {
      return (
        <div className="p-6 text-center">
          <p className="text-sm text-red-500">
            You are not authorized to view artifacts for this trial.
          </p>
        </div>
      );
    }
    return (
      <div className="p-6 text-center">
        <p className="text-muted-foreground text-sm">
          Could not load artifacts.
        </p>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          className="mt-3 h-7"
          onClick={() => void mutate()}
          disabled={isValidating}
        >
          {isValidating ? (
            <>
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              Retrying…
            </>
          ) : (
            "Retry"
          )}
        </Button>
      </div>
    );
  }

  const fileCountLabel = `${treePaths.length} ${
    treePaths.length === 1 ? "file" : "files"
  }`;

  return (
    <div className="@container/file-browser flex h-full min-h-0 min-w-0 flex-col overflow-hidden">
      <div className="border-border flex shrink-0 items-center gap-2 border-b px-2 py-1">
        <Button variant="ghost" size="sm" onClick={() => setSidebarOpen(!sidebarOpen)} aria-label={sidebarOpen ? "Hide file list" : "Show file list"} aria-expanded={sidebarOpen}>
          {sidebarOpen ? <PanelLeftClose className="size-4" /> : <PanelLeftOpen className="size-4" />}
        </Button>
        {onArtifactsOnlyChange ? <label className="flex items-center gap-2 text-xs">
          Show
          <select aria-label="File filter" value={artifactsOnly ? "artifacts" : "all"}
            onChange={event => {
              const only = event.target.value === "artifacts";
              if (only && selected && !isArtifactPath(selected)) {
                select(null);
                onSelectLinesChange?.(null);
              }
              onArtifactsOnlyChange(only);
            }}
            className="bg-background rounded border px-2 py-1">
            <option value="all">All files</option><option value="artifacts">Artifacts only</option>
          </select>
        </label> : <span className="text-xs">Artifacts</span>}
        <span className="text-muted-foreground ml-auto text-xs">{fileCountLabel}{hasMore ? " loaded" : ""}</span>
        {selected && source && selected === source.recordingPath && onOpenScene && (
          <Button size="sm" variant="outline" onClick={onOpenScene}>Scene and observations</Button>
        )}
      </div>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden @2xl/file-browser:flex-row">
        {/* Stacked layout needs a definite height: FileTreePane virtualizes
            against this viewport. Once the viewer's own container is wide
            enough for a row, the tree stretches to the full pane height. */}
        {sidebarOpen && <div className="border-border bg-muted/30 flex h-[20vh] w-full flex-col overflow-hidden border-b p-2 @2xl/file-browser:h-auto @2xl/file-browser:w-56 @2xl/file-browser:shrink-0 @2xl/file-browser:border-r @2xl/file-browser:border-b-0 @3xl/file-browser:w-64">
          <FileTreePane
            className="min-h-0 flex-1"
            onSelectPath={path => select(path)}
            paths={treePaths}
            selectedPath={selected}
          />
          {hasMore && <Button size="sm" variant="ghost" disabled={isValidating}
            onClick={() => { if (error) void mutate(); else void setSize(size + 1); }}>
            {isValidating ? "Loading files…" : error ? "Retry loading files" : "Load more files"}
          </Button>}
          {error && <p role="alert" className="px-2 text-xs text-destructive">Could not load more files.</p>}
        </div>}
        {candidates ? (
          <div className="min-w-0 flex-1 space-y-2 p-4 text-sm">
            <p>Several files match this link. Choose one:</p>
            <ul className="space-y-1">
              {candidates.map(path => <li key={path}>
                <button type="button" className="font-mono text-xs underline underline-offset-4" onClick={() => select(path, true)}>{path}</button>
              </li>)}
            </ul>
          </div>
        ) : selected ? (
          <ArtifactContentPane
            filesUrl={filesUrl}
            key={selected}
            fullPath={selected}
            presignedUrl={selectedFile?.url}
            fileSize={selectedFile?.size}
            resolving={resolving}
            viewMode={viewMode}
            onViewModeChange={setViewMode}
            selectedLines={selectedLines}
            onSelectLinesChange={onSelectLinesChange}
            frameCommand={frameCommand}
          />
        ) : pages && treePaths.length === 0 && !hasMore ? (
          <div className="min-w-0 flex-1 p-6 text-center">
            <Package className="text-muted-foreground/50 mx-auto mb-2 h-8 w-8" />
            <p className="text-muted-foreground text-sm">{artifactsOnly ? "No artifacts" : "No files"}</p>
          </div>
        ) : (
          <p className="text-muted-foreground min-w-0 flex-1 p-6 text-sm">Select a file</p>
        )}
      </div>
    </div>
  );
}

interface ArtifactContentPaneProps {
  filesUrl: string;
  fullPath: string;
  presignedUrl?: string;
  fileSize?: number;
  /** A URL path still being matched against listings; errors wait for it. */
  resolving?: boolean;
  viewMode: "rendered" | "raw";
  onViewModeChange: (mode: "rendered" | "raw") => void;
  selectedLines?: LineRange | null;
  onSelectLinesChange?: (range: LineRange | null) => void;
  frameCommand?: ReactNode;
}

function ArtifactContentPane({
  filesUrl,
  fullPath,
  presignedUrl,
  fileSize,
  resolving = false,
  viewMode,
  onViewModeChange,
  selectedLines,
  onSelectLinesChange,
  frameCommand,
}: ArtifactContentPaneProps) {
  const contentRef = useRef<HTMLDivElement>(null);
  const [content, setContent] = useState<string | null>(null);
  const [contentLoading, setContentLoading] = useState(false);
  const [contentError, setContentError] = useState<string | null>(null);
  const [isTruncated, setIsTruncated] = useState(false);
  const [loadingFullFile, setLoadingFullFile] = useState(false);

  const fileName = fullPath.split("/").pop() ?? "";
  const isBinary = fileName ? isBinaryRendererFile(fileName) : false;
  const isImage = /\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(fileName);

  // Each path segment is URL-encoded individually so `/` separators in the
  // path stay intact for the backend file route (encodeURIComponent would
  // turn them into %2F and miss the route).
  const proxyUrl = useMemo(() => {
    if (!fullPath) return null;
    const encoded = fullPath.split("/").map(encodeURIComponent).join("/");
    return `${filesUrl}/${encoded}`;
  }, [filesUrl, fullPath]);

  useEffect(() => {
    if (contentRef.current) contentRef.current.scrollTop = 0;
  }, [fullPath]);

  useEffect(() => {
    if (!proxyUrl) {
      setContent(null);
      setContentLoading(false);
      setContentError(null);
      setIsTruncated(false);
      return;
    }
    if (isBinary) {
      setContent(null);
      setContentLoading(false);
      setContentError(null);
      setIsTruncated(false);
      return;
    }

    const shouldTruncate =
      typeof fileSize === "number" && fileSize > TRUNCATE_THRESHOLD;
    let cancelled = false;
    setContentLoading(true);
    setContentError(null);

    async function fetchText() {
      try {
        let text: string | null = null;
        let truncated = false;

        if (presignedUrl) {
          try {
            const headers: HeadersInit = shouldTruncate
              ? { Range: `bytes=0-${TRUNCATE_THRESHOLD - 1}` }
              : {};
            const res = await fetch(presignedUrl, { headers });
            if (res.ok || res.status === 206) {
              text = await res.text();
              truncated =
                res.status === 206 ||
                (!!shouldTruncate && text.length >= TRUNCATE_THRESHOLD);
            }
          } catch {
            // fall through to proxy
          }
        }

        if (text === null) {
          const res = await fetch(proxyUrl!);
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          text = await res.text();
          truncated = !!shouldTruncate && text.length >= TRUNCATE_THRESHOLD;
        }

        if (!cancelled) {
          setContent(text ?? "");
          setIsTruncated(truncated);
        }
      } catch (err) {
        if (!cancelled) {
          setContentError(
            err instanceof Error ? err.message : "Failed to load file"
          );
          setContent("");
          setIsTruncated(false);
        }
      } finally {
        if (!cancelled) setContentLoading(false);
      }
    }

    void fetchText();
    return () => {
      cancelled = true;
    };
  }, [fullPath, proxyUrl, presignedUrl, isBinary, fileSize]);

  const loadFullFile = useCallback(async () => {
    if (!proxyUrl) return;
    setLoadingFullFile(true);
    try {
      if (presignedUrl) {
        try {
          const res = await fetch(presignedUrl);
          if (res.ok) {
            const text = await res.text();
            setContent(text);
            setIsTruncated(false);
            return;
          }
        } catch {
          // fall through to proxy
        }
      }
      const res = await fetch(proxyUrl);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const text = await res.text();
      setContent(text);
      setIsTruncated(false);
    } catch (err) {
      setContentError(
        err instanceof Error ? err.message : "Failed to load full file"
      );
    } finally {
      setLoadingFullFile(false);
    }
  }, [proxyUrl, presignedUrl]);

  const renderUrl = presignedUrl || proxyUrl || null;

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      <div className="border-border bg-muted/30 flex items-center justify-between gap-2 border-b px-3 py-2 sm:px-4">
        <div className="text-muted-foreground min-w-0 flex-1 truncate font-mono text-[10px] sm:text-xs">
          {fullPath}
          {typeof fileSize === "number" && (
            <span className="text-muted-foreground/70 ml-2">
              ({formatFileSize(fileSize)})
            </span>
          )}
        </div>
        {renderUrl && <a href={renderUrl} download={fileName} className="shrink-0 text-xs underline underline-offset-4">Download file</a>}
        {!isBinary && (
          <Tabs
            value={viewMode}
            onValueChange={(v) => onViewModeChange(v as "rendered" | "raw")}
          >
            <TabsList className="h-7">
              <TabsTrigger value="rendered" className="h-6 px-2 text-[10px]">
                <Eye className="mr-1 h-3 w-3" />
                Rendered
              </TabsTrigger>
              <TabsTrigger value="raw" className="h-6 px-2 text-[10px]">
                <Code className="mr-1 h-3 w-3" />
                Raw
              </TabsTrigger>
            </TabsList>
          </Tabs>
        )}
      </div>
      <div ref={contentRef} className={`bg-card min-h-0 flex-1 ${isImage ? "flex flex-col overflow-hidden" : "overflow-auto"}`}>

        {contentLoading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-5/6" />
          </div>
        ) : contentError && !isBinary ? (
          resolving ? (
            <p role="status" className="text-muted-foreground p-4 text-sm">Locating {fileName}…</p>
          ) : (
            <div className="text-destructive p-4 text-sm">
              Failed to load {fileName}: {contentError}
            </div>
          )
        ) : (
          <>
            <div className={isImage ? "min-h-0 flex-1" : "h-full"}>
            <FileRenderer
              fileName={fileName}
              fitImage={isImage}
              url={renderUrl}
              content={isBinary ? null : content}
              fileSize={fileSize}
              viewMode={viewMode}
              selectedLines={selectedLines}
              onSelectLines={onSelectLinesChange}
            />
            </div>
            {frameCommand && <div className="max-h-[42%] shrink-0 overflow-auto">{frameCommand}</div>}
          </>
        )}
      </div>
      {!isBinary && isTruncated && (
        <div className="border-border bg-muted/50 flex items-center justify-between border-t px-4 py-3">
          <span className="text-muted-foreground text-xs">
            Showing first {formatFileSize(TRUNCATE_THRESHOLD)} of{" "}
            {fileSize ? formatFileSize(fileSize) : "large file"}
          </span>
          <Button
            type="button"
            size="sm"
            onClick={loadFullFile}
            disabled={loadingFullFile}
            className="h-auto px-3 py-1.5 text-xs"
          >
            {loadingFullFile ? (
              <>
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                Loading...
              </>
            ) : (
              "Load full file"
            )}
          </Button>
        </div>
      )}
    </div>
  );
}
