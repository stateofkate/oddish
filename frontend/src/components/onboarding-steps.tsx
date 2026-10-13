"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { useAuth, useUser } from "@clerk/nextjs";
import {
  ArrowRight,
  Bot,
  Check,
  Copy,
  Key,
  Loader2,
  Terminal,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { apiFetch, fetcher } from "@/lib/api";
import { useOrgHref } from "@/lib/use-org-href";
import { cn } from "@/lib/utils";

const PROD_API_URL = "https://api.oddish.app";
// Production's Modal hostname, which the app calls; users see the custom domain.
const PROD_MODAL_API_URL = "https://abundant-ai--api.modal.run";
// The install script and CLI default to production; other deployments
// (staging, previews) need the CLI pointed at them.
const API_URL = (() => {
  const configured = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");
  return configured.startsWith("https://") && configured !== PROD_MODAL_API_URL
    ? configured
    : PROD_API_URL;
})();
const INSTALL_COMMAND = `curl -fsSL ${API_URL}/cli/install.sh | sh`;
const FIRST_RUN_COMMAND = "oddish run -p my-task -a codex -m openai/gpt-5.4";

type Shell = "zsh" | "bash";

interface CreatedKey {
  id: string;
  key: string;
}

interface ListedKey {
  id: string;
  is_active: boolean;
  last_used_at: string | null;
  created_at: string;
  mine?: boolean;
}

interface KeyPermissions {
  can_create: boolean;
  allowed_scopes: string[];
}

function exportCommand(key: string, shell: Shell): string {
  const rc = shell === "zsh" ? "~/.zshrc" : "~/.bashrc";
  const lines = [`export ODDISH_API_KEY="${key}"`];
  if (API_URL !== PROD_API_URL)
    lines.push(`export ODDISH_API_URL="${API_URL}"`);
  const append = lines.map((line) => `echo '${line}' >> ${rc}`).join(" && ");
  return `${append} && source ${rc}`;
}

const AGENT_INSTRUCTIONS = `# Set up the Oddish CLI

Oddish runs evaluation tasks for AI agents in the cloud. The human you work with has an Oddish API key in the environment variable \`ODDISH_API_KEY\`. Never print, log, or commit its value.

1. Check that \`ODDISH_API_KEY\` is set: \`[ -n "$ODDISH_API_KEY" ] && echo set\`. If it is not, stop and ask the human to create a key on the Oddish dashboard and add the export line it shows to their shell profile.
2. Install or update the CLI:

\`\`\`sh
${API_URL === PROD_API_URL ? "" : `export ODDISH_API_URL="${API_URL}"\n`}${INSTALL_COMMAND}
\`\`\`

It installs to \`~/.oddish/cli\` and links \`~/.local/bin/oddish\`. Add \`~/.local/bin\` to \`PATH\` if \`oddish\` is not found.

3. Verify with \`oddish version\`, then \`oddish ls\`. An empty list is fine.
4. Before running anything, read the guide that ships with the CLI: \`~/.oddish/cli/oddish-*/src/oddish/assets/skills/oddish/SKILL.md\`.
5. A first run looks like this, where \`-p\` is a local task folder, \`-a\` the agent and \`-m\` the model. \`oddish --help\` lists every command.

\`\`\`sh
${FIRST_RUN_COMMAND}
\`\`\`
`;

function useCopy() {
  const [copied, setCopied] = useState<string | null>(null);
  /** Resolves true only when the text actually reached the clipboard. */
  const copy = async (id: string, text: string): Promise<boolean> => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(id);
      window.setTimeout(() => setCopied((c) => (c === id ? null : c)), 1500);
      return true;
    } catch {
      setCopied(null);
      return false;
    }
  };
  return { copied, copy };
}

/** A copyable command; ``display`` can mask a secret that ``command`` holds. */
export function CommandSnippet({
  command,
  display,
}: {
  command: string;
  display?: string;
}) {
  const { copied, copy } = useCopy();
  return (
    <div className="border-border/80 bg-muted/35 flex items-center gap-2 rounded-md border px-3 py-2">
      <code className="min-w-0 flex-1 font-mono text-xs leading-relaxed break-all whitespace-pre-wrap">
        {display ?? command}
      </code>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-7 shrink-0 px-2 text-xs"
        onClick={() => copy("snippet", command)}
        aria-label="Copy command"
      >
        {copied ? (
          <Check className="h-3.5 w-3.5 text-[color:var(--paper-pass)]" />
        ) : (
          <Copy className="h-3.5 w-3.5" />
        )}
      </Button>
    </div>
  );
}

