"use client";

import { useState } from "react";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { Share2, X } from "lucide-react";
import { apiFetch, fetcher } from "@/lib/api";
import type { OrgShare, ShareResourceType, ShareableOrg } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

function orgSharesKey(type: ShareResourceType, id: string): string {
  return `/api/org-shares?${new URLSearchParams({ resource_type: type, resource_id: id })}`;
}

/** The customer orgs this caller may share with; an error outside the operator org. */
function useShareableOrgs() {
  // Keyed by org so switching orgs never shows the last org's answer.
  const { orgId } = useAuth();
  return useSWR<ShareableOrg[]>(
    orgId ? `/api/org-shares/orgs?org=${encodeURIComponent(orgId)}` : null,
    fetcher,
    { shouldRetryOnError: false, revalidateOnFocus: false }
  );
}

type ShareFlags = { show_qa: boolean; show_costs: boolean };

/** What one share lets its grantee see: QA (on by default) and costs (off). */
export function ShareToggles({
  share,
  disabled,
  onChange,
}: {
  share: ShareFlags;
  disabled: boolean;
  onChange: (patch: Partial<ShareFlags>) => void;
}) {
  const toggle = (key: keyof ShareFlags, label: string) => (
    <label className="text-muted-foreground flex items-center gap-1.5 text-xs">
      <Checkbox
        checked={share[key]}
        disabled={disabled}
        onCheckedChange={(checked) => onChange({ [key]: checked === true })}
      />
      {label}
    </label>
  );
  return (
    <>
      {toggle("show_qa", "Show QA")}
      {toggle("show_costs", "Show costs")}
    </>
  );
}

/**
 * Operator-only: share an experiment, task, or delivery with customer orgs.
 * Renders nothing for everyone else (the org list answers 403 outside the
 * operator org).
 */
export function OrgSharePicker({
  resourceType = "experiment",
  resourceId,
  onChange,
}: {
  resourceType?: ShareResourceType;
  resourceId: string;
  onChange?: () => void;
}) {
  const { data: orgs, error: orgsError } = useShareableOrgs();
  const { data: shares, mutate } = useSWR<OrgShare[]>(
    orgs ? orgSharesKey(resourceType, resourceId) : null,
    fetcher
  );
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // No customer orgs means nothing to share with.
  if (orgsError || !orgs?.length) return null;
  const sharedIds = new Set(shares?.map((s) => s.grantee_org_id));
  const available = orgs.filter((o) => !sharedIds.has(o.id));

  const run = async (url: string, init: RequestInit, done: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await apiFetch(url, init);
      const payload = await res.json().catch(() => null);
      if (!res.ok) {
        throw new Error(payload?.detail || payload?.error || "Request failed");
      }
      // Share and revoke answer with the list; a toggle answers with one share.
      await (Array.isArray(payload)
        ? mutate(payload as OrgShare[], false)
        : mutate());
      setSelected("");
      setNotice(done);
      onChange?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2 rounded-lg border p-3">
      <Label htmlFor="org-share-select">Share with orgs</Label>
      {shares && shares.length > 0 && (
        <ul className="space-y-1 text-sm">
          {shares.map((share) => (
            <li key={share.id} className="flex items-center gap-3">
              <span className="min-w-0 flex-1 truncate">
                {share.grantee_org_name}
              </span>
              <ShareToggles
                share={share}
                disabled={busy}
                onChange={(patch) =>
                  run(
                    `/api/org-shares/${encodeURIComponent(share.id)}`,
                    {
                      method: "PATCH",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify(patch),
                    },
                    `Updated ${share.grantee_org_name}`
                  )
                }
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-6 w-6"
                title={`Stop sharing with ${share.grantee_org_name}`}
                disabled={busy}
                onClick={() =>
                  confirm(`Stop sharing with ${share.grantee_org_name}?`) &&
                  run(
                    `/api/org-shares/${encodeURIComponent(share.id)}`,
                    { method: "DELETE" },
                    `Stopped sharing with ${share.grantee_org_name}`
                  )
                }
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      {available.length === 0 ? (
        <p className="text-muted-foreground text-sm">
          Shared with every customer org.
        </p>
      ) : (
        <div className="flex items-center gap-2">
          <select
            id="org-share-select"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            disabled={busy}
            className="border-input bg-background h-8 flex-1 rounded-md border px-2 text-sm"
          >
            <option value="">Choose an org</option>
            {available.map((org) => (
              <option key={org.id} value={org.id}>
                {org.name}
              </option>
            ))}
          </select>
          <Button
            type="button"
            size="sm"
            disabled={busy || !selected}
            onClick={() =>
              run(
                "/api/org-shares",
                {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    grantee_org_id: selected,
                    resource_type: resourceType,
                    resource_id: resourceId,
                  }),
                },
                `Shared with ${orgs.find((o) => o.id === selected)?.name ?? "the org"}`
              )
            }
          >
            Share
          </Button>
        </div>
      )}
      {notice && (
        <p role="status" className="text-xs text-green-600">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="text-xs text-red-500">
          {error}
        </p>
      )}
    </div>
  );
}

/** A Share button that opens the org picker; hidden outside the operator org. */
export function OrgShareButton({
  resourceType,
  resourceId,
}: {
  resourceType: ShareResourceType;
  resourceId: string;
}) {
  const { data: orgs, error } = useShareableOrgs();
  if (!orgs?.length || error) return null;
  return (
    <Dialog modal={false}>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" size="sm">
          <Share2 className="mr-1 h-4 w-4" />
          Share
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Share {resourceType}</DialogTitle>
        </DialogHeader>
        <OrgSharePicker resourceType={resourceType} resourceId={resourceId} />
      </DialogContent>
    </Dialog>
  );
}
