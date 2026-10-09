"use client";

import { useState } from "react";
import { useAuth } from "@clerk/nextjs";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import useSWR from "swr";
import { Download } from "lucide-react";
import { TaskFilesPanel, type TaskPane } from "@/components/task-files-panel";
import { fetcher } from "@/lib/api";
import { useOrgHref } from "@/lib/use-org-href";
import { PUBLIC_API_URL } from "@/lib/utils";

// Shared tasks open under this key instead of an experiment's (see
// DIRECT_TASK_KEY in oddish/core/sharing/helpers.py).
const BASE = `${PUBLIC_API_URL}/experiments/direct`;

/** A task shared with this org, or shipped to it in a delivery. */
export default function SharedTaskPage() {
  const params = useParams();
  const id = Array.isArray(params.task) ? params.task[0] : params.task;
  const versionParam = useSearchParams().get("version");
  const version = versionParam ? Number(versionParam) : undefined;
  const orgHref = useOrgHref();
  // The instruction is what a customer reads first; QA is under Overview.
  const [pane, setPane] = useState<TaskPane>("file");
  // Signed out, a shared task answers 404: wait for the token.
  const { isLoaded } = useAuth();
  const query = new URLSearchParams({ recursive: "false", limit: "1" });
  if (version != null) query.set("version", String(version));
  const { data: listing } = useSWR<{ archive_url?: string | null }>(
    isLoaded && id
      ? `${BASE}/tasks/${encodeURIComponent(id)}/files?${query}`
      : null,
    fetcher
  );
  if (!isLoaded || !id) return null;

  return (
    <div className="flex h-[calc(100vh-7rem)] flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <Link
          href={orgHref("/shared/tasks")}
          className="text-muted-foreground text-sm"
        >
          ← Tasks
        </Link>
        {listing?.archive_url && (
          <a
            href={listing.archive_url}
            className="inline-flex items-center gap-1.5 text-sm font-medium"
          >
            <Download className="h-4 w-4" />
            Download task
          </a>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-hidden rounded-lg border">
        <TaskFilesPanel
          isOpen
          onClose={() => undefined}
          taskId={id}
          apiBaseUrl={BASE}
          readOnly
          allowRetry={false}
          activePane={pane}
          onActivePaneChange={setPane}
          contentOnly
          loadFilesLazily
          taskVersion={version}
          initialFilePath="instruction.md"
          showRuns={false}
        />
      </div>
    </div>
  );
}
