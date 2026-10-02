"use client";

import { useState, type ReactNode } from "react";
import { CodeBlock } from "@/components/code-block";
import { cn } from "@/lib/utils";
import type { OutputView, ToolView } from "@/lib/trajectory-view";

// Long commands and outputs start collapsed to this many lines.
const COLLAPSE_LINES = 20;

const SOURCE_COLORS: Record<string, string> = {
  system: "text-gray-500",
  user: "text-blue-500",
  agent: "text-purple-500",
};

type ImageSource = Extract<OutputView, { kind: "image" }>["source"];
type RenderImage = (source: ImageSource) => ReactNode;

export function StepHeader({
  index,
  source,
  model,
  preview,
  badges,
  hideAgentIdentity = false,
}: {
  index: number | string;
  source: string;
  model?: string | null;
  preview?: string | null;
  badges?: ReactNode;
  hideAgentIdentity?: boolean;
}) {
  const label = source === "agent" ? "Agent" : source;
  return (
    <div className="flex min-w-0 flex-1 items-center gap-3 overflow-hidden pr-2">
      <div className="flex max-w-[40%] min-w-0 shrink-0 items-center gap-2">
        <span className="text-muted-foreground font-mono text-xs">
          #{index}
        </span>
        {!(hideAgentIdentity && source === "agent") && (
          <span
            className={cn(
              "text-xs font-medium capitalize",
              SOURCE_COLORS[source] || "text-gray-500"
            )}
          >
            {label}
          </span>
        )}
        {model && !(hideAgentIdentity && source === "agent") && (
          <span
            className="text-muted-foreground truncate text-xs"
            title={model}
          >
            {model}
          </span>
        )}
      </div>

      <span className="text-muted-foreground min-w-0 flex-1 truncate text-left text-xs">
        {preview || <span className="italic">No message</span>}
      </span>

      {badges && (
        <div className="flex shrink-0 items-center gap-1.5">{badges}</div>
      )}
    </div>
  );
}

const isFailure = (fact: string) => /^exit [^0]|^rejected$/.test(fact);

function Facts({ items }: { items: string[] }) {
  return items.map((item, index) => (
    <span
      key={index}
      title={item}
      className={cn(
        "bg-muted text-muted-foreground max-w-full truncate rounded px-1.5 font-mono text-[10px] leading-4",
        isFailure(item) && "bg-red-500/10 text-red-600 dark:text-red-400"
      )}
    >
      {item}
    </span>
  ));
}

function OutputBlock({
  view,
  renderImage,
}: {
  view: OutputView;
  renderImage?: RenderImage;
}) {
  if (view.kind === "image") return renderImage?.(view.source) ?? null;
  return (
    <div className="min-w-0 space-y-1">
      {view.meta.length > 0 && (
        <div className="flex flex-wrap gap-1">
          <Facts items={view.meta} />
        </div>
      )}
      {view.code ? (
        <CodeBlock
          code={view.code}
          copyText={view.copyText}
          language={view.language}
          collapseAfter={COLLAPSE_LINES}
        />
      ) : (
        <p className="text-muted-foreground text-xs italic">No output</p>
      )}
    </div>
  );
}

/** Outputs with no tool call to sit under: live results, unlinked results. */
export function ObservationBlock({
  outputs,
  isError,
  trailing,
  renderImage,
}: {
  outputs: OutputView[];
  isError?: boolean;
  trailing?: ReactNode;
  renderImage?: RenderImage;
}) {
  return (
    <div
      className={cn(
        "min-w-0 space-y-2",
        isError && "border-l-2 border-red-500/60 pl-2"
      )}
    >
      {outputs.length ? (
        outputs.map((view, index) => (
          <OutputBlock key={index} view={view} renderImage={renderImage} />
        ))
      ) : (
        <p className="text-muted-foreground text-xs italic">No output</p>
      )}
      {(isError || trailing) && (
        <div className="flex items-center gap-1.5 pl-0.5 text-[10px]">
          {isError && <span className="font-medium text-red-500">error</span>}
          {trailing}
        </div>
      )}
    </div>
  );
}

/** One tool call: its name and recorded facts, the input it ran, then what
 *  came back. The step header above already names the command. */
export function ToolCallCard({
  view,
  trailing,
  renderImage,
}: {
  view: ToolView;
  trailing?: ReactNode;
  renderImage?: RenderImage;
}) {
  const facts = [...(view.input?.meta ?? []), ...view.meta];
  const failed = view.outputs.some(
    (output) => output.kind === "code" && output.meta.some(isFailure)
  );
  return (
    <div
      className={cn(
        "min-w-0 overflow-hidden rounded-md border",
        failed ? "border-red-500/40" : "border-purple-500/20"
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-1.5 bg-purple-500/10 px-2 py-1">
        <span className="font-mono text-xs text-purple-600 dark:text-purple-400">
          {view.name || "tool"}
        </span>
        <Facts items={facts} />
        {trailing}
      </div>
      <div className="min-w-0 space-y-2 p-2">
        {view.input && (
          <CodeBlock
            code={view.input.code}
            language={view.input.language}
            collapseAfter={COLLAPSE_LINES}
          />
        )}
        {view.outputs.map((output, index) => (
          <OutputBlock key={index} view={output} renderImage={renderImage} />
        ))}
      </div>
    </div>
  );
}

/** The recorded step, verbatim; mounted only when opened. */
export function RawDisclosure({ value }: { value: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs">
        Raw step JSON
      </summary>
      {open && (
        <CodeBlock
          code={JSON.stringify(value, null, 2)}
          language="json"
          collapseAfter={COLLAPSE_LINES * 2}
          className="mt-1"
        />
      )}
    </details>
  );
}