function Step({
  n,
  done,
  active,
  icon,
  title,
  status,
  children,
}: {
  n: number;
  done: boolean;
  active: boolean;
  icon: React.ReactNode;
  title: string;
  status?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section
      aria-label={title}
      className={cn(
        "bg-background/80 flex min-w-0 flex-col gap-3 rounded-lg border p-4 transition-colors",
        done
          ? "border-[color:var(--paper-pass)]/40"
          : active
            ? "border-[#6f88b4]/60 shadow-[0_0_0_1px_rgba(111,136,180,0.25)]"
            : "border-border/70"
      )}
    >
      <div className="flex items-center gap-2 text-sm font-medium">
        <span
          className={cn(
            "flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold transition-colors",
            done
              ? "bg-[color:var(--paper-pass)] text-white"
              : active
                ? "bg-[#6f88b4] text-white"
                : "bg-muted text-muted-foreground"
          )}
        >
          {done ? <Check className="h-3.5 w-3.5" /> : n}
        </span>
        <span className="text-muted-foreground">{icon}</span>
        <span className="truncate">{title}</span>
        {status && <span className="ml-auto shrink-0 text-xs">{status}</span>}
      </div>
      {children}
    </section>
  );
}

function ShellToggle({
  shell,
  onChange,
}: {
  shell: Shell;
  onChange: (shell: Shell) => void;
}) {
  return (
    <div
      role="radiogroup"
      aria-label="Shell"
      className="bg-muted/50 inline-flex w-fit rounded-md border p-0.5 text-xs"
    >
      {(["zsh", "bash"] as const).map((option) => (
        <button
          key={option}
          type="button"
          role="radio"
          aria-checked={shell === option}
          onClick={() => onChange(option)}
          className={cn(
            "rounded px-2.5 py-1 font-mono transition-colors",
            shell === option
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground"
          )}
        >
          {option}
        </button>
      ))}
    </div>
  );
}

