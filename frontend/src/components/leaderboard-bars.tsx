"use client";

import { QueueKeyIcon } from "./queue-key-icon";

export type LeaderboardBarRow = {
  key: string;
  label: string;
  badge?: string;
  hoverKey?: string;
  agent?: string | null;
  model?: string | null;
  queueKey?: string | null;
  /** 0–1: the bar's length and the score shown. */
  value: number;
  color: string;
  /** Small text under the label. */
  detail?: string;
};

const SCALE_TICKS = [0, 0.25, 0.5, 0.75, 1];

/** A ranked card of agent or model rows, each with its provider icon, score
 * and a bar on a 0–100% scale, in the order given. */
export function LeaderboardBars({
  title,
  caption,
  nameHeading,
  scoreHeading,
  rows,
  hoverKey,
  onHover,
  emptyText,
}: {
  title: string;
  caption: string;
  nameHeading: string;
  scoreHeading: string;
  rows: LeaderboardBarRow[];
  hoverKey?: string | null;
  onHover?: (key: string | null) => void;
  emptyText: string;
}) {
  return (
    <section
      aria-label={title}
      className="flex h-full min-w-0 flex-col rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3"
    >
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h3 className="font-display text-[15px] font-medium tracking-[-0.01em] text-[color:var(--paper-ink)]">
          {title}
        </h3>
        <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
          {caption}
        </span>
      </div>

      <div className="grid grid-cols-[1fr_60px] border-b border-[color:var(--paper-line-2)] pb-1.5 font-mono text-[9.5px] font-semibold tracking-[0.12em] text-[color:var(--paper-ink-3)] uppercase">
        <span>{nameHeading}</span>
        <span className="text-right">{scoreHeading}</span>
      </div>

      <div className="flex flex-col">
        {rows.map((row, index) => {
          const isDim =
            hoverKey != null && hoverKey !== (row.hoverKey ?? row.key);
          const isLast = index === rows.length - 1;
          return (
            <div
              key={row.key}
              className={`grid grid-cols-[1fr_60px] items-center pt-2 pb-1.5 transition-opacity ${
                isLast
                  ? ""
                  : "border-b border-dashed border-[color:var(--paper-line-2)]"
              }`}
              style={{ opacity: isDim ? 0.32 : 1 }}
              onMouseEnter={() => onHover?.(row.hoverKey ?? row.key)}
              onMouseLeave={() => onHover?.(null)}
            >
              <div className="flex min-w-0 items-center gap-2">
                <QueueKeyIcon
                  queueKey={row.queueKey}
                  model={row.model}
                  agent={row.agent}
                  size={13}
                  className="shrink-0"
                />
                <span className="truncate font-mono text-[11.5px] text-[color:var(--paper-ink)]">
                  {row.label}
                </span>
                {row.badge && (
                  <span className="shrink-0 rounded border border-[color:var(--paper-line)] px-1.5 py-0.5 text-[10px] text-[color:var(--paper-ink-3)]">
                    {row.badge}
                  </span>
                )}
              </div>
              <div className="text-right font-mono text-xs font-semibold tracking-[-0.01em] text-[color:var(--paper-ink)]">
                {(row.value * 100).toFixed(1)}%
              </div>
              {row.detail && (
                <div className="col-span-2 font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
                  {row.detail}
                </div>
              )}
              <div className="col-span-2 mt-1.5 h-1.5 overflow-hidden rounded-full bg-[color:var(--paper-bg-2)]">
                <div
                  className="h-full rounded-full transition-[width] duration-200"
                  style={{
                    width: `${row.value * 100}%`,
                    background: `linear-gradient(90deg, color-mix(in oklch, ${row.color}, white 18%) 0%, ${row.color} 100%)`,
                  }}
                />
              </div>
            </div>
          );
        })}
        {rows.length === 0 && (
          <div className="py-6 text-center text-xs text-[color:var(--paper-ink-3)]">
            {emptyText}
          </div>
        )}
      </div>

      <div className="mt-auto flex justify-between border-t border-[color:var(--paper-line-2)] pt-2 font-mono text-[9.5px] text-[color:var(--paper-ink-3)]">
        {SCALE_TICKS.map((tick) => (
          <span key={tick}>{Math.round(tick * 100)}%</span>
        ))}
      </div>
    </section>
  );
}
