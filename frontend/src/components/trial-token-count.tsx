import type { Trial } from "@/lib/types";
import {
  TooltipProvider,
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { formatTokenCount } from "@/lib/format";

/** Input includes cache reads; cache writes remain part of uncached input. */
export function TrialTokenCount({
  trial,
}: {
  trial: Pick<Trial, "input_tokens" | "cache_tokens" | "output_tokens">;
}) {
  const valid = (value: number | null | undefined) =>
    value != null && Number.isFinite(value) && value >= 0 ? value : null;
  const input = valid(trial.input_tokens);
  const cached = valid(trial.cache_tokens);
  const output = valid(trial.output_tokens);
  const uncached =
    input != null && cached != null && cached <= input ? input - cached : null;
  const total = input != null && output != null ? input + output : null;
  const format = (value: number | null) =>
    value == null ? "—" : value.toLocaleString();
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            tabIndex={0}
            className="focus-visible:outline-ring rounded-sm focus-visible:outline"
          >
            {total == null ? "— tokens" : formatTokenCount(total)}
          </span>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="max-w-xs">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 tabular-nums">
            <dt>Input</dt>
            <dd className="text-right">{format(input)}</dd>
            <dt>Cached input</dt>
            <dd className="text-right">{format(cached)}</dd>
            <dt>Uncached input</dt>
            <dd className="text-right">{format(uncached)}</dd>
            <dt>Output</dt>
            <dd className="text-right">{format(output)}</dd>
          </dl>
          <p className="mt-2">
            Total = input + output. Cached input is already included.
          </p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
