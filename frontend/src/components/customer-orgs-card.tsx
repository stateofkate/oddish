"use client";

import { useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import type { CustomerOrg } from "@/lib/types";
import { useOrgHref } from "@/lib/use-org-href";
import { ACCESS_LABEL, send } from "@/components/customer-org-page";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const ORGS_KEY = "/api/admin/customer-orgs";

function CreateCustomerForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await send(ORGS_KEY, "POST", { name, domain });
      setName("");
      setDomain("");
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create org");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Name, e.g. Cursive"
          className="max-w-xs"
        />
        <Input
          value={domain}
          onChange={(e) => setDomain(e.target.value)}
          placeholder="Email domain, e.g. cursive.ai"
          className="max-w-xs"
        />
        <Button
          onClick={submit}
          disabled={busy || !name.trim() || !domain.trim()}
        >
          {busy ? "Creating..." : "Create customer org"}
        </Button>
      </div>
      {error && <p className="text-sm text-red-500">{error}</p>}
    </div>
  );
}

/** Admin > Customers: every org; each row opens its management page. */
export function CustomerOrgsCard() {
  const orgHref = useOrgHref();
  const { data, error, mutate } = useSWR<CustomerOrg[]>(ORGS_KEY, fetcher);
  const [filter, setFilter] = useState("");

  const needle = filter.trim().toLowerCase();
  const orgs = (data ?? []).filter(
    (org) =>
      !needle ||
      org.name.toLowerCase().includes(needle) ||
      org.slug.includes(needle)
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle>Customers</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <CreateCustomerForm onCreated={() => void mutate()} />
        <Input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter orgs"
          className="max-w-xs"
        />
        {error ? (
          <p className="text-sm text-red-500">Unable to load orgs.</p>
        ) : !data ? (
          <p className="text-muted-foreground text-sm">Loading...</p>
        ) : (
          <ul className="divide-y rounded-md border">
            {orgs.map((org) => (
              <li key={org.id}>
                <Link
                  href={orgHref(
                    `/admin/customers/${encodeURIComponent(org.id)}`
                  )}
                  className="hover:bg-muted/50 flex items-center gap-3 px-3 py-2"
                >
                  {org.image_url ? (
                    <img
                      src={org.image_url}
                      alt=""
                      className="h-6 w-6 rounded object-cover"
                    />
                  ) : (
                    <span className="bg-muted text-muted-foreground flex h-6 w-6 items-center justify-center rounded text-xs">
                      {org.name.slice(0, 1).toUpperCase()}
                    </span>
                  )}
                  <span className="min-w-0 flex-1 truncate font-medium">
                    {org.name}
                  </span>
                  <span className="text-muted-foreground text-xs">
                    {org.members_count != null &&
                      `${org.members_count} member${org.members_count === 1 ? "" : "s"} · `}
                    {org.share_count} shared
                  </span>
                  <Badge
                    variant={org.access === "none" ? "outline" : "secondary"}
                  >
                    {ACCESS_LABEL[org.access]}
                  </Badge>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
