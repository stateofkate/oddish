"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { ArrowLeft, X } from "lucide-react";
import { apiFetch, fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import type {
  CustomerDomain,
  CustomerInvitation,
  CustomerMember,
  CustomerOrg,
  CustomerOrgDetail,
  CustomerRole,
  CustomerShare,
  ExperimentOptionsResponse,
  SharedExperiment,
} from "@/lib/types";
import { useOrgHref } from "@/lib/use-org-href";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";

export const ACCESS_LABEL: Record<CustomerOrg["access"], string> = {
  viewer: "View-only",
  execution: "Runs jobs",
  none: "No access",
};

/** POST/PUT/PATCH/DELETE a JSON body; throws the backend's message on failure. */
export async function send(url: string, method: string, body?: unknown) {
  const res = await apiFetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = payload?.detail ?? payload?.error;
    throw new Error(
      typeof detail === "string" ? detail : `Request failed (${res.status})`
    );
  }
  return payload;
}

/** Run a request, then refresh; returns the error text to show, if any. */
function useAction() {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async (action: () => Promise<unknown>, after?: () => void) => {
    setError(null);
    setBusy(true);
    try {
      await action();
      after?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };
  return { error, busy, run };
}

function ErrorText({ error }: { error: string | null }) {
  return error ? <p className="text-sm text-red-500">{error}</p> : null;
}

function OrgLogo({ org, size }: { org: CustomerOrg; size: string }) {
  return org.image_url ? (
    <img
      src={org.image_url}
      alt=""
      className={cn("rounded object-cover", size)}
    />
  ) : (
    <span
      className={cn(
        "bg-muted text-muted-foreground flex items-center justify-center rounded text-xs",
        size
      )}
    >
      {org.name.slice(0, 1).toUpperCase()}
    </span>
  );
}

function formatDate(ms: number | null) {
  return ms
    ? new Date(ms).toLocaleDateString(undefined, { dateStyle: "medium" })
    : "";
}

function Subsection({
  title,
  children,
  className,
}: {
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("space-y-3", className)}>
      <h3 className="text-sm font-medium">{title}</h3>
      {children}
    </section>
  );
}

const ROLE_LABEL: Record<string, string> = {
  "org:admin": "Admin",
  "org:member": "Member",
};

function RoleSelect({
  value,
  onChange,
  className,
}: {
  value: string;
  onChange: (role: CustomerRole) => void;
  className?: string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value as CustomerRole)}
      className={cn(
        "border-input bg-background h-8 rounded-md border px-2 text-sm",
        className
      )}
    >
      <option value="org:member">Member</option>
      <option value="org:admin">Admin</option>
    </select>
  );
}

function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function GeneralSection({
  org,
  base,
  onChange,
}: {
  org: CustomerOrgDetail;
  base: string;
  onChange: () => void;
}) {
  const router = useRouter();
  const orgHref = useOrgHref();
  const [name, setName] = useState(org.name);
  const [slug, setSlug] = useState(org.slug);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const { error, busy, run } = useAction();
  useEffect(() => {
    setName(org.name);
    setSlug(org.slug);
  }, [org.name, org.slug]);

  return (
    <div className="space-y-8">
      <Subsection title="Name">
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-muted-foreground space-y-1 text-xs">
            Display name
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="text-foreground w-64"
            />
          </label>
          <label className="text-muted-foreground space-y-1 text-xs">
            URL slug
            <Input
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              className="text-foreground w-64 font-mono"
            />
          </label>
          <Button
            disabled={busy || (name === org.name && slug === org.slug)}
            onClick={() =>
              run(
                () =>
                  send(base, "PATCH", {
                    name: name === org.name ? null : name,
                    slug: slug === org.slug ? null : slug,
                  }),
                onChange
              )
            }
          >
            Save
          </Button>
        </div>
      </Subsection>
      <Subsection title="Logo">
        <div className="flex items-center gap-3">
          <OrgLogo org={org} size="h-12 w-12" />
          <label className="border-input hover:bg-muted flex h-10 cursor-pointer items-center rounded-md border px-4 text-sm">
            Upload logo
            <input
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (file)
                  void run(
                    async () =>
                      send(`${base}/logo`, "POST", {
                        filename: file.name,
                        content_type: file.type,
                        data_base64: await readBase64(file),
                      }),
                    onChange
                  );
              }}
            />
          </label>
        </div>
      </Subsection>
      <Subsection title="Delete organization" className="border-t pt-6">
        {confirmDelete ? (
          <div className="flex gap-2">
            <Button
              variant="destructive"
              disabled={busy}
              onClick={() =>
                run(
                  () => send(base, "DELETE"),
                  () => router.push(orgHref("/admin?tab=customers"))
                )
              }
            >
              Delete {org.name} and remove all its members
            </Button>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
          </div>
        ) : (
          <Button
            variant="outline"
            className="text-red-500"
            onClick={() => setConfirmDelete(true)}
          >
            Delete organization
          </Button>
        )}
      </Subsection>
      <ErrorText error={error} />
    </div>
  );
}

