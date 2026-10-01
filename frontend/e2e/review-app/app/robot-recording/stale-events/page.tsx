"use client";

import { useEffect, useState } from "react";
import { WebViewer, type WebViewerEvents, type TimeUpdateEvent } from "@rerun-io/web-viewer";
import { READY_PLAYBACK, RobotTrialPreview } from "../../../robot-trial";

/** Hold real viewer notifications so tests can deliver an old seek last. */
export default function StaleViewerEventsPreview() {
  const [pendingCount, setPendingCount] = useState(0);
  const [ready, setReady] = useState(false);
  const [release, setRelease] = useState<(() => void) | null>(null);
  useEffect(() => {
    const on = WebViewer.prototype.on;
    const subscribe = on as (this: WebViewer, event: keyof WebViewerEvents, callback: (event: never) => void) => () => void;
    const pending: (() => void)[] = [];
    WebViewer.prototype.on = function (event: keyof WebViewerEvents, callback: (event: never) => void) {
      if (event !== "time_update") return subscribe.call(this, event, callback);
      const notify = callback as (event: TimeUpdateEvent) => void;
      return subscribe.call(this, "time_update", (update: TimeUpdateEvent) => {
        pending.push(() => notify(update));
        setPendingCount(pending.length);
      });
    };
    setRelease(() => () => {
      for (const notify of pending.splice(0).reverse()) notify();
      setPendingCount(0);
    });
    setReady(true);
    return () => { WebViewer.prototype.on = on; };
  }, []);
  return <>
    <output data-testid="pending-viewer-events">{pendingCount}</output>
    <button onClick={() => release?.()}>Deliver viewer events in reverse</button>
    {ready && <RobotTrialPreview playback={READY_PLAYBACK} />}
  </>;
}
