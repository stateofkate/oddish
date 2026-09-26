"use client";

import { useRef, useState } from "react";
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

/** Replays QA with one recorded prompt version (POST /qa-evals) over every
 * counted golden, or over `trials` alone (a subset run), after showing what
 * it should cost. */
export function RunDialog({
  open,
  ...props
}: RunDialogProps & { open: boolean }) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && props.onClose()}>
      <DialogContent className="sm:max-w-lg">
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
  const [model, setModel] = useState("");
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<{ body: string; key: string } | null>(null);
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
      const body: QAEvalStart = {
        stage,
        name: name.trim() || defaultName,
        prompt_sha256: version.sha256,
        model: model.trim() || null,
        ...(trials ? { source_trial_ids: trials } : { goldens: true }),
      };
      const serialized = JSON.stringify(body);
      if (request.current?.body !== serialized) {
        request.current = { body: serialized, key: crypto.randomUUID() };
      }
      const created = await fetcher<{ experiment_id: string }>(
        "/api/qa-evals",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": request.current.key,
          },
          body: request.current.body,
        }
      );
      onClose();
      onStarted(created.experiment_id);
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
            : `About ${formatCostUsd(perTrial * count)}: ${countTrials} at ${formatCostUsd(perTrial)} each.`}
        </DialogDescription>
      </DialogHeader>
      <div className="space-y-3 text-sm">
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
          <span>Model</span>
          <Input
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder="the production QA model"
          />
        </label>
        <label className="block space-y-1">
          <span>Name</span>
          <Input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={defaultName}
          />
        </label>
        {error && <p className="text-xs text-red-500">{error}</p>}
      </div>
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button
          disabled={!version || submitting || count === 0}
          onClick={start}
        >
          {submitting ? "Starting…" : `Replay ${countTrials}`}
        </Button>
      </DialogFooter>
    </>
  );
}
