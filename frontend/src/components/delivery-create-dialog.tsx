"use client";

import { useState } from "react";
import { CustomerPicker } from "@/components/customer-picker";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { fetcher } from "@/lib/api";

// Mount only while open: each new draft starts empty, while errors preserve it.
export function DeliveryCreateDialog({
  taskIds = [],
  defaultCustomer = "",
  onClose,
  onCreated,
}: {
  taskIds?: string[];
  defaultCustomer?: string;
  onClose: () => void;
  onCreated: (delivery: { id: string }, taskIds: string[]) => void;
}) {
  const [name, setName] = useState("");
  const [customerId, setCustomerId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function create() {
    setBusy(true);
    setError(null);
    const ids = [...taskIds];
    try {
      const delivery = await fetcher<{ id: string }>("/api/deliveries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          customer: customerId,
          task_ids: ids,
        }),
      });
      onCreated(delivery, ids);
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Could not create delivery."
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New delivery</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="delivery-name">Name</Label>
            <Input
              id="delivery-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              disabled={busy}
              placeholder="September batch"
            />
          </div>
          <CustomerPicker
            value={customerId}
            onValueChange={setCustomerId}
            defaultName={defaultCustomer}
          />
          {error ? (
            <p role="alert" className="text-destructive text-sm">
              {error}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={onClose}
            >
              Cancel
            </Button>
            <Button
              type="button"
              onClick={() => void create()}
              disabled={busy || !name.trim() || !customerId}
            >
              {busy
                ? "Creating…"
                : taskIds.length
                  ? `Create and add ${taskIds.length.toLocaleString()}`
                  : "Create delivery"}
            </Button>
          </DialogFooter>
        </div>
      </DialogContent>
    </Dialog>
  );
}
