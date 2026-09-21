"use client";

import { useId, useMemo, useState } from "react";
import useSWR from "swr";
import { Copy, Eye, EyeOff, Loader2 } from "lucide-react";
import { apiFetch, fetcher } from "@/lib/api";
import type { ExperimentShareInfo } from "@/lib/types";
import { encodeExperimentRouteParam } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

export function ExperimentShareButton({
  experimentId,
  canManageShare = true,
}: {
  experimentId: string;
  canManageShare?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [isUpdating, setIsUpdating] = useState(false);
  const [showQaDraft, setShowQaDraft] = useState<boolean | null>(null);
  const showQaId = useId();
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [statusTone, setStatusTone] = useState<"success" | "error" | null>(
    null,
  );

  const encodedId = encodeExperimentRouteParam(experimentId);
  const shareKey = `/api/experiments/${encodedId}/share`;
  // Share metadata is only needed when the publish dialog is opened.
  const { data, error: shareError, mutate } = useSWR<ExperimentShareInfo>(
    isOpen ? shareKey : null,
    fetcher,
  );
  const showQa = showQaDraft ?? data?.show_qa ?? false;
  const hasChanges = showQa !== (data?.show_qa ?? false);

  const shareUrl = useMemo(() => {
    if (!data?.public_token || typeof window === "undefined") return null;
    return `${window.location.origin}/share/${data.public_token}`;
  }, [data?.public_token]);

  const handlePublish = async () => {
    if (!canManageShare || !data) return;
    const wasPublic = data.is_public;
    setIsUpdating(true);
    setStatusMessage(null);
    try {
      const res = await apiFetch(`/api/experiments/${encodedId}/publish`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ show_qa: showQa }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(payload.detail || payload.error || "Failed to publish");
      }
      await mutate(payload, false);
      setShowQaDraft(null);
      setStatusTone("success");
      setStatusMessage(
        wasPublic ? "Share settings saved." : "Public link is active."
      );
      if (!wasPublic && payload.public_token) {
        const url = `${window.location.origin}/share/${payload.public_token}`;
        try {
          await navigator.clipboard.writeText(url);
          setStatusMessage("Public link copied to clipboard.");
        } catch {
          setStatusMessage("Public link is active. Use Copy link to copy it.");
        }
      }
    } catch (error) {
      setStatusTone("error");
      setStatusMessage(
        error instanceof Error
          ? error.message
          : "Unable to publish experiment.",
      );
    } finally {
      setIsUpdating(false);
    }
  };

  const handleUnpublish = async () => {
    if (!canManageShare) return;
    setIsUpdating(true);
    setStatusMessage(null);
    try {
      const res = await apiFetch(`/api/experiments/${encodedId}/unpublish`, {
        method: "POST",
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(
          payload.detail || payload.error || "Failed to unpublish",
        );
      }
      await mutate(payload, false);
      setShowQaDraft(null);
      setStatusTone("success");
      setStatusMessage("Public link disabled.");
    } catch (error) {
      setStatusTone("error");
      setStatusMessage(
        error instanceof Error
          ? error.message
          : "Unable to unpublish experiment.",
      );
    } finally {
      setIsUpdating(false);
    }
  };

  const handleCopy = async () => {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setStatusTone("success");
      setStatusMessage("Link copied to clipboard.");
    } catch {
      setStatusTone("error");
      setStatusMessage("Failed to copy link.");
    }
  };

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        setIsOpen(open);
        if (open) {
          setShowQaDraft(null);
          setStatusMessage(null);
        }
      }}
      modal={false}
    >
      <DialogTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          title={
            canManageShare
              ? "Publish experiment"
              : "Only org admins can publish experiments"
          }
          disabled={!canManageShare}
          className="h-8 select-none gap-[7px] rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 text-[12px] leading-none text-[color:var(--paper-ink)] transition-colors hover:border-[color:var(--paper-ink-4)] hover:bg-[color:var(--paper-surface-2)] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-[color:var(--paper-line)] disabled:hover:bg-[color:var(--paper-surface)]"
        >
          <svg
            width="13"
            height="13"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8" />
            <polyline points="16 6 12 2 8 6" />
            <line x1="12" x2="12" y1="2" y2="15" />
          </svg>
          {data?.is_public ? "Public" : "Publish"}
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Publish experiment</DialogTitle>
          <DialogDescription>
            Anyone with the link can view tasks, trials, logs, and files.
          </DialogDescription>
        </DialogHeader>

        {shareError ? (
          <p role="alert" className="py-2 text-sm text-red-500">
            Unable to load share settings. Close this window and try again.
          </p>
        ) : !data ? (
          <p role="status" className="text-muted-foreground py-2 text-sm">
            Loading share settings...
          </p>
        ) : data.is_public && shareUrl ? (
          <div className="space-y-4 py-2">
            <div className="flex items-center gap-2 text-xs text-emerald-600">
              <Eye className="h-4 w-4" />
              Public link is active
            </div>
            <div className="space-y-2">
              <Label className="text-muted-foreground">Share link</Label>
              <div className="flex items-center gap-2">
                <Input
                  value={shareUrl}
                  readOnly
                  className="font-mono text-xs"
                />
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  onClick={handleCopy}
                  title="Copy link"
                >
                  <Copy className="h-4 w-4" />
                </Button>
              </div>
            </div>
            <div className="text-muted-foreground text-xs">
              This view is read-only for anyone with the link.
            </div>
          </div>
        ) : (
          <div className="text-muted-foreground space-y-3 py-2 text-sm">
            <div className="flex items-center gap-2">
              <EyeOff className="h-4 w-4" />
              Not public yet
            </div>
            <div>Publish to generate a shareable read-only link.</div>
          </div>
        )}

        {data && !shareError && (
          <div className="flex items-center justify-between gap-4 rounded-lg border p-3">
            <div className="space-y-1">
              <Label htmlFor={showQaId}>Show QA</Label>
              <p
                id={`${showQaId}-description`}
                className="text-muted-foreground text-xs"
              >
                Let anyone with the link view QA checks, findings, and trial
                reviews.
              </p>
            </div>
            <button
              id={showQaId}
              type="button"
              role="switch"
              aria-checked={showQa}
              aria-describedby={`${showQaId}-description`}
              disabled={isUpdating || !canManageShare}
              onClick={() => {
                setShowQaDraft(!showQa);
                setStatusMessage(null);
              }}
              className={`focus-visible:ring-ring inline-flex h-6 w-11 shrink-0 items-center rounded-full border-2 border-transparent transition-colors focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50 ${showQa ? "bg-primary" : "bg-input"}`}
            >
              <span
                className={`bg-background pointer-events-none block h-5 w-5 rounded-full shadow-sm transition-transform ${showQa ? "translate-x-5" : "translate-x-0"}`}
              />
            </button>
          </div>
        )}

        {statusMessage && (
          <div
            role="status"
            className={`text-xs ${
              statusTone === "success" ? "text-emerald-600" : "text-red-500"
            }`}
          >
            {statusMessage}
          </div>
        )}

        <DialogFooter>
          {data?.is_public ? (
            <Button
              type="button"
              variant="outline"
              onClick={handleUnpublish}
              disabled={isUpdating || !!shareError}
            >
              {isUpdating ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Updating...
                </>
              ) : (
                "Unpublish"
              )}
            </Button>
          ) : (
            <Button
              type="button"
              onClick={handlePublish}
              disabled={isUpdating || !data || !!shareError}
            >
              {isUpdating ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Publishing...
                </>
              ) : (
                "Publish"
              )}
            </Button>
          )}
          {data?.is_public && hasChanges && (
            <Button
              type="button"
              onClick={handlePublish}
              disabled={isUpdating || !!shareError}
            >
              {isUpdating ? "Saving..." : "Save changes"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
