// Shared by the QA issues pages, the experiment page, and the delivery page's
// client card.
import Link from "next/link";
import type { ReactNode } from "react";
import { ShieldAlert } from "lucide-react";
import { QaBurndownChart } from "@/components/qa-burndown-chart";
import { BURNDOWN_SERIES, type QaIssueReport } from "@/lib/qa-issues";

export function Bar({ parts, of }: { parts: [number, string][]; of?: number }) {
  const total = Math.max(1, of ?? parts.reduce((s, [n]) => s + n, 0));
  return (
    <div className="bg-muted flex h-2 w-full overflow-hidden rounded-full">
      {parts.map(([n, color], i) => (
        <div
          key={i}
          className={color}
          style={{ width: `${(100 * n) / total}%` }}
        />
      ))}
    </div>
  );
}

export function QaSplit({
  summary,
  burndown,
  href,
  pointsHref,
  selected,
  action,
}: {
  summary: QaIssueReport["summary"];
  burndown: QaIssueReport["burndown"];
  /** Set where the card links to the full QA issues page. */
  href?: string;
  /** The QA issues page that each chart point opens at its version. */
  pointsHref?: string;
  /** The version the report shows; undefined for Now. */
  selected?: number;
  /** A control at the end of the count row. */
  action?: ReactNode;
}) {
  return (
    <section className="bg-card space-y-3 rounded-xl border p-4">
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1.5">
        {href && (
          <Link
            href={href}
            className="hover:bg-muted order-last ml-auto inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs"
          >
            <ShieldAlert className="h-3.5 w-3.5" />
            QA issues
          </Link>
        )}
        {action && <div className="order-last ml-auto">{action}</div>}
        <h2 className="text-sm font-medium">
          <span className="text-muted-foreground">Tasks:</span>{" "}
          <span className="text-2xl font-semibold tabular-nums">
            {summary.tasks}
          </span>
        </h2>
        {BURNDOWN_SERIES.map((s) => (
          <span key={s.key} className="flex items-baseline gap-1.5 text-sm">
            <span className={`h-2 w-2 self-center rounded-full ${s.dot}`} />
            <span className="text-muted-foreground">{s.label}:</span>
            <span className="font-semibold tabular-nums">{summary[s.key]}</span>
            {summary.tasks > 0 && (
              <span className="text-muted-foreground tabular-nums">
                ({Math.round((100 * summary[s.key]) / summary.tasks)}%)
              </span>
            )}
          </span>
        ))}
      </div>
      {burndown.length > 1 ? (
        <QaBurndownChart
          points={burndown}
          baseHref={pointsHref}
          selected={selected}
        />
      ) : (
        <Bar parts={BURNDOWN_SERIES.map((s) => [summary[s.key], s.dot])} />
      )}
    </section>
  );
}