function MembersSection({ base }: { base: string }) {
  const members = useSWR<CustomerMember[]>(`${base}/members`, fetcher);
  const invitations = useSWR<CustomerInvitation[]>(
    `${base}/invitations`,
    fetcher
  );
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<CustomerRole>("org:member");
  const { error, busy, run } = useAction();
  const refresh = () => {
    void members.mutate();
    void invitations.mutate();
  };
  const memberUrl = (userId: string) =>
    `${base}/members/${encodeURIComponent(userId)}`;

  return (
    <div className="space-y-8">
      <Subsection title="People">
        {members.error ? (
          <ErrorText error="Unable to load members from Clerk." />
        ) : !members.data ? (
          <p className="text-muted-foreground text-sm">Loading...</p>
        ) : (
          <ul className="divide-y rounded-md border">
            {members.data.map((member) => (
              <li
                key={member.user_id}
                className="flex items-center gap-3 px-3 py-2 text-sm"
              >
                {member.image_url ? (
                  <img
                    src={member.image_url}
                    alt=""
                    className="h-8 w-8 rounded-full"
                  />
                ) : (
                  <span className="bg-muted text-muted-foreground flex h-8 w-8 items-center justify-center rounded-full text-xs">
                    {(member.name ?? member.email ?? "?")
                      .slice(0, 1)
                      .toUpperCase()}
                  </span>
                )}
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">
                    {member.name ?? member.email}
                  </span>
                  {member.name && (
                    <span className="text-muted-foreground block truncate text-xs">
                      {member.email}
                    </span>
                  )}
                </span>
                <span className="text-muted-foreground hidden text-xs sm:block">
                  Joined {formatDate(member.joined_at)}
                </span>
                <RoleSelect
                  value={member.role}
                  onChange={(next) =>
                    run(
                      () =>
                        send(memberUrl(member.user_id), "PATCH", {
                          role: next,
                        }),
                      refresh
                    )
                  }
                />
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8"
                  title="Remove from organization"
                  disabled={busy}
                  onClick={() =>
                    confirm(`Remove ${member.email} from this organization?`) &&
                    run(
                      () => send(memberUrl(member.user_id), "DELETE"),
                      refresh
                    )
                  }
                >
                  <X className="h-4 w-4" />
                </Button>
              </li>
            ))}
            {invitations.data?.map((invite) => (
              <li
                key={invite.id}
                className="flex items-center gap-3 px-3 py-2 text-sm"
              >
                <span className="h-8 w-8 rounded-full border border-dashed" />
                <span className="min-w-0 flex-1 truncate">{invite.email}</span>
                <span className="text-muted-foreground text-xs">
                  Invited as {ROLE_LABEL[invite.role] ?? invite.role}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8"
                  disabled={busy}
                  onClick={() =>
                    confirm(`Revoke the invitation to ${invite.email}?`) &&
                    run(
                      () =>
                        send(
                          `${base}/invitations/${encodeURIComponent(invite.id)}`,
                          "DELETE"
                        ),
                      refresh
                    )
                  }
                >
                  Revoke
                </Button>
              </li>
            ))}
            {members.data.length === 0 && !invitations.data?.length && (
              <li className="text-muted-foreground px-3 py-2 text-sm">
                No members yet.
              </li>
            )}
          </ul>
        )}
        <div className="flex flex-wrap gap-2">
          <Input
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="name@company.com"
            className="max-w-xs"
          />
          <RoleSelect value={role} onChange={setRole} className="h-10" />
          <Button
            disabled={busy || !email.trim()}
            onClick={() =>
              run(
                () => send(`${base}/invitations`, "POST", { email, role }),
                () => {
                  setEmail("");
                  refresh();
                }
              )
            }
          >
            Invite
          </Button>
        </div>
        <ErrorText error={error} />
      </Subsection>
      <DomainsSection base={base} />
    </div>
  );
}

