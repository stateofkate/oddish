"use client";

import { useState } from "react";
import { BURNDOWN_SERIES, type QaBurndownPoint } from "@/lib/qa-issues";

const W = 1000;

/** The live point has no version: it is the report's own headline. */
function label(point: QaBurndownPoint) {
  return point.version === null ? "Now" : `v${point.version}`;
}
const H = 160;

function total(p: QaBurndownPoint) {
  return p.blocked + p.clear + p.waiting;
}

export function QaBurndownChart({ points }: { points: QaBurndownPoint[] }) {
  const [hover, setHover] = useState<number | null>(null);
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
      >
        <svg
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          className="h-full w-full overflow-visible"
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
        ).map((p) => (
          <span key={label(p)}>{label(p)}</span>
        ))}
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
