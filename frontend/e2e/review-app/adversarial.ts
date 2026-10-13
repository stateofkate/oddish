import type { PreTrialFinding, Trial } from "../../src/lib/types";
import { tasks } from "./records";

export const finding: PreTrialFinding = {
  id: "reference-leak", source: "pre_trial", dimension: "info_leakage",
  problem_type: "mismatch", tier: "must_fix", file: "environment/Dockerfile",
  line_start: 8, line_end: 8, title: "Reference solution is readable",
  detail: "The image contains the protected reference implementation.",
  recommendation: "Remove the reference from the agent image.",
};
// The model the fixture's attacks ran.
export const ADVERSARIAL_FIXTURE_MODEL = "openai/gpt-6-luna";
const solver = tasks[0].trials![0];
// A replay that copied a protected file out records this must-fix finding.
export const replayFinding: PreTrialFinding = {
  id: "adversarial-leak:tests/expected.json", source: "adversarial", dimension: "info_leakage",
  problem_type: "incompleteness", tier: "must_fix", file: "tests/expected.json",
  line_start: 1, line_end: 1, title: "The solver can read tests/expected.json.",
  detail: "Exploit replay replay-leak copied tests/expected.json byte for byte out of a fresh copy of the task before grading.",
  recommendation: "Keep tests/expected.json out of the solver's image and filesystem.",
  replay_trial_id: "replay-leak",
};
const attack = { ...solver, kind: "adversarial", attempts: 1, has_trajectory: true,
  analysis_status: null, analysis: null } as Trial;
export const adversarialTrials: Trial[] = [solver, {
  ...attack, id: "attack-blocked", name: "Attack 3", created_at: "2026-09-29T23:00:00Z",
  agent: "codex", reward: 0, status: "success",
}, {
  ...attack, id: "attack-leak", name: "Attack 1", agent: "codex",
  model: ADVERSARIAL_FIXTURE_MODEL, reward: 0, status: "success",
}, {
  // The replay failed the grader, but its leak finding still makes the attack a must fix.
  ...attack, id: "replay-leak", name: "Replay 1", agent: "exploit-replay", reward: 0,
  status: "success", harbor_config: { adversarial: { replay_of: "attack-leak", attack_attempt: 1 } },
}, {
  ...attack, id: "attack-setup", name: "Attack 2", model: ADVERSARIAL_FIXTURE_MODEL,
  status: "failed", reward: null, agent: "claude-code", has_trajectory: false,
  error_message: "pip install --quiet litellm==1.83.14\nConnectionResetError(104, Connection reset by peer)",
}];

// A newer run on another version must not replace v7's latest card.
adversarialTrials.push({
  ...adversarialTrials.find(trial => trial.id === "attack-setup")!,
  id: "attack-v6", name: "Version 6 attack", task_version: 6,
  task_version_id: "task-a-v6", created_at: "2026-09-30T00:00:00Z",
});