function DomainsSection({ base }: { base: string }) {
  const {
    data,
    error: loadError,
    mutate,
  } = useSWR<CustomerDomain[]>(`${base}/domains`, fetcher);
  const [domain, setDomain] = useState("");
  const { error, busy, run } = useAction();

  return (
    <Subsection title="Auto-join domains">
      {loadError ? (
        <ErrorText error="Unable to load domains from Clerk." />
      ) : !data ? (
        <p className="text-muted-foreground text-sm">Loading...</p>
      ) : (
        data.length > 0 && (
          <ul className="divide-y rounded-md border text-sm">
            {data.map((row) => (
              <li key={row.id} className="flex items-center gap-3 px-3 py-2">
                <span className="flex-1 font-mono">{row.name}</span>
                <span className="text-muted-foreground text-xs">
                  {row.enrollment_mode.replaceAll("_", " ")}
                </span>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8"
                  title="Remove domain"
                  disabled={busy}
                  onClick={() =>
                    confirm(`Remove ${row.name}?`) &&
                    run(
                      () =>
                        send(
                          `${base}/domains/${encodeURIComponent(row.id)}`,
                          "DELETE"
                        ),
                      () => void mutate()
                    )
                  }
                >
                  <X className="h-4 w-4" />
                </Button>
              </li>
            ))}
          </ul>
        )
      )}
      <div className="flex gap-2">
        <Input
          value={domain}
          onChange={(e) => setDomain(e.target.value)}
          placeholder="company.com"
          className="max-w-xs"
        />
        <Button
          disabled={busy || !domain.trim()}
          onClick={() =>
            run(
              () => send(`${base}/domains`, "POST", { domain }),
              () => {
                setDomain("");
                void mutate();
              }
            )
          }
        >
          Add domain
        </Button>
      </div>
      <ErrorText error={error} />
    </Subsection>
  );
}

function AccessSection({
  org,
  base,
  onChange,
}: {
  org: CustomerOrgDetail;
  base: string;
  onChange: () => void;
}) {
  const savedBudget =
    org.monthly_limit_usd != null ? String(org.monthly_limit_usd) : "";
  const [access, setAccess] = useState(org.access);
  const [budget, setBudget] = useState(savedBudget);
  const { error, busy, run } = useAction();
  useEffect(() => setAccess(org.access), [org.access]);
  useEffect(() => setBudget(savedBudget), [savedBudget]);

  const options: { value: CustomerOrg["access"]; detail: string }[] = [
    { value: "viewer", detail: "Reads only what is shared with it." },
    {
      value: "execution",
      detail: "Runs its own jobs within a monthly budget.",
    },
    { value: "none", detail: "Cannot sign in. Its running jobs stop." },
  ];
  const unchanged =
    access === org.access && (access !== "execution" || budget === savedBudget);

  return (
    <div className="space-y-8">
      <Subsection title="Level">
        <div className="grid gap-2 sm:grid-cols-3">
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => setAccess(option.value)}
              className={cn(
                "rounded-md border p-3 text-left text-sm",
                access === option.value
                  ? "border-foreground bg-muted"
                  : "hover:bg-muted/50"
              )}
            >
              <span className="block font-medium">
                {ACCESS_LABEL[option.value]}
              </span>
              <span className="text-muted-foreground text-xs">
                {option.detail}
              </span>
            </button>
          ))}
        </div>
      </Subsection>
      {access === "execution" && (
        <Subsection title="Monthly budget">
          <div className="flex items-center gap-3">
            <div className="relative w-40">
              <span className="text-muted-foreground absolute top-1/2 left-3 -translate-y-1/2 text-sm">
                $
              </span>
              <Input
                type="number"
                min="1"
                value={budget}
                onChange={(e) => setBudget(e.target.value)}
                placeholder="Required"
                className="pl-7"
              />
            </div>
            <span className="text-muted-foreground text-sm">
              {formatCostUsd(org.month_spend_usd)} spent this month
            </span>
          </div>
          {org.access === "execution" && org.monthly_limit_usd == null && (
            <p className="text-sm text-amber-500">
              No budget set, so this org's spending is uncapped.
            </p>
          )}
        </Subsection>
      )}
      <div className="space-y-2">
        <Button
          disabled={
            busy ||
            unchanged ||
            (access === "execution" && !(Number(budget) > 0))
          }
          onClick={() =>
            run(
              () =>
                send(`${base}/access`, "PUT", {
                  access,
                  monthly_limit_usd:
                    access === "execution" ? Number(budget) || null : null,
                }),
              onChange
            )
          }
        >
          Save
        </Button>
        <ErrorText error={error} />
      </div>
    </div>
  );
}

