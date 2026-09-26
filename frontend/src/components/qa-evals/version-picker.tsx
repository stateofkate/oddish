"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import { formatDate } from "@/lib/format";
import { shortSha, type QAPromptVersion } from "@/lib/qa-evals";

/** A compact dropdown of the versions whose text was recorded: the button
 * shows hash and date, the menu each version's name with its author, date
 * and hash beneath. */
export function VersionPicker({
  label,
  versions,
  value,
  exclude,
  onChange,
}: {
  label: string;
  versions: QAPromptVersion[];
  value: QAPromptVersion | undefined;
  exclude?: string;
  onChange: (sha256: string) => void;
}) {
  const options = versions
    .filter((v) => v.text_available && v.sha256 !== exclude)
    .reverse();
  return (
    // With nothing to pick, Radix would open an empty menu.
    <Select
      value={value?.sha256 ?? ""}
      onValueChange={onChange}
      disabled={options.length === 0}
    >
      <SelectTrigger
        aria-label={label}
        className="h-7 w-auto gap-1.5 px-2 font-mono text-xs"
      >
        {/* Written here, not through SelectValue: Radix portals the chosen
            item's own (two-line) text into a SelectValue. */}
        <span className="truncate">
          {value
            ? `${shortSha(value.sha256)} · ${formatDate(value.first_seen_at)}`
            : "none"}
        </span>
      </SelectTrigger>
      <SelectContent className="max-w-[420px]">
        {options.map((v) => (
          <SelectItem
            key={v.sha256}
            value={v.sha256}
            textValue={v.name}
            className="[&>span:last-child]:min-w-0"
          >
            <span className="block truncate">{v.name}</span>
            <span className="block truncate text-xs text-[color:var(--paper-ink-3)]">
              {v.author ?? "unknown"} · {formatDate(v.first_seen_at)} ·{" "}
              {shortSha(v.sha256)}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
