"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import { PackagePlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

import { SavedFiltersMenu } from "@/components/saved-filters-menu";
import { DeliveryCreateDialog } from "@/components/delivery-create-dialog";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import { isOrgAdminRole } from "@/lib/org-roles";
import {
  parseStoredSelection,
  SELECTION_LIMIT,
  selectionStorageKey,
  selectionTotals,
  serializeSelection,
  type Selection,
} from "@/lib/tasks-selection";
import type {
  DeliveryListItem,
  TaskBrowseIdsResponse,
  TaskBrowseItem,
} from "@/lib/types";
import {
  browseKey,
  useTaskBrowse,
  useTaskBrowseCount,
} from "@/lib/use-task-browse";
import { cn } from "@/lib/utils";

type SelectionContextValue = {
  selection: Selection;
  isSelected: (id: string) => boolean;
  toggle: (task: TaskBrowseItem) => void;
  addTasks: (tasks: TaskBrowseItem[]) => void;
  addIds: (ids: string[]) => void;
  clear: () => void;
  removeIds: (ids: string[]) => void;
  deliveryId: string | null;
  delivery: DeliveryListItem | undefined;
  deliveryError: string | null;
};

const SelectionContext = createContext<SelectionContextValue | null>(null);

export function useSelection(): SelectionContextValue {
  const ctx = useContext(SelectionContext);
  if (!ctx) {
    throw new Error("useSelection must be used within a SelectionProvider");
  }
  return ctx;
}

function entryFor(task: TaskBrowseItem) {
  return {
    name: task.name,
    cost: task.cost_usd,
    estimated: task.cost_has_estimated && !task.cost_has_native,
  };
}

// The selection outlives the page: it is restored from localStorage (one
// entry per org) after mount and written back on every change, so paging,
// changing filters, reloading, or closing the tab never loses the picks.
// Hydration happens in an effect, not during render, so the server and the
// first client render agree (an empty selection).
export function SelectionProvider({ children }: { children: ReactNode }) {
  const { orgId, isLoaded } = useAuth();
  const deliveryId = useSearchParams().get("delivery");
  const storageKey =
    selectionStorageKey(orgId) + (deliveryId ? `.delivery.${deliveryId}` : "");
  return (
    <SelectionStateProvider
      key={storageKey}
      storageKey={storageKey}
      isLoaded={isLoaded}
      deliveryId={deliveryId}
    >
      {children}
    </SelectionStateProvider>
  );
}

function SelectionStateProvider({
  children,
  storageKey,
  isLoaded,
  deliveryId,
}: {
  children: ReactNode;
  storageKey: string;
  isLoaded: boolean;
  deliveryId: string | null;
}) {
  const { data: deliveries, error } = useSWR<DeliveryListItem[]>(
    deliveryId ? "/api/deliveries" : null,
    fetcher
  );
  const delivery = deliveries?.find((item) => item.id === deliveryId);
  const deliveryError = error
    ? "Could not load the delivery."
    : deliveries && !delivery
      ? "Delivery not found."
      : delivery && delivery.status !== "active"
        ? "This delivery is finalized."
        : null;
  const [selection, setSelection] = useState<Selection>(() => new Map());
  // React commits this marker with the restored selection. A ref would let
  // the persistence effect see "loaded" while still holding the empty render.
  const [hydratedKey, setHydratedKey] = useState<string | null>(null);

  useEffect(() => {
    if (!isLoaded) return;
    try {
      setSelection(
        parseStoredSelection(window.localStorage.getItem(storageKey))
      );
    } catch {
      setSelection(new Map());
    }
    setHydratedKey(storageKey);
  }, [isLoaded, storageKey]);

  useEffect(() => {
    if (!isLoaded || hydratedKey !== storageKey) return;
    try {
      if (selection.size === 0) window.localStorage.removeItem(storageKey);
      else
        window.localStorage.setItem(storageKey, serializeSelection(selection));
    } catch {
      // Private mode or a full store: the in-memory selection still works.
    }
  }, [selection, storageKey, hydratedKey, isLoaded]);

  const toggle = useCallback((task: TaskBrowseItem) => {
    if (task.active_deliveries?.some(d => d.delivery_id === deliveryId)) return;
    setSelection((prev) => {
      const next = new Map(prev);
      if (next.has(task.id)) next.delete(task.id);
      else if (next.size < SELECTION_LIMIT) next.set(task.id, entryFor(task));
      return next;
    });
  }, [deliveryId]);
  const addTasks = useCallback((tasks: TaskBrowseItem[]) => {
    setSelection((prev) => {
      const next = new Map(prev);
      for (const task of tasks) {
        if (task.active_deliveries?.some(d => d.delivery_id === deliveryId)) continue;
        if (next.size >= SELECTION_LIMIT) break;
        if (!next.has(task.id)) next.set(task.id, entryFor(task));
      }
      return next;
    });
  }, [deliveryId]);
  const addIds = useCallback((ids: string[]) => {
    setSelection((prev) => {
      const next = new Map(prev);
      for (const id of ids) {
        if (next.size >= SELECTION_LIMIT) break;
        if (!next.has(id)) next.set(id, { cost: null, estimated: false });
      }
      return next;
    });
  }, []);
  const clear = useCallback(() => setSelection(new Map()), []);
  const removeIds = useCallback(
    (ids: string[]) =>
      setSelection((prev) => {
        const next = new Map(prev);
        for (const id of ids) next.delete(id);
        return next;
      }),
    []
  );

  const value = useMemo<SelectionContextValue>(
    () => ({
      selection,
      isSelected: (id) => selection.has(id),
      toggle,
      addTasks,
      addIds,
      clear,
      removeIds,
      deliveryId,
      delivery,
      deliveryError,
    }),
    [
      selection,
      toggle,
      addTasks,
      addIds,
      clear,
      removeIds,
      deliveryId,
      delivery,
      deliveryError,
    ]
  );

  return (
    <SelectionContext.Provider value={value}>
      {children}
    </SelectionContext.Provider>
  );
}

