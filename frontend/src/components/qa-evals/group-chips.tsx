"use client";

import { cn } from "@/lib/utils";

/** A count per group of a list, each narrowing the list to its group (a
 * second click shows them all), then the total. */
export function GroupChips<T extends string>({
  groups,
  total,
  value,
  onChange,
}: {
  groups: { value: T; label: React.ReactNode }[];
  total: number;
  value: T | "all";
  onChange: (value: T | "all") => void;
}) {
  const chip = (option: T | "all", label: React.ReactNode) => (
    <button
      key={option}
      type="button"
      aria-pressed={option === value}
      onClick={() => onChange(option === value ? "all" : option)}
      className={cn(
        "rounded-full border px-2 py-0.5 text-xs tabular-nums",
        option === value
          ? "border-[color:var(--paper-ink)] bg-[color:var(--paper-ink)] text-[color:var(--paper-surface)]"
          : "border-[color:var(--paper-line)] hover:bg-[color:var(--paper-line-2)]/60"
      )}
    >
      {label}
    </button>
  );
  return (
    <>
      {groups.map((group) => chip(group.value, group.label))}
      <span className="text-xs text-[color:var(--paper-ink-3)]">=</span>
      {chip("all", <>all {total}</>)}
    </>
  );
}
