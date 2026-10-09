"use client";

import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { useIncomingShares } from "@/lib/use-org-access";
import { useOrgHref } from "@/lib/use-org-href";
import type { SharedExperiment } from "@/lib/types";
import { formatDate } from "@/lib/format";

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
        {label}
      </div>
      <div className="font-mono text-lg tabular-nums">{value}</div>
    </div>
  );
}

/** Runs by outcome, the same five parts and colors as the sample page's bar,
 *  so skipped runs and harness errors never read as passes or failures. */
function OutcomeBar({ sample }: { sample: SharedExperiment }) {
  const s = sample.summary;
  const parts = s
    ? [
        { n: s.pass_count, color: "var(--paper-pass)", label: "passed" },
        { n: s.partial_count, color: "var(--paper-partial)", label: "partial" },
        { n: s.fail_count, color: "var(--paper-fail)", label: "failed" },
        {
          n: s.harness_error_count,
          color: "var(--paper-error)",
          label: "errors",
        },
        { n: s.skipped, color: "var(--paper-ink-3)", label: "skipped" },
      ]
    : [];
  const total = parts.reduce((sum, part) => sum + part.n, 0);
  return (
    <div
      className="bg-muted flex h-1.5 overflow-hidden rounded-full"
      title={parts.map((part) => `${part.n} ${part.label}`).join(" · ")}
    >
      {total > 0 &&
        parts.map((part) => (
          <div
            key={part.label}
            style={{
              width: `${(part.n / total) * 100}%`,
              background: part.color,
            }}
          />
        ))}
    </div>
  );
}

function SampleCard({
  sample,
  href,
}: {
  sample: SharedExperiment;
  href: string;
}) {
  const s = sample.summary;
  const score =
    s?.average_score != null ? `${Math.round(s.average_score * 100)}%` : "—";
  const qa = s ? `${s.qa_accepted} / ${s.qa_rejected}` : "—";
  return (
    <Link
      href={href}
      className="bg-card hover:border-foreground/30 group flex flex-col gap-4 rounded-lg border p-5 transition-colors"
    >
      <div className="min-w-0">
        <div className="flex items-start justify-between gap-3">
          <h2 className="truncate text-base font-semibold">{sample.name}</h2>
          <ArrowRight className="text-muted-foreground group-hover:text-foreground mt-0.5 h-4 w-4 shrink-0 transition-colors" />
        </div>
        {sample.description && (
          <p className="text-muted-foreground mt-1 line-clamp-2 text-sm">
            {sample.description}
          </p>
        )}
      </div>
      <div className="grid grid-cols-4 gap-3">
        <Stat label="Tasks" value={String(sample.task_count)} />
        <Stat label="Runs" value={String(sample.run_count)} />
        <Stat label="Score" value={score} />
        {sample.show_qa !== false && <Stat label="QA ✓ / ✗" value={qa} />}
      </div>
      <OutcomeBar sample={sample} />
      <div className="text-muted-foreground text-xs">
        Shared {formatDate(sample.shared_at)}
      </div>
    </Link>
  );
}

export default function SharedPage() {
  const orgHref = useOrgHref();
  const { data, error } = useIncomingShares();

  return (
    <div className="space-y-5">
      <div className="flex items-baseline gap-3">
        <h1 className="text-xl font-semibold">Samples</h1>
        {data && data.length > 0 && (
          <span className="text-muted-foreground text-sm">{data.length}</span>
        )}
      </div>
      {error ? (
        <p role="alert" className="text-sm text-red-500">
          Unable to load samples.
        </p>
      ) : !data ? (
        <div role="status" className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-48 rounded-lg" />
          ))}
        </div>
      ) : data.length === 0 ? (
        <p className="text-muted-foreground text-sm">Nothing shared yet.</p>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {data.map((sample) => (
            <SampleCard
              key={sample.id}
              sample={sample}
              href={orgHref(`/shared/${encodeURIComponent(sample.id)}`)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