export function TasksSelectionControls() {
  const { selection, addTasks, addIds, removeIds, deliveryId } = useSelection();
  const sp = new URLSearchParams(useSearchParams().toString());
  const { data: page, isLoading, error } = useTaskBrowse(sp);
  const { total, isStale } = useTaskBrowseCount(sp);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const items = (page?.items ?? []).filter(task => !task.active_deliveries?.some(record => record.delivery_id === deliveryId));
  const all = items.length > 0 && items.every((task) => selection.has(task.id));
  const selectAll = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const result = await fetcher<TaskBrowseIdsResponse>(browseKey(sp, "ids"));
      addIds(result.ids);
      if (
        result.truncated ||
        selection.size + result.ids.filter((id) => !selection.has(id)).length >
          SELECTION_LIMIT
      )
        setNotice(
          `Selection is limited to ${SELECTION_LIMIT.toLocaleString()} tasks. Narrow the filters to select a different set.`
        );
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not select tasks.");
    } finally {
      setBusy(false);
    }
  };
  if (!page?.items.length) return null;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            aria-label="Select this page"
            checked={all}
            disabled={!items.length || busy || isLoading || !!error}
            onChange={() =>
              all ? removeIds(items.map((t) => t.id)) : addTasks(items)
            }
          />
          Select page
        </label>
        <Button
          variant="ghost"
          size="sm"
          disabled={busy || isStale || !total}
          onClick={() => void selectAll()}
        >
          Select{" "}
          {total !== null && total > SELECTION_LIMIT
            ? `first ${SELECTION_LIMIT.toLocaleString()} matching tasks`
            : `all ${total?.toLocaleString() ?? ""} matching tasks`}
        </Button>
      </div>
      {notice ? (
        <p role="status" className="text-muted-foreground text-sm">
          {notice}
        </p>
      ) : null}
    </div>
  );
}

type Notice = { tone: "info" | "error"; text: ReactNode };