/** First-run setup on an empty dashboard: key, install, agent, first job. */
export function OnboardingSteps() {
  const { user } = useUser();
  const { orgId, userId } = useAuth();
  const orgHref = useOrgHref();
  const { data: permissions } = useSWR<KeyPermissions>(
    "/api/settings/api-keys/permissions",
    fetcher,
    { revalidateOnFocus: false }
  );
  const [created, setCreated] = useState<CreatedKey | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shell, setShell] = useState<Shell>("zsh");
  const [agentCopied, setAgentCopied] = useState(false);
  const [showAgent, setShowAgent] = useState(false);
  const { copied, copy } = useCopy();

  // The caller's own keys; polled until one is used by the CLI.
  const { data: keys } = useSWR<ListedKey[]>(
    "/api/settings/api-keys",
    fetcher,
    {
      refreshInterval: (latest) =>
        ownKeys(latest).some((key) => key.last_used_at) ? 0 : 5000,
    }
  );
  const mine = ownKeys(keys);
  const connected = mine.some((key) => key.last_used_at);
  const hasKey = Boolean(created) || mine.length > 0;

  const { mutate: mutateKeys } = useSWR<ListedKey[]>("/api/settings/api-keys");
  const scope = permissions?.allowed_scopes?.[0];
  const createKey = async () => {
    if (!scope) return;
    setCreating(true);
    setError(null);
    try {
      const day = new Date().toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
      });
      const who = user?.firstName ? `${user.firstName}'s key` : "CLI key";
      const res = await apiFetch("/api/settings/api-keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: `${who} · ${day}`, scope }),
      });
      const payload = await res.json().catch(() => null);
      if (!res.ok || !payload?.key) {
        throw new Error(payload?.detail || payload?.error || "Request failed");
      }
      setCreated({ id: payload.id, key: payload.key });
      mutateKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setCreating(false);
    }
  };

  // Make the first key unasked, once, for anyone who has none of their own.
  // The ref covers this render tree; the stored flag, written before the
  // request, covers remounts, reloads and other tabs.
  const autoCreated = useRef(false);
  useEffect(() => {
    if (autoCreated.current || !keys || !permissions?.can_create || !scope)
      return;
    if (ownKeys(keys).length > 0) return;
    const flag = `oddish:first-key:${orgId ?? ""}:${userId ?? ""}`;
    if (!orgId || !userId || readFlag(flag)) return;
    autoCreated.current = true;
    writeFlag(flag);
    void createKey();
    // createKey is stable enough for a one-shot; the ref guards re-runs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keys, permissions?.can_create, scope, orgId, userId]);

  const doneCount = [hasKey, connected, agentCopied, false].filter(
    Boolean
  ).length;
  const masked = created
    ? `${created.key.slice(0, 7)}…${created.key.slice(-4)}`
    : null;
  const lastMade = mine[0]
    ? new Date(mine[0].created_at).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
      })
    : null;

  return (
    <div className="bg-card/60 rounded-lg border border-dashed border-[#6f88b4]/30 p-5 sm:p-6">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-base font-medium">Get set up</p>
          <p className="text-muted-foreground text-sm">No experiments yet</p>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-muted-foreground font-mono text-xs tabular-nums">
            {doneCount} / 4
          </span>
          <div className="bg-muted h-1.5 w-28 overflow-hidden rounded-full">
            <div
              className="h-full rounded-full bg-[color:var(--paper-pass)] transition-[width] duration-500"
              style={{ width: `${(doneCount / 4) * 100}%` }}
            />
          </div>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <Step
          n={1}
          done={hasKey}
          active={!hasKey}
          icon={<Key className="h-4 w-4" />}
          title="Your API key"
        >
          {created ? (
            <>
              <div className="rounded-md border border-[color:var(--paper-pass)]/40 bg-[color:var(--paper-pass)]/10 px-3 py-2 text-sm font-medium">
                We made your first key for you
                <span className="text-muted-foreground block text-xs font-normal">
                  Shown once. Paste this into your terminal.
                </span>
              </div>
              <ShellToggle shell={shell} onChange={setShell} />
              <CommandSnippet
                command={exportCommand(created.key, shell)}
                display={exportCommand(masked ?? "", shell)}
              />
            </>
          ) : mine.length > 0 ? (
            <>
              <p className="text-sm">
                Made {lastMade} · saved as{" "}
                <code className="font-mono text-xs">ODDISH_API_KEY</code>
              </p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={createKey}
                disabled={creating || !scope}
                className="w-fit"
              >
                {creating && (
                  <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
                )}
                Make another
              </Button>
            </>
          ) : permissions && !permissions.can_create ? (
            <Link
              href={orgHref("/settings?section=api-keys")}
              className="text-sm underline underline-offset-2"
            >
              Ask an org admin for a key
            </Link>
          ) : error ? (
            <>
              <p role="alert" className="text-xs text-red-500">
                {error}
              </p>
              <Button
                type="button"
                size="sm"
                onClick={createKey}
                className="w-fit"
              >
                Try again
              </Button>
            </>
          ) : creating ? (
            <p className="text-muted-foreground inline-flex items-center gap-2 text-sm">
              <Loader2 className="h-4 w-4 animate-spin" />
              Making your key…
            </p>
          ) : !keys || !permissions ? (
            <div className="bg-muted/60 h-9 w-40 animate-pulse rounded-md" />
          ) : (
            <Button
              type="button"
              onClick={createKey}
              disabled={!scope}
              className="w-fit"
            >
              <Key className="mr-2 h-4 w-4" />
              Create key
            </Button>
          )}
          {error && (created || mine.length > 0) && (
            <p role="alert" className="text-xs text-red-500">
              {error}
            </p>
          )}
        </Step>

        <Step
          n={2}
          done={connected}
          active={hasKey && !connected}
          icon={<Terminal className="h-4 w-4" />}
          title="Install the CLI"
          status={
            connected ? (
              <span className="text-[color:var(--paper-pass)]">Connected</span>
            ) : hasKey ? (
              <span className="text-muted-foreground inline-flex items-center gap-1">
                <Loader2 className="h-3 w-3 animate-spin" />
                Waiting
              </span>
            ) : null
          }
        >
          <CommandSnippet command={INSTALL_COMMAND} />
          <CommandSnippet command="oddish ls" />
        </Step>

        <Step
          n={3}
          done={agentCopied}
          active={connected && !agentCopied}
          icon={<Bot className="h-4 w-4" />}
          title="Set up your agent"
        >
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={async () => {
                if (await copy("agent", AGENT_INSTRUCTIONS))
                  setAgentCopied(true);
              }}
            >
              {copied === "agent" ? (
                <Check className="mr-1.5 h-3.5 w-3.5 text-[color:var(--paper-pass)]" />
              ) : (
                <Copy className="mr-1.5 h-3.5 w-3.5" />
              )}
              Copy instructions
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-expanded={showAgent}
              onClick={() => setShowAgent((v) => !v)}
            >
              {showAgent ? "Hide" : "Preview"}
            </Button>
          </div>
          {showAgent && (
            <pre className="bg-muted/35 max-h-56 overflow-auto rounded-md border p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
              {AGENT_INSTRUCTIONS}
            </pre>
          )}
        </Step>

        <Step
          n={4}
          done={false}
          active={agentCopied}
          icon={<ArrowRight className="h-4 w-4" />}
          title="Submit your first job"
        >
          <CommandSnippet command={FIRST_RUN_COMMAND} />
        </Step>
      </div>
    </div>
  );
}

function readFlag(name: string): boolean {
  try {
    return window.localStorage.getItem(name) !== null;
  } catch {
    return false;
  }
}

function writeFlag(name: string): void {
  try {
    window.localStorage.setItem(name, new Date().toISOString());
  } catch {
    // Storage blocked: the in-memory ref still guards this page.
  }
}

function ownKeys(keys: ListedKey[] | undefined): ListedKey[] {
  // Anything but a list (an error body) means no keys. A key without
  // ``mine`` (an older server) counts as yours, so nothing is minted unasked.
  return Array.isArray(keys)
    ? keys.filter((key) => key.is_active && key.mine !== false)
    : [];
}
