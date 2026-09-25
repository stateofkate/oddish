"use client";

import { useState, useTransition } from "react";
import { Loader2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  BURNDOWN_SERIES,
  qaIssuesHref,
  type QaBurndownPoint,
} from "@/lib/qa-issues";
import { useOrgPath, useQaIssuesView } from "@/lib/use-qa-issues-view";

const W = 1000;

/** The live point has no version: it is the report's own headline. */
function label(point: QaBurndownPoint) {
  return point.version === null ? "Now" : `v${point.version}`;
}
const H = 160;

function total(p: QaBurndownPoint) {
  return p.blocked + p.clear + p.waiting;
}

export function QaBurndownChart({
  points,
  baseHref,
  selected,
}: {
  points: QaBurndownPoint[];
  /** The QA issues page; when set, each point links to its version there. */
  baseHref?: string;
  /** The version the page shows; undefined for Now. */
  selected?: number;
}) {
  const router = useRouter();
  const [hover, setHover] = useState<number | null>(null);
  // On the QA issues page, keep the open category and filters in the URL.
  const view = useQaIssuesView(baseHref ?? "");
  const orgPath = useOrgPath();
  const href = (p: QaBurndownPoint) =>
    orgPath(
      qaIssuesHref(baseHref!, {
        ...view,
        page: 1,
        version: p.version ?? undefined,
      })
    );
  // A version click marks its point at once; the page follows when it loads.
  const [loading, startLoading] = useTransition();
  const [target, setTarget] = useState<number | null>(null);
  const open = (i: number) => {
    setTarget(i);
    startLoading(() => router.push(href(points[i]), { scroll: false }));
  };
  const at =
    loading && target !== null
      ? target
      : selected
        ? points.findIndex((p) => p.version === selected)
        : points.length - 1;
  const max = Math.max(1, ...points.map(total));
  const x = (i: number) => (i / (points.length - 1)) * W;
  const y = (v: number) => H - (v / max) * H;

  // Each band's polygon: its top edge left to right, its bottom edge back.
  let below = points.map(() => 0);
  const bands = BURNDOWN_SERIES.map((series) => {
    const above = points.map((p, i) => below[i] + p[series.key]);
    const path = [
      ...above.map((v, i) => `${x(i)},${y(v)}`),
      ...below.map((v, i) => `${x(i)},${y(v)}`).reverse(),
    ].join(" ");
    below = above;
    return { ...series, path };
  });

  const point = hover === null ? null : points[hover];
  return (
    <figure className="space-y-1">
      <div
        className="relative h-40"
        onPointerMove={(event) => {
          const box = event.currentTarget.getBoundingClientRect();
          const f = (event.clientX - box.left) / box.width;
          setHover(
            Math.round(Math.min(1, Math.max(0, f)) * (points.length - 1))
          );
        }}
        onPointerLeave={() => setHover(null)}
        onClick={() => {
          if (baseHref && hover !== null) open(hover);
        }}
        style={baseHref ? { cursor: "pointer" } : undefined}
      >
        <svg
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          className={`h-full w-full overflow-visible transition-opacity ${loading ? "opacity-60" : ""}`}
          aria-hidden
        >
          {bands.map((band) => (
            <polygon
              key={band.key}
              points={band.path}
              className={`${band.fill} stroke-card`}
              strokeWidth={2}
              vectorEffect="non-scaling-stroke"
            />
          ))}
          {baseHref && at >= 0 && (
            <line
              x1={x(at)}
              x2={x(at)}
              y1={0}
              y2={H}
              className="stroke-foreground"
              strokeWidth={2}
              vectorEffect="non-scaling-stroke"
            />
          )}
          {hover !== null && (
            <line
              x1={x(hover)}
              x2={x(hover)}
              y1={0}
              y2={H}
              className="stroke-foreground/60"
              strokeWidth={1}
              vectorEffect="non-scaling-stroke"
            />
          )}
        </svg>
        <span className="text-muted-foreground absolute top-0 left-1 text-[11px] tabular-nums">
          {max}
        </span>
        {loading && target !== null && (
          <span className="bg-card text-muted-foreground absolute top-0 right-1 flex items-center gap-1 rounded px-1 text-[11px]">
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
            Loading {label(points[target])}
          </span>
        )}
        {point && hover !== null && (
          <div
            className="bg-popover text-popover-foreground pointer-events-none absolute top-2 z-10 min-w-36 rounded-md border px-2.5 py-2 text-xs shadow-md"
            style={
              hover / (points.length - 1) > 0.5
                ? { right: `${100 - (100 * hover) / (points.length - 1) + 1}%` }
                : { left: `${(100 * hover) / (points.length - 1) + 1}%` }
            }
          >
            <p className="text-muted-foreground mb-1">{label(point)}</p>
            {[...BURNDOWN_SERIES].reverse().map((s) => (
              <p key={s.key} className="flex items-center gap-2">
                <span className={`h-0.5 w-3 ${s.dot}`} aria-hidden />
                <span className="font-semibold tabular-nums">
                  {point[s.key]}
                </span>
                <span className="text-muted-foreground">{s.label}</span>
              </p>
            ))}
          </div>
        )}
      </div>
      <figcaption className="text-muted-foreground flex justify-between text-[11px]">
        {(points.length <= 12
          ? points
          : [points[0], points[points.length - 1]]
        ).map((p) =>
          baseHref ? (
            <Link
              key={label(p)}
              href={href(p)}
              scroll={false}
              // Each version page builds a report on the server; load on click.
              prefetch={false}
              onClick={(event) => {
                if (event.metaKey || event.ctrlKey || event.shiftKey) return;
                event.preventDefault();
                open(points.indexOf(p));
              }}
              aria-current={points[at] === p ? "page" : undefined}
              className={
                points[at] === p
                  ? "text-foreground font-semibold"
                  : "hover:text-foreground"
              }
            >
              {label(p)}
            </Link>
          ) : (
            <span key={label(p)}>{label(p)}</span>
          )
        )}
      </figcaption>
      <table className="sr-only">
        <thead>
          <tr>
            <th>Version</th>
            {BURNDOWN_SERIES.map((s) => (
              <th key={s.key}>{s.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {points.map((p) => (
            <tr key={label(p)}>
              <td>{label(p)}</td>
              {BURNDOWN_SERIES.map((s) => (
                <td key={s.key}>{p[s.key]}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
