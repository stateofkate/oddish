"use client";
import { Suspense, useState } from "react";
import { TaskOverviewPanel } from "@/components/task-overview-panel";
import { finding, replayFinding } from "../../adversarial";
import { tasks } from "../../records";

export default function AdversarialPreview() {
  const [opened, setOpened] = useState("");
  const [version, setVersion] = useState(7);
  return <main className="mx-auto max-w-3xl p-6">
    <h1 className="text-xl">Adversarial evidence</h1>
    <p>Local fixture showing the production findings component.</p>
    <label>Task version
      <select aria-label="Task version" value={version} onChange={event => setVersion(Number(event.target.value))}>
        <option value={7}>v7</option><option value={6}>v6</option>
      </select>
    </label>
    <output aria-label="Opened trial">{opened}</output>
    <Suspense fallback={<p>Loading…</p>}>
      <TaskOverviewPanel taskId="task-a" taskVersionId={`task-a-v${version}`}
        experiments={[{ id: "one", name: "Original experiment" }, { id: "two", name: "Another experiment" }, { id: "qa", name: "Original experiment (qa report)" }]} apiBaseUrl="/api/adversarial-demo"
        version={version} verdictTask={{ ...tasks[0], id: "task-a", current_version: 7 }} checksFindings={version === 7 ? [finding, replayFinding] : []} checksStatus="success"
        checksRerunning={false} onOpenTrial={(trial) => { setOpened(trial.id); return true; }} />
    </Suspense>
  </main>;
}
