"use client";

import { useEffect, useRef, useState } from "react";

import type { AgentTiming } from "@/components/agent-elapsed-time";
import { fetcher } from "@/lib/api";

export interface LiveEvent {
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
}

export interface LiveUsage {
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
}

export interface LiveResponse {
  agent_timing?: AgentTiming | null;
  attempt: number;
  events: LiveEvent[];
  next_seq: number;
  usage: LiveUsage;
  harbor_stage: string | null;
  done: boolean;
}

export interface TrialLiveEvents {
  events: LiveEvent[];
  last: LiveResponse | null;
  error: string | null;
  /** Increments when a new trial attempt replaces the events. */
  restarts: number;
}

const POLL_MS = 10_000;

export interface TrialLiveEventsOptions {
  apiBaseUrl?: string;
  enabled?: boolean;
  /** Stop following the cursor once this returns true for a response. */
  stopWhen?: (data: LiveResponse) => boolean;
  /** Start over when this changes, e.g. the trial's attempt number. */
  resetKey?: string | number;
}

/** Poll a trial's live events (`/trials/{id}/live`), following its cursor. */
export function useTrialLiveEvents(
  trialId: string,
  {
    apiBaseUrl = "/api",
    enabled = true,
    stopWhen,
    resetKey,
  }: TrialLiveEventsOptions = {}
): TrialLiveEvents {
  const stopWhenRef = useRef(stopWhen);
  useEffect(() => {
    stopWhenRef.current = stopWhen;
  }, [stopWhen]);

  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [last, setLast] = useState<LiveResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [restarts, setRestarts] = useState(0);

  // A different trial, API or reset key starts from nothing: no render shows
  // the previous rows, and the next poll starts from the first event.
  const source = `${apiBaseUrl}|${trialId}|${resetKey ?? ""}`;
  const [loadedSource, setLoadedSource] = useState(source);
  if (loadedSource !== source) {
    setLoadedSource(source);
    setEvents([]);
    setLast(null);
    setError(null);
    setRestarts(0);
  }

  const attemptRef = useRef<number | null>(null);
  const afterSeqRef = useRef(0);
  const cursorSourceRef = useRef(source);

  useEffect(() => {
    if (cursorSourceRef.current !== source) {
      cursorSourceRef.current = source;
      attemptRef.current = null;
      afterSeqRef.current = 0;
    }
    if (!enabled) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const params =
          `after_seq=${afterSeqRef.current}` +
          (attemptRef.current != null ? `&attempt=${attemptRef.current}` : "");
        const data = await fetcher<LiveResponse>(
          `${apiBaseUrl}/trials/${trialId}/live?${params}`
        );
        if (cancelled) return;

        const restart =
          attemptRef.current != null && data.attempt > attemptRef.current;
        attemptRef.current = data.attempt;
        afterSeqRef.current = data.next_seq;
        if (restart) {
          setRestarts((count) => count + 1);
          setEvents(data.events);
        } else if (data.events.length) {
          setEvents((prev) => [...prev, ...data.events]);
        }
        setLast(data);
        setError(null);
        if (stopWhenRef.current?.(data)) return;
        timer = setTimeout(poll, data.events.length ? 0 : POLL_MS);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load");
        timer = setTimeout(poll, POLL_MS);
      }
    };

    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [trialId, apiBaseUrl, enabled, source]);

  return { events, last, error, restarts };
}