const INACTIVE_LABEL = {
  experiment: "experiment deleted",
  task: "task deleted",
  delivery: "not finalized",
} as const;

function SharingSection({
  org,
  base,
  onChange,
}: {
  org: CustomerOrgDetail;
  base: string;
  onChange: () => void;
}) {
  const orgHref = useOrgHref();
  const shares = useSWR<CustomerShare[]>(`${base}/shares`, fetcher);
  const experiments = useSWR<SharedExperiment[]>(
    `${base}/experiments`,
    fetcher
  );
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(search.trim()), 300);
    return () => window.clearTimeout(handle);
  }, [search]);
  const options = useSWR<ExperimentOptionsResponse>(
    debounced
      ? `/api/tasks/browse/experiment-options?limit=8&query=${encodeURIComponent(debounced)}`
      : null,
    fetcher,
    { keepPreviousData: true }
  );
  const { error, busy, run } = useAction();
  const refresh = () => {
    void shares.mutate();
    void experiments.mutate();
    onChange();
  };

  const shareByResource = new Map(
    (shares.data ?? []).map((share) => [share.resource_id, share])
  );
  const share = (resourceType: "experiment", resourceId: string) =>
    run(
      () =>
        send("/api/org-shares", "POST", {
          grantee_org_id: org.id,
          resource_type: resourceType,
          resource_id: resourceId,
        }),
      refresh
    );
  const unshare = (shareId: string, label: string) =>
    confirm(`Stop sharing ${label} with ${org.name}?`) &&
    run(
      () => send(`/api/org-shares/${encodeURIComponent(shareId)}`, "DELETE"),
      refresh
    );
  const costsToggle = (row: CustomerShare) => (
    <label className="text-muted-foreground flex items-center gap-1.5 text-xs">
      <Checkbox
        checked={row.show_costs}
        disabled={busy}
        onCheckedChange={(checked) =>
          run(
            () =>
              send(`/api/org-shares/${encodeURIComponent(row.id)}`, "PATCH", {
                show_costs: checked === true,
              }),
            refresh
          )
        }
      />
      Show costs
    </label>
  );

  const linkCustomer = (customerId: string, linked: boolean) =>
    run(
      () =>
        send(
          `${base}/delivery-customers/${encodeURIComponent(customerId)}`,
          "PUT",
          { linked }
        ),
      refresh
    );
  // Customers linked here, plus ones no other org has claimed.
  const deliveryCustomers = org.delivery_customers.filter(
    (customer) =>
      customer.customer_org_id === null || customer.customer_org_id === org.id
  );

  return (
    <div className="space-y-8">
      <label className="flex items-center gap-2 text-sm">
        <Checkbox
          checked={org.show_shared_qa}
          disabled={busy}
          onCheckedChange={(checked) =>
            run(
              () =>
                send(`${base}/shared-qa`, "PUT", { show: checked === true }),
              refresh
            )
          }
        />
        Show QA on shared samples
      </label>

      <Subsection title="Delivery customers">
        <ul className="divide-y rounded-md border text-sm">
          {deliveryCustomers.map((customer) => (
            <li key={customer.id} className="flex items-center gap-3 px-3 py-2">
              <Checkbox
                checked={customer.customer_org_id === org.id}
                disabled={busy}
                onCheckedChange={(checked) =>
                  linkCustomer(customer.id, checked === true)
                }
              />
              <span className="min-w-0 flex-1 truncate">{customer.name}</span>
            </li>
          ))}
          {deliveryCustomers.length === 0 && (
            <li className="text-muted-foreground px-3 py-2">
              No delivery customers.
            </li>
          )}
        </ul>
      </Subsection>

      <Subsection title="Shared">
        {(shares.data ?? []).length > 0 && (
          <ul className="divide-y rounded-md border text-sm">
            {(shares.data ?? []).map((row) => (
              <li key={row.id} className="flex items-center gap-3 px-3 py-2">
                <span className="min-w-0 flex-1 truncate">{row.label}</span>
                <span className="text-muted-foreground text-xs">
                  {row.resource_type}
                </span>
                {row.inactive ? (
                  <Badge variant="destructive">
                    {INACTIVE_LABEL[row.resource_type]}
                  </Badge>
                ) : (
                  costsToggle(row)
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8"
                  title="Stop sharing"
                  disabled={busy}
                  onClick={() => unshare(row.id, row.label)}
                >
                  <X className="h-4 w-4" />
                </Button>
              </li>
            ))}
          </ul>
        )}
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search experiments to share"
          className="max-w-md"
        />
        {debounced && options.data && (
          <ul className="max-w-md divide-y rounded-md border text-sm">
            {options.data.items.length === 0 && (
              <li className="text-muted-foreground px-3 py-2">No matches</li>
            )}
            {options.data.items.map((option) => (
              <li
                key={option.id}
                className="flex items-center justify-between gap-3 px-3 py-2"
              >
                <span className="truncate">{option.name}</span>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy || shareByResource.has(option.id)}
                  onClick={() => share("experiment", option.id)}
                >
                  {shareByResource.has(option.id) ? "Shared" : "Share"}
                </Button>
              </li>
            ))}
          </ul>
        )}
      </Subsection>

      <Subsection
        title={`What ${org.name} sees (${experiments.data?.length ?? 0})`}
        className="border-t pt-6"
      >
        {experiments.data?.length === 0 ? (
          <p className="text-muted-foreground text-sm">Nothing shared yet.</p>
        ) : (
          <ul className="divide-y rounded-md border text-sm">
            {experiments.data?.map((experiment) => (
              <li key={experiment.id}>
                <Link
                  href={orgHref(`/shared/${encodeURIComponent(experiment.id)}`)}
                  className="hover:bg-muted/50 block px-3 py-2"
                >
                  {experiment.name}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Subsection>
      <ErrorText
        error={
          error ??
          (shares.error || experiments.error
            ? "Unable to load shared content."
            : null)
        }
      />
    </div>
  );
}

const SECTIONS = [
  { id: "general", label: "General" },
  { id: "members", label: "Members" },
  { id: "access", label: "Access" },
  { id: "sharing", label: "Shared content" },
] as const;

type Section = (typeof SECTIONS)[number]["id"];

/** Admin > Customers > one org: everything about it, managed from Oddish. */
export function CustomerOrgPage({ orgId }: { orgId: string }) {
  const orgHref = useOrgHref();
  const base = `/api/admin/customer-orgs/${encodeURIComponent(orgId)}`;
  const { data: org, error, mutate } = useSWR<CustomerOrgDetail>(base, fetcher);
  const [section, setSection] = useState<Section>("general");
  const refresh = () => void mutate();

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6 py-2">
      <Link
        href={orgHref("/admin?tab=customers")}
        className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1.5 text-sm"
      >
        <ArrowLeft className="h-4 w-4" />
        Customers
      </Link>
      {error ? (
        <ErrorText error="Unable to load this organization." />
      ) : !org ? (
        <p className="text-muted-foreground text-sm">Loading...</p>
      ) : (
        <>
          <div className="flex items-center gap-3">
            <OrgLogo org={org} size="h-10 w-10" />
            <h1 className="text-2xl font-semibold">{org.name}</h1>
            <Badge variant="secondary">{ACCESS_LABEL[org.access]}</Badge>
          </div>
          <div className="grid gap-6 lg:grid-cols-[180px_minmax(0,1fr)]">
            <nav className="flex gap-1 overflow-x-auto lg:flex-col">
              {SECTIONS.map((entry) => (
                <button
                  key={entry.id}
                  type="button"
                  onClick={() => setSection(entry.id)}
                  className={cn(
                    "rounded-md px-3 py-2 text-left text-sm whitespace-nowrap",
                    section === entry.id
                      ? "bg-muted text-foreground font-medium"
                      : "text-muted-foreground hover:text-foreground"
                  )}
                >
                  {entry.label}
                </button>
              ))}
            </nav>
            <Card>
              <CardHeader>
                <CardTitle>
                  {SECTIONS.find((entry) => entry.id === section)?.label}
                </CardTitle>
              </CardHeader>
              <CardContent>
                {section === "general" && (
                  <GeneralSection org={org} base={base} onChange={refresh} />
                )}
                {section === "members" && <MembersSection base={base} />}
                {section === "access" && (
                  <AccessSection org={org} base={base} onChange={refresh} />
                )}
                {section === "sharing" && (
                  <SharingSection org={org} base={base} onChange={refresh} />
                )}
              </CardContent>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
