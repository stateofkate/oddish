"use client";

import { useState } from "react";
import { VersionPicker } from "@/components/qa-evals/version-picker";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  REASONING_EFFORT_AGENTS,
  reasoningEffortOptions,
} from "@/lib/reasoning-effort";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import {
  shortSha,
  type QAEvalStart,
  type QAEvalStatus,
  type QAPromptVersion,
  type QAStage,
} from "@/lib/qa-evals";

type RunDialogProps = {
  stage: QAStage;
  status: QAEvalStatus;
  /** Source trials picked to rerun; null replays every counted golden. */
  trials: string[] | null;
  /** The version picked first, when its text was recorded. */
  defaultVersion: QAPromptVersion | undefined;
  onClose: () => void;
  onStarted: (experimentId: string) => void;
};

type RunRequest = {
  body: QAEvalStart;
  key: string;
  check?: string;
  run?: string;
};

/** Replays QA with one recorded prompt version (POST /qa-evals) over every
 * counted golden, or over `trials` alone (a subset run), after showing what
 * it should cost. */
export function RunDialog({
  open,
  ...props
}: RunDialogProps & { open: boolean }) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && props.onClose()}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-lg">
        {/* Unmounts on close, so each opening starts a fresh form. */}
        <RunForm {...props} />
      </DialogContent>
    </Dialog>
  );
}

