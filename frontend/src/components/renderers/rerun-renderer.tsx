"use client";

import { useEffect, useEffectEvent, useId, useRef, useState, type ReactNode } from "react";
import { WebViewer } from "@rerun-io/web-viewer";
import { Pause, Play, RotateCcw, Maximize2, ChevronLeft, ChevronRight } from "lucide-react";
import type { PlaybackManifest, PlaybackObservation } from "@/lib/run-playback";
import { PlaybackResources, type ObservationImage } from "./observation-images";

const noObservations: PlaybackObservation[] = [];

function siblingUrl(url: string, name: string) {
  return `${url.slice(0, url.lastIndexOf("/"))}/${encodeURIComponent(name)}`;
}

/**
 * One Rerun viewer. With a manifest it is an episode player whose time,
 * observation image and caption Oddish controls; without one it is the raw
 * `.rrd` viewer with Rerun's own controls. The viewer is created once per
 * recording and `resources`; seeking, a new `observationId`, or an equivalent
 * manifest never recreates it.
 */
export function RerunRenderer({
  url,
  fileName,
  playback,
  resources,
  active = true,
  observationId,
  renderObservation,
}: {
  url: string;
  fileName: string;
  playback?: PlaybackManifest;
  /** Downloads owned by the caller for this viewer's lifetime. */
  resources?: PlaybackResources;
  /** False while another drawer view covers the viewer; playback pauses. */
  active?: boolean;
  /** Observation to show; a change seeks the existing viewer. */
  observationId?: string | null;
  renderObservation?: (observation: PlaybackObservation) => ReactNode;
}) {
  const actionsId = useId();
  const container = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<WebViewer | null>(null);
  const imagesRef = useRef<PlaybackResources["images"]>(null);
  const seekRequest = useRef(0);
  const [observationImage, setObservationImage] = useState<ObservationImage | null>(null);
  const [buffering, setBuffering] = useState(false);
  const [imageError, setImageError] = useState(false);
  const [status, setStatus] = useState("Loading recording…");
  const [playbackStart, setPlaybackStart] = useState<number | null>(null);
  const playing = playbackStart !== null;
  const startTime = playback?.start_time ?? 0;
  const requestedTime = playback?.observations.find(o => o.id === observationId)?.time ?? startTime;
  const [time, setTime] = useState(requestedTime);
  const [diagnostics, setDiagnostics] = useState(false);
  const [sceneLayout, setSceneLayout] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [actionsOpen, setActionsOpen] = useState(false);
  // An explicitly selected result still belongs to its originating action,
  // even when that timestamp is also the next action's input observation.
  const [selectedActionId, setSelectedActionId] = useState<string | null>(null);
  const actions = playback?.actions ?? [];
  const activeAction = actions.find(action => action.id === selectedActionId)
    ?? actions.find(action => time >= action.time && time < action.end_time)
    ?? (time >= (actions.at(-1)?.end_time ?? Infinity) ? actions.at(-1) : undefined);
  const stops = playback?.observations.map(observation => observation.time) ?? [];
  const previous = stops.findLast(stop => stop < time - 0.001);
  const next = stops.find(stop => stop > time + 0.001);
  const timeline = playback?.timeline;
  const embeddedLayout = playback?.viewer_layout === "embedded_scene";
  const observations = playback?.observations ?? noObservations;
  const observation = observationImage && observations[observationImage.index];
  // Effect-owned viewer callbacks read only the latest committed props.
  const playbackValues = useEffectEvent(() => ({ observations, requestedTime, time }));

  useEffect(() => {
    const parent = container.current;
    if (!parent) return;
    // Every viewer owns its clock, decoded images and animation loop. A new
    // recording or resource owner starts paused with fresh state.
    const { observations, requestedTime: initialTime } = playbackValues();
    seekRequest.current++;
    setPlaybackStart(null);
    setSceneLayout(false);
    setObservationImage(null);
    setTime(initialTime);
    setStatus("Loading recording…");
    setBuffering(false);
    setImageError(false);
    setDiagnostics(false);
    setSelectedActionId(null);
    const viewer = new WebViewer();
    viewerRef.current = viewer;
    const abort = new AbortController();
    const firstIndex = Math.max(0, observations.findLastIndex(o => o.time <= initialTime + 0.000001));
    const owned = resources ? null : new PlaybackResources(
      url, observations.map(o => siblingUrl(url, o.image)), firstIndex
    );
    const loaded = (resources ?? owned)!;
    const images = loaded.images;
    imagesRef.current = images;
    const showScene = async () => {
      setSceneLayout(true);
      setStatus("");
      if (!images) return;
      const request = seekRequest.current;
      try {
        const first = await images.load(firstIndex);
        if (abort.signal.aborted || request !== seekRequest.current) return;
        images.pin(firstIndex);
        setObservationImage(first);
      } catch {
        if (!abort.signal.aborted && request === seekRequest.current) setImageError(true);
      }
    };
    const subscriptions = [
      viewer.on("recording_open", (event) => {
        if (abort.signal.aborted) return;
        if (timeline) {
          viewer.set_active_timeline(event.recording_id, timeline);
          viewer.set_current_time(event.recording_id, timeline, initialTime * 1e9);
          viewer.set_playing(event.recording_id, false);
        }
        // A recording with an embedded scene-only layout shows the agent's
        // images beside it; others keep their own layout and diagnostics.
        if (timeline && embeddedLayout) void showScene();
        else setStatus("");
      }),
      viewer.on("time_update", (event) => {
        // Manifest playback owns its clock. Rerun can report an older seek
        // after a newer frame has committed, so only raw recordings use it.
        if (abort.signal.aborted || timeline) return;
        const seconds = event.time / 1e9;
        setTime(seconds);
        const index = playbackValues().observations.findLastIndex(observation => observation.time <= seconds + 0.000001);
        const image = images?.get(index);
        if (images && image) {
          images.pin(index);
          setObservationImage(image);
        }
      }),
      viewer.on("play", (event) => {
        if (abort.signal.aborted) return;
        // Rerun has no playback-speed API. Manifest players advance its time
        // below and leave native playback paused, including canvas shortcuts.
        if (timeline) viewer.set_playing(event.recording_id, false);
        seekRequest.current++;
        setBuffering(false);
        setImageError(false);
        setSelectedActionId(null);
        const current = timeline ? playbackValues().time : 0;
        setPlaybackStart(previous => timeline && previous !== null ? null : current);
      }),
      viewer.on("pause", () => { if (!abort.signal.aborted && !timeline) setPlaybackStart(null); }),
    ];
    async function load() {
      try {
        const [data] = await Promise.all([
          loaded.recording,
          viewer.start(null, parent, {
            hide_welcome_screen: true,
            width: "100%",
            height: "100%",
            theme: timeline ? "dark" : undefined,
          }).then(() => { if (abort.signal.aborted) viewer.stop(); }),
        ]);
        if (abort.signal.aborted) {
          viewer.stop();
          return;
        }
        if (timeline) {
          for (const panel of [
            "top",
            "blueprint",
            "selection",
            "time",
          ] as const)
            viewer.override_panel_state(panel, "hidden");
        } else {
          // Task recordings can embed a hidden timeline. Files supplies no
          // Oddish player, so make native playback controls available here.
          viewer.override_panel_state("top", "expanded");
          viewer.override_panel_state("time", "expanded");
        }
        const channel = viewer.open_channel(fileName);
        channel.send_rrd(data);
        channel.close();
      } catch (error) {
        if (!abort.signal.aborted) {
          abort.abort();
          viewer.stop();
          setStatus(
            `Cannot preview ${fileName}: ${error instanceof Error ? error.message : "viewer failed"}`
          );
        }
      }
    }
    void load();
    return () => {
      abort.abort();
      owned?.dispose();
      imagesRef.current = null;
      subscriptions.forEach((unsubscribe) => unsubscribe());
      viewer.stop();
      viewerRef.current = null;
    };
  }, [url, fileName, timeline, embeddedLayout, resources]);

  useEffect(() => {
    if (observationImage) imagesRef.current?.prefetch(observationImage.index);
  }, [observationImage]);

  // A hidden viewer stops advancing; it resumes paused where it was.
  useEffect(() => {
    if (!active) setPlaybackStart(null);
  }, [active]);

  // Another observation was requested (e.g. browser Back to a frame): seek
  // the existing viewer once it has loaded.
  const seekToRequested = useEffectEvent(() => {
    const target = observations.find(o => o.id === observationId);
    if (target && target.id !== observation?.id) seek(target.time);
  });
  useEffect(() => {
    if (observationId && !status) seekToRequested();
  }, [observationId, status]);

  const duration = playback?.duration_seconds ?? 0;
  useEffect(() => {
    if (playbackStart === null || !timeline) return;
    const viewer = viewerRef.current;
    if (!viewer?.ready) return;
    const id = viewer.get_active_recording_id();
    if (!id) return;
    const end = duration;
    // An equivalent manifest from a revalidation must not restart this loop.
    const observations = playbackValues().observations;
    let lastFrame = performance.now();
    let current = playbackStart;
    let frameId: number;
    let waiting = false;
    let cancelled = false;
    const request = seekRequest.current;
    const advance = (now: number) => {
      if (cancelled || request !== seekRequest.current) return;
      // Do not jump over the episode when a background tab resumes.
      const nextTime = Math.min(end, current + Math.min(now - lastFrame, 100) / 1000 * speed);
      lastFrame = now;
      const index = observations.findLastIndex(observation => observation.time <= nextTime + 0.000001);
      const images = sceneLayout ? imagesRef.current : null;
      if (images && index >= 0 && !images.get(index)) {
        if (!waiting) {
          waiting = true;
          setBuffering(true);
          void images.load(index).then(() => {
            if (cancelled || request !== seekRequest.current) return;
            waiting = false;
            setBuffering(false);
          }).catch(() => {
            if (cancelled || request !== seekRequest.current) return;
            setBuffering(false);
            setImageError(true);
            setPlaybackStart(null);
          });
        }
        frameId = requestAnimationFrame(advance);
        return;
      }
      current = nextTime;
      images?.pin(index);
      setTime(current);
      if (images) setObservationImage(images.get(index) ?? null);
      viewer.set_current_time(id, timeline, current * 1e9);
      if (current >= end) setPlaybackStart(null);
      else frameId = requestAnimationFrame(advance);
    };
    frameId = requestAnimationFrame(advance);
    return () => {
      cancelled = true;
      cancelAnimationFrame(frameId);
    };
  }, [playbackStart, speed, timeline, duration, sceneLayout]);

  function seek(seconds: number, actionId: string | null = null) {
    setPlaybackStart(null);
    setImageError(false);
    const request = ++seekRequest.current;
    const viewer = viewerRef.current;
    const id = viewer?.get_active_recording_id();
    if (!viewer || !id || !timeline) return;
    const apply = () => {
      if (request !== seekRequest.current || viewer !== viewerRef.current) return;
      setBuffering(false);
      setSelectedActionId(actionId);
      images?.pin(index);
      setTime(seconds);
      if (images) setObservationImage(images.get(index) ?? null);
      viewer.set_playing(id, false);
      viewer.set_current_time(id, timeline, seconds * 1e9);
    };
    const images = sceneLayout ? imagesRef.current : null;
    const index = observations.findLastIndex(observation => observation.time <= seconds + 0.000001);
    if (images && index >= 0 && !images.get(index)) {
      setBuffering(true);
      void images.load(index).then(apply).catch(() => {
        if (request !== seekRequest.current || viewer !== viewerRef.current) return;
        setBuffering(false);
        setImageError(true);
      });
    } else apply();
  }

  function togglePlaying() {
    const viewer = viewerRef.current;
    const id = viewer?.get_active_recording_id();
    if (!viewer || !id) return;
    seekRequest.current++;
    setBuffering(false);
    setImageError(false);
    setSelectedActionId(null);
    setPlaybackStart(playing ? null : playback && time >= playback.duration_seconds ? startTime : time);
  }

  function toggleDiagnostics() {
    const next = !diagnostics;
    viewerRef.current?.override_panel_state(
      "blueprint",
      next ? "expanded" : "hidden"
    );
    viewerRef.current?.override_panel_state(
      "selection",
      next ? "collapsed" : "hidden"
    );
    // The Oddish controls keep ownership of seeking in either mode.
    setDiagnostics(next);
  }

  return (
    <div
      ref={frame}
      className="bg-card flex h-full min-h-0 w-full flex-col [&:fullscreen]:h-screen"
      aria-label={`Robot recording: ${fileName}`}
    >
      <div className={`grid min-h-0 flex-1 ${sceneLayout && observations.length ? "grid-cols-[minmax(0,2fr)_minmax(0,1fr)]" : "grid-cols-1"}`}>
        <div className={`relative min-h-0 overflow-hidden ${sceneLayout ? "bg-[#1e232a]" : ""}`} data-testid="reviewer-scene"
          onContextMenuCapture={event => { if (sceneLayout) { event.preventDefault(); event.stopPropagation(); } }}
          onMouseDownCapture={event => { if (sceneLayout && event.button === 2) { event.preventDefault(); event.stopPropagation(); } }}
          onKeyDownCapture={event => {
            if (!sceneLayout || !(event.target instanceof HTMLCanvasElement) || event.key === "Tab") return;
            // Keep camera pointer navigation, but don't expose Rerun's layout,
            // hide/delete, or command-palette shortcuts in the replay surface.
            event.preventDefault();
            event.stopPropagation();
            if (event.code === "Space" && !status) togglePlaying();
          }}>
          <div ref={container} className="h-full w-full" />
          {sceneLayout && <div data-testid="reviewer-heading"
            className="absolute inset-x-0 top-0 z-10 flex h-14 items-center bg-[#1e232a] px-3 text-xs text-white/85 select-none">
            {/* Rerun 0.38's viewport title/drag controls occupy the top 56px.
                One scene-only blueprint plus this fixed header prevents tile editing;
                the uncovered 3D canvas retains orbit, pan and zoom. */}
            Reviewer: complete scene
          </div>}
          {status && (
            <p role="status" className="bg-card absolute inset-0 z-20 p-4 text-sm">
              {status}
            </p>
          )}
        </div>
        {sceneLayout && observations.length > 0 && <figure aria-label="Agent observations" aria-busy={buffering}
          className="border-border flex min-h-0 min-w-0 flex-col border-l bg-black" data-testid="agent-observations">
          <figcaption className="bg-card sticky top-0 z-10 flex min-h-14 shrink-0 flex-wrap items-center justify-between gap-x-2 gap-y-1 px-3 py-2 text-xs">
            <span>Agent view · Observation frames</span>
            {observation && observationImage && <span className="text-muted-foreground tabular-nums" data-testid="observation-time">
              Frame {observationImage.index + 1} / {observations.length} · Captured {observation.time.toFixed(1)} s
            </span>}
          </figcaption>
          <div className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden">
            {/* Keep the authenticated observation pixels unchanged. */}
            {observationImage && <img src={observationImage.src}
              alt="" role="img" aria-label="Agent view" data-frame-index={observationImage.index}
              draggable={false}
              className="pointer-events-none h-full w-full object-contain select-none" />}
            {(buffering || imageError) && <span role={imageError ? "alert" : "status"}
              className="absolute bottom-2 left-2 rounded bg-black/70 px-2 py-1 text-xs text-white">
              {imageError ? "Frame unavailable" : "Buffering"}
            </span>}
          </div>
        </figure>}
      </div>
      {observation && renderObservation && <div className="h-36 max-h-[30%] shrink-0 overflow-auto">
        {renderObservation(observation)}
      </div>}
      {playback && (
        <div className="border-border shrink-0 space-y-2 border-t px-3 py-2">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={!!status}
              onClick={togglePlaying}
              aria-label={playing ? "Pause replay" : "Play replay"}
              className="hover:bg-muted rounded p-1 disabled:opacity-40"
            >
              {playing ? (
                <Pause className="size-4" />
              ) : (
                <Play className="size-4" />
              )}
            </button>
            <button
              type="button"
              disabled={!!status}
              onClick={() => seek(startTime)}
              aria-label="Restart replay"
              className="hover:bg-muted rounded p-1 disabled:opacity-40"
            >
              <RotateCcw className="size-4" />
            </button>
            <button type="button" disabled={!!status || previous === undefined}
              aria-label="Previous observation" onClick={() => previous !== undefined && seek(previous)}
              className="hover:bg-muted rounded p-1 disabled:opacity-40">
              <ChevronLeft className="size-4" />
            </button>
            <button type="button" disabled={!!status || next === undefined}
              aria-label="Next observation" onClick={() => next !== undefined && seek(next)}
              className="hover:bg-muted rounded p-1 disabled:opacity-40">
              <ChevronRight className="size-4" />
            </button>
            <select aria-label="Playback speed" value={speed} disabled={!!status}
              onChange={event => {
                if (playing) {
                  setBuffering(false);
                  setPlaybackStart(time);
                }
                setSpeed(Number(event.target.value));
              }}
              className="bg-card rounded px-1 py-1 text-xs">
              {[0.25, 0.5, 1, 2].map(value => <option key={value} value={value}>{value}×</option>)}
            </select>
            <input
              aria-label="Simulation time"
              type="range"
              min={startTime}
              max={playback.duration_seconds || 0.001}
              step={0.01}
              value={Math.min(time, playback.duration_seconds)}
              disabled={!!status}
              onChange={(event) => seek(Number(event.target.value))}
              className="min-w-16 flex-1 accent-neutral-400"
            />
            <span className="text-muted-foreground shrink-0 text-xs tabular-nums">
              Replay {time.toFixed(1)} / {playback.duration_seconds.toFixed(1)} s
            </span>
            {!sceneLayout && <button
              type="button"
              disabled={!!status}
              aria-pressed={diagnostics}
              onClick={toggleDiagnostics}
              className="text-muted-foreground hover:bg-muted aria-pressed:bg-muted rounded px-2 py-1 text-xs"
            >
              Data
            </button>}
            <button
              type="button"
              aria-label="Full screen replay"
              onClick={() => {
                if (document.fullscreenElement === frame.current)
                  void document.exitFullscreen();
                else void frame.current?.requestFullscreen();
              }}
              className="hover:bg-muted rounded p-1"
            >
              <Maximize2 className="size-4" />
            </button>
          </div>
          <div className="space-y-2 text-xs">
            <div className="flex items-center gap-2">
              <button type="button" aria-expanded={actionsOpen} aria-controls={actionsId}
                onClick={() => setActionsOpen(!actionsOpen)}
                className="hover:bg-muted shrink-0 rounded px-2 py-1">Actions</button>
              {activeAction && <>
                <span className="text-muted-foreground min-w-0 flex-1 truncate" title={activeAction.label}>
                  {actions.indexOf(activeAction) + 1} · {activeAction.label}
                </span>
                <button type="button" disabled={!!status}
                  aria-pressed={Math.abs(time - activeAction.time) < 0.001}
                  onClick={() => seek(activeAction.time, activeAction.id)}
                  className="hover:bg-muted rounded px-2 py-1 aria-pressed:bg-muted">Before</button>
                <button type="button" disabled={!!status}
                  aria-pressed={Math.abs(time - activeAction.end_time) < 0.001}
                  onClick={() => seek(activeAction.end_time, activeAction.id)}
                  className="hover:bg-muted rounded px-2 py-1 aria-pressed:bg-muted">After</button>
              </>}
            </div>
            {actionsOpen && <ol id={actionsId} aria-label="Agent actions"
              className="max-h-32 overflow-auto">
              {actions.length === 0 && <li>No recorded actions</li>}
              {actions.map((action, index) => (
                <li key={action.id} aria-current={activeAction?.id === action.id ? "step" : undefined}
                  className="rounded aria-[current=step]:bg-muted">
                  <button type="button" disabled={!!status} onClick={() => seek(action.time, action.id)}
                    className="flex w-full items-baseline gap-3 px-2 py-1.5 text-left hover:bg-muted disabled:opacity-40">
                    <span className="text-muted-foreground w-5 shrink-0 tabular-nums">{index + 1}</span>
                    <span className="text-muted-foreground w-12 shrink-0 tabular-nums">{action.time.toFixed(1)} s</span>
                    <span>{action.label}</span>
                  </button>
                </li>
              ))}
            </ol>}
          </div>
        </div>
      )}
    </div>
  );
}