// Pinned above the results: select the page or the whole filter set, see
// what is ticked, and hand the picks to a delivery in one action. Only
// admins can create or fill deliveries (the API requires it), so everyone
// else gets the selection without the button.
export function SelectionBar() {
  const { selection, removeIds, clear, deliveryId, delivery, deliveryError } =
    useSelection();
  const { orgRole } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const sp = new URLSearchParams(searchParams.toString());
  // Same SWR keys as the grid and the header count: cached, never a second
  // fetch for the same filter state.
  const totals = selectionTotals(selection);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [targetId, setTargetId] = useState("");
  const { data: deliveries, error: deliveriesError, mutate: reloadDeliveries } = useSWR<DeliveryListItem[]>(
    menuOpen ? "/api/deliveries" : null,
    fetcher
  );
  const active = (deliveries ?? []).filter(d => d.status === "active" && (!sp.get("lab") || d.customer_name === sp.get("lab")));
  const target = active.find(d => d.id === targetId);

  const addToExisting = async (delivery: DeliveryListItem) => {
    setBusy(true);
    setNotice(null);
    const ids = Array.from(selection.keys());
    try {
      const { added } = await fetcher<{ added: number }>(
        `/api/deliveries/${encodeURIComponent(delivery.id)}/tasks`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ task_ids: ids }),
        }
      );
      removeIds(ids);
      setMenuOpen(false);
      if (deliveryId) {
        router.push(`/deliveries/${encodeURIComponent(delivery.id)}`);
        return;
      }
      setNotice({
        tone: "info",
        text: (
          <>
            Added {added} of {totals.count} to {delivery.name}
            {added < totals.count ? " (the rest were already on it)" : ""}.{" "}
            <Link
              href={`/deliveries/${encodeURIComponent(delivery.id)}`}
              className="font-medium underline underline-offset-2"
            >
              Open the board
            </Link>
          </>
        ),
      });
    } catch (err) {
      setNotice({
        tone: "error",
        text: err instanceof Error ? err.message : "Adding failed.",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2" data-testid="tasks-selection-bar">
      {totals.count > 0 ? (
        <div className="bg-muted/40 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border border-[#6f88b4]/20 px-3 py-2 text-xs">
          <span className="font-medium tabular-nums">
            {totals.count.toLocaleString()} selected
          </span>
          {totals.cost !== null ? (
            <span className="text-muted-foreground tabular-nums">
              {totals.anyEstimated ? "~" : ""}
              {formatCostUsd(totals.cost)}
            </span>
          ) : null}
          <Button variant="ghost" size="sm" onClick={() => setReviewOpen(true)}>
            Review selection
          </Button>
          <SavedFiltersMenu
            key={Array.from(selection.keys()).join(",")}
            selectedIds={Array.from(selection.keys())}
          />
          <Button variant="ghost" size="sm" disabled={busy} onClick={clear}>
            Clear selection
          </Button>
          {deliveryId && isOrgAdminRole(orgRole) ? (
            <Button
              className="ml-auto"
              size="sm"
              disabled={busy || !delivery || !!deliveryError}
              onClick={() => delivery && void addToExisting(delivery)}
            >
              Add {totals.count.toLocaleString()} to{" "}
              {delivery?.name ?? "delivery"}
            </Button>
          ) : null}
          {!deliveryId && isOrgAdminRole(orgRole) ? (
            <>
              <Button size="sm" className="ml-auto" disabled={busy} onClick={() => setMenuOpen(true)}><PackagePlus className="mr-2 h-4 w-4" />Add {totals.count.toLocaleString()} to delivery</Button>
              <Dialog open={menuOpen} onOpenChange={open => { if (!busy) setMenuOpen(open); }}>
                <DialogContent className="w-[calc(100%-2rem)] grid-cols-[minmax(0,1fr)]">
                  <DialogHeader>
                    <DialogTitle>Add {totals.count.toLocaleString()} to delivery</DialogTitle>
                  </DialogHeader>
                  <div className="min-w-0 space-y-2">
                    <Label htmlFor="delivery-batch">Delivery batch</Label>
                    {deliveriesError ? (
                      <p role="alert">
                        Could not load deliveries.
                        <Button variant="link" onClick={() => reloadDeliveries()}>Retry</Button>
                      </p>
                    ) : (
                      <Select value={target?.id ?? ""} onValueChange={setTargetId} disabled={busy || !deliveries || !active.length}>
                        <SelectTrigger id="delivery-batch" className="min-w-0 [&>span]:truncate [&>svg]:shrink-0">
                          <SelectValue placeholder={!deliveries ? "Loading deliveries…" : active.length ? "Choose a delivery" : "No active deliveries"} />
                        </SelectTrigger>
                        <SelectContent className="w-[var(--radix-select-trigger-width)]">
                          {active.map(d => (
                            <SelectItem key={d.id} value={d.id} className="[&>span:last-child]:min-w-0 [&>span:last-child]:break-words">
                              {d.name} · {d.customer_name ?? "No lab"} · {d.task_count} tasks
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  </div>
                  <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-between">
                    <Button variant="outline" disabled={busy} onClick={() => { setMenuOpen(false); setNewOpen(true); }}>New delivery…</Button>
                    <Button disabled={busy || !target || Boolean(deliveriesError)} onClick={() => { if (target) void addToExisting(target); }}>
                      Add {totals.count.toLocaleString()} tasks
                    </Button>
                  </div>
                  {notice?.tone === "error" ? <p role="alert" className="text-destructive break-words text-sm">{notice.text}</p> : null}
                </DialogContent>
              </Dialog>
            </>
          ) : null}
        </div>
      ) : null}
      {notice && !newOpen ? (
        <p
          role={notice.tone === "error" ? "alert" : "status"}
          className={cn(
            "text-xs",
            notice.tone === "error"
              ? "text-destructive"
              : "text-muted-foreground"
          )}
        >
          {notice.text}
        </p>
      ) : null}
      <Dialog open={reviewOpen} onOpenChange={setReviewOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {totals.count.toLocaleString()} selected tasks
            </DialogTitle>
            <DialogDescription>
              Selection across all pages and filters.
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-80 space-y-1 overflow-auto">
            {Array.from(selection, ([id, entry]) => (
              <div key={id} className="flex items-center justify-between gap-2">
                <Link
                  className="truncate text-sm underline"
                  href={`/tasks/${encodeURIComponent(id)}`}
                >
                  {entry.name ?? id}
                </Link>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busy}
                  onClick={() => removeIds([id])}
                >
                  Remove
                </Button>
              </div>
            ))}
          </div>
        </DialogContent>
      </Dialog>
      {newOpen ? <DeliveryCreateDialog
        taskIds={Array.from(selection.keys())}
        defaultCustomer={sp.get("lab") ?? ""}
        onClose={() => setNewOpen(false)}
        onCreated={(delivery, ids) => {
          removeIds(ids);
          setNewOpen(false);
          router.push(`/deliveries/${delivery.id}`);
        }}
      /> : null}
    </div>
  );
}