function RunForm({
  stage,
  status,
  trials,
  defaultVersion,
  onClose,
  onStarted,
}: RunDialogProps) {
  const { versions, cost_per_trial: perTrial } = status;
  const [version, setVersion] = useState(() =>
    defaultVersion?.text_available
      ? defaultVersion
      : versions.findLast((v) => v.text_available)
  );
  const [models, setModels] = useState("");
  const [agent, setAgent] = useState("claude-code");
  const [effort, setEffort] = useState("default");
  const [config, setConfig] = useState("");
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [batch, setBatch] = useState<RunRequest[] | null>(null);
  const selectedModels = [
    ...new Set(
      models
        .split("\n")
        .map((m) => m.trim())
        .filter(Boolean)
    ),
  ];
  const efforts = reasoningEffortOptions(agent, selectedModels[0] ?? "").filter(
    (value) =>
      selectedModels.every((model) =>
        reasoningEffortOptions(agent, model).includes(value)
      )
  );
  const passed = batch?.every(
    (item) =>
      status.runs.find((run) => run.experiment_id === item.check)
        ?.preflight_passed
  );
  const checked = batch?.every((item) => item.check);
  const [stamp] = useState(() =>
    new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "")
  );
  const count = trials?.length ?? status.goldens.counted;
  const countTrials = `${count} ${count === 1 ? "trial" : "trials"}`;
  const defaultName = version
    ? `qa-eval-${shortSha(version.sha256)}${trials ? "-rerun" : ""}-${stamp}`
    : "";

  const start = async () => {
    if (!version || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const overrides = config.trim() ? JSON.parse(config) : {};
      if (
        !overrides ||
        Array.isArray(overrides) ||
        typeof overrides !== "object"
      ) {
        throw new Error("Agent settings must be a JSON object.");
      }
      const requests: RunRequest[] = batch
        ? [...batch]
        : (selectedModels.length ? selectedModels : [null]).map((model) => ({
            key: crypto.randomUUID(),
            body: {
              stage,
              name: `${name.trim() || defaultName}${model ? `-${model}` : ""}`,
              prompt_sha256: version.sha256,
              agent,
              model,
              agent_config: {
                ...overrides,
                kwargs: {
                  ...overrides.kwargs,
                  ...(effort === "default" ? {} : { reasoning_effort: effort }),
                },
              },
              ...(trials
                ? { source_trial_ids: trials }
                : { goldens: true as const }),
            },
          }));
      setBatch([...requests]);
      for (let index = 0; index < requests.length; index++) {
        const item = requests[index];
        if (passed ? item.run : item.check) continue;
        const created = await fetcher<{ experiment_id: string }>(
          "/api/qa-evals",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": `${item.key}:${passed ? "run" : "check"}`,
            },
            body: JSON.stringify({
              ...item.body,
              ...(passed
                ? { preflight_run_id: item.check }
                : { preflight: true, name: `${item.body.name}-check` }),
            }),
          }
        );
        requests[index] = {
          ...item,
          [passed ? "run" : "check"]: created.experiment_id,
        };
        setBatch([...requests]);
        onStarted(created.experiment_id);
      }
      if (passed) onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the run");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <DialogHeader>
        <DialogTitle>
          {trials ? "Rerun picked trials" : "New run over all goldens"}
        </DialogTitle>
        <DialogDescription>
          {perTrial === null
            ? "No past runs to estimate the cost from."
            : `About ${formatCostUsd(perTrial * (count + 1) * Math.max(selectedModels.length, 1))} including checks, based on past runs. Model and settings affect cost.`}
        </DialogDescription>
      </DialogHeader>
      <fieldset
        disabled={submitting || batch !== null}
        className="space-y-3 text-sm"
      >
        <label className="flex items-center justify-between gap-3">
          Prompt version
          <VersionPicker
            label="Prompt version to run"
            versions={versions}
            value={version}
            onChange={(sha) =>
              setVersion(versions.find((v) => v.sha256 === sha))
            }
          />
        </label>
        <label className="block space-y-1">
          <span>Harness</span>
          <Input
            value={agent}
            onChange={(event) => {
              setAgent(event.target.value);
              setEffort("default");
            }}
            list="qa-agents"
          />
          <datalist id="qa-agents">
            {REASONING_EFFORT_AGENTS.map((value) => (
              <option key={value} value={value} />
            ))}
          </datalist>
        </label>
        <label className="block space-y-1">
          <span>Models (one per line)</span>
          <Textarea
            rows={3}
            value={models}
            onChange={(event) => {
              setModels(event.target.value);
              setEffort("default");
            }}
            placeholder="the production QA model"
          />
        </label>
        <label className="block space-y-1">
          <span>Reasoning effort</span>
          <select
            className="bg-background w-full rounded-md border p-2"
            value={effort}
            onChange={(event) => setEffort(event.target.value)}
          >
            {["default", ...efforts].map((value) => (
              <option key={value} value={value}>
                {value === "default" ? "Agent default" : value}
              </option>
            ))}
          </select>
        </label>
        <details>
          <summary>Agent settings</summary>
          <label className="block space-y-1">
            <span>Agent settings (JSON)</span>
            <Input
              value={config}
              onChange={(event) => setConfig(event.target.value)}
              placeholder='{"kwargs":{"max_turns":100}}'
            />
          </label>
        </details>
        <label className="block space-y-1">
          <span>Name</span>
          <Input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={defaultName}
          />
        </label>
      </fieldset>
      <p className="text-muted-foreground text-xs">
        {!status.execution_configurable
          ? "Deploy the QA execution-configuration update before running checks."
          : batch
          ? passed
            ? "Checks passed. Each harness produced a valid QA result."
            : "Checks are saved in Runs. The full set requires every check to pass."
          : "First run one golden per model. Then replay the full set with the same settings."}
      </p>
      {batch && (
        <Button
          variant="outline"
          disabled={submitting}
          onClick={() => setBatch(null)}
        >
          Change settings
        </Button>
      )}
      {error && (
        <p role="alert" className="text-xs text-red-500">
          {error}
        </p>
      )}
      <DialogFooter>
        <Button variant="outline" disabled={submitting} onClick={onClose}>
          Close
        </Button>
        <Button
          disabled={
            !status.execution_configurable ||
            !version ||
            !agent.trim() ||
            submitting ||
            count === 0 ||
            Boolean(checked && !passed)
          }
          onClick={start}
        >
          {submitting
            ? "Starting…"
            : passed
              ? `Replay ${countTrials} per model`
              : checked
                ? "Waiting for checks"
                : "Check one golden per model"}
        </Button>
      </DialogFooter>
    </>
  );
}
