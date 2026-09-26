"use client";

import { memo, useMemo } from "react";
import type { Task, Trial } from "@/lib/types";
import { getExperimentAgentKey } from "@/lib/experiment-agent-grouping";
import type { AgentSummary } from "./experiment-trials-table";
import { AGENT_COLORS } from "./pass-at-k-graph";
import { LeaderboardBars } from "./leaderboard-bars";

interface PassAtOneLeaderboardProps {
  tasks: Task[];
  agentSummaries: AgentSummary[];
  groupEfforts?: boolean;
  hiddenAgents: Set<string>;
  onToggleAgent?: (agent: string) => void;
  hoverAgent?: string | null;
  onHoverAgent?: (key: string | null) => void;
}

type LeaderboardRow = {
  key: string;
  label: string;
  agent: string;
  model: string | null;
  queueKey: string | null;
  mean: number;
};

function getPassAtOneValue(trials: Trial[]): number | null {
  // Skipped trials are counted as non-passes in the denominator (like a harness
  // error): a skipped trial did not pass, so it lowers pass@1 accordingly.
  if (trials.length === 0) return null;
  const passing = trials.filter((trial) => trial.reward === 1).length;
  return passing / trials.length;
}

function calculateRows(
  tasks: Task[],
  agentSummaries: AgentSummary[],
  groupEfforts: boolean
): LeaderboardRow[] {
  const rows: LeaderboardRow[] = [];

  for (const summary of agentSummaries) {
    const taskValues: number[] = [];
    for (const task of tasks) {
      const trials = (task.trials ?? []).filter(
        (trial) => getExperimentAgentKey(trial, groupEfforts) === summary.key
      );
      const value = getPassAtOneValue(trials);
      if (value !== null) {
        taskValues.push(value);
      }
    }

    if (taskValues.length === 0) continue;

    const mean =
      taskValues.reduce((acc, value) => acc + value, 0) / taskValues.length;
    rows.push({
      key: summary.key,
      label: summary.label,
      agent: summary.agent,
      model: summary.model,
      queueKey: summary.queueKey,
      mean,
    });
  }

  return rows.sort((a, b) => b.mean - a.mean);
}

export const PassAtOneLeaderboard = memo(function PassAtOneLeaderboard({
  tasks,
  agentSummaries,
  groupEfforts = false,
  hiddenAgents,
  hoverAgent,
  onHoverAgent,
}: PassAtOneLeaderboardProps) {
  const rows = useMemo(
    () => calculateRows(tasks, agentSummaries, groupEfforts),
    [tasks, agentSummaries, groupEfforts]
  );
  const visibleRows = useMemo(
    () => rows.filter((row) => !hiddenAgents.has(row.key)),
    [rows, hiddenAgents]
  );
  const colorByAgent = useMemo(() => {
    const colors = new Map<string, string>();
    for (const [idx, summary] of agentSummaries.entries()) {
      colors.set(summary.key, AGENT_COLORS[idx % AGENT_COLORS.length]);
    }
    return colors;
  }, [agentSummaries]);

  if (rows.length === 0) {
    return null;
  }

  return (
    <LeaderboardBars
      title="Leaderboard"
      caption="score = (pass + ½·partial) / completed"
      nameHeading="Agent"
      scoreHeading="Score"
      rows={visibleRows.map((row) => ({
        ...row,
        value: row.mean,
        color: colorByAgent.get(row.key) ?? AGENT_COLORS[0],
      }))}
      hoverKey={hoverAgent}
      onHover={onHoverAgent}
      emptyText="All agents are hidden."
    />
  );
});
