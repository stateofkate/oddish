import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/** One of the QA Evals page's cards: a bordered column with a title bar
 * (`aside` sits at its right end). Children scroll where they choose to. */
export function Pane({
  title,
  aside,
  className,
  children,
}: {
  title?: React.ReactNode;
  aside?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      className={cn(
        "flex min-h-0 flex-col overflow-hidden rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)]",
        className
      )}
    >
      {title && (
        <div className="flex items-baseline gap-2 border-b border-[color:var(--paper-line-2)] px-3 py-2 text-xs font-semibold">
          {title}
          {aside && (
            <span className="ml-auto shrink-0 font-normal text-[color:var(--paper-ink-3)] tabular-nums">
              {aside}
            </span>
          )}
        </div>
      )}
      {children}
    </section>
  );
}

/** A pane's body until its data can show: a skeleton while loading, or the
 * error that stopped it, and a few words when there is nothing. A failed
 * refresh keeps the data it had on screen. */
export function PaneState({
  error,
  loading,
  empty,
  children,
}: {
  error?: { message: string } | null;
  loading?: boolean;
  empty?: React.ReactNode;
  children?: React.ReactNode;
}) {
  if (loading)
    return error ? (
      <p className="text-destructive p-3 text-sm">{error.message}</p>
    ) : (
      <Skeleton className="m-3 h-40" />
    );
  if (empty)
    return (
      <p className="p-3 text-sm text-[color:var(--paper-ink-3)]">{empty}</p>
    );
  return <>{children}</>;
}

/** From lg up, the cards below the page header fill a window of their own
 * (anything above scrolls away), so long lists and diffs have room. */
export const PANE_WINDOW =
  "lg:h-[calc(100vh-3.5rem-var(--preview-banner-h,0px)-2rem)]";

/** The cards below the page header, a narrow list beside the rest, in the
 * pane window. */
export function PaneGrid({ children }: { children: React.ReactNode }) {
  return (
    <div
      className={cn(
        "grid gap-3 lg:min-h-[640px] lg:grid-cols-[300px_minmax(0,1fr)]",
        PANE_WINDOW
      )}
    >
      {children}
    </div>
  );
}
