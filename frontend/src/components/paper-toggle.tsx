import { Button, type ButtonProps } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** An on/off button in the paper theme, filled with ink while pressed (the
 * experiment page's Pass/k graph button, the QA Evals chart switch). */
export function PaperToggle({
  pressed,
  className,
  ...props
}: ButtonProps & { pressed: boolean }) {
  return (
    <Button
      type="button"
      variant="ghost"
      aria-pressed={pressed}
      className={cn(
        "h-8 gap-[7px] rounded-[7px] border px-3 text-[12px] leading-none transition-colors select-none",
        pressed
          ? "border-[color:var(--paper-ink)] bg-[color:var(--paper-ink)] text-[color:var(--paper-bg)] hover:bg-[color:color-mix(in_oklch,var(--paper-ink),white_12%)]"
          : "border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] text-[color:var(--paper-ink)] hover:border-[color:var(--paper-ink-4)] hover:bg-[color:var(--paper-surface-2)]",
        className
      )}
      {...props}
    />
  );
}
