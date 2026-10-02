import { useMemo } from "react";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import {
  parsePlaybackIndex,
  playbackIndexError,
  runFileUrl,
  type PlaybackManifest,
  type RecordedCommand,
  type TrialPlaybackDescriptor,
} from "@/lib/run-playback";

/** Everything Run and Files need to present one trial's playback. */
export interface RunPlaybackSource {
  manifest: PlaybackManifest;
  /** Viewer identity within a trial attempt: the index revision. */
  revision: string;
  recordingPath: string;
  recordingUrl: string;
  outcomeUrl: string;
  imageUrl: (image: string) => string;
  /** Recorded command per observation ID; null when none was attributed. */
  commands: Record<string, RecordedCommand | null>;
  /** Full trial-relative image path to observation ID. */
  observationPaths: Record<string, string>;
}

export interface RunPlayback {
  preparation: "ready" | "absent" | "invalid" | "unchecked";
  /** Whether the trial has playback (ready or failed); undefined while unknown. */
  available: boolean | undefined;
  data: RunPlaybackSource | null;
  isLoading: boolean;
  error: Error | null;
  retry?: () => Promise<unknown>;
  isRetrying?: boolean;
}

/**
 * Read a trial's playback index. Run and Files share this one request; the
 * index already carries each observation's command, so no trace is fetched.
 * `filesUrl` is null until the descriptor is authoritative.
 */
export function useRunPlayback(
  filesUrl: string | null,
  descriptor: TrialPlaybackDescriptor | null | undefined
): RunPlayback {
  const ready =
    descriptor?.index_status === "ready" &&
    descriptor.index_path &&
    descriptor.index_revision
      ? { path: descriptor.index_path, revision: descriptor.index_revision }
      : null;
  const index = useSWR(
    filesUrl && ready
      ? [`${runFileUrl(filesUrl, ready.path)}?revision=${encodeURIComponent(ready.revision)}`, ready.revision]
      : null,
    async ([url, revision]: [string, string]) =>
      parsePlaybackIndex(await fetcher(url), revision),
    {
      revalidateOnFocus: false,
      revalidateIfStale: false,
      shouldRetryOnError: false,
    }
  );
  // Stable identity: viewers and image caches key their lifetime on it.
  const data = useMemo((): RunPlaybackSource | null => {
    if (!filesUrl || !index.data) return null;
    const { manifest, manifest_path, revision } = index.data;
    const file = (name: string) =>
      `${runFileUrl(filesUrl, manifest_path.replace(/playback\.json$/, name))}?revision=${encodeURIComponent(revision)}`;
    return {
      manifest,
      revision,
      recordingPath: manifest_path.replace(/playback\.json$/, manifest.recording),
      recordingUrl: file(manifest.recording),
      outcomeUrl: file(manifest.outcome),
      imageUrl: file,
      commands: index.data.commands,
      observationPaths: index.data.observation_paths,
    };
  }, [filesUrl, index.data]);

  if (!filesUrl)
    return { preparation: "unchecked", available: undefined, data: null, isLoading: false, error: null };
  if (descriptor?.index_status === "invalid")
    return {
      preparation: "invalid",
      available: true,
      data: null,
      isLoading: false,
      error: new Error(playbackIndexError(descriptor.index_error)),
    };
  if (!ready)
    return { preparation: descriptor?.index_status === "absent" ? "absent" : "unchecked", available: false, data: null, isLoading: false, error: null };
  return {
    preparation: "ready",
    available: true,
    data,
    isLoading: index.isLoading,
    error: index.error ?? null,
    retry: index.mutate,
    isRetrying: index.isValidating,
  };
}
