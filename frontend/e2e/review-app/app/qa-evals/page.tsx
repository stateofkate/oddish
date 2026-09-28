"use client";

import { useState } from "react";
import useSWR from "swr";
import { RunDialog } from "@/components/qa-evals/run-dialog";
import { fetcher } from "@/lib/api";
import type { QAEvalStatus } from "@/lib/qa-evals";

export default function Page() {
  const [open, setOpen] = useState(true);
  const { data, mutate } = useSWR<QAEvalStatus>(
    "/api/qa-evals/status",
    fetcher,
    { refreshInterval: 100, dedupingInterval: 0 }
  );
  return (
    data && (
      <RunDialog
        open={open}
        stage="post_trial"
        status={data}
        trials={null}
        defaultVersion={data.versions[0]}
        onClose={() => setOpen(false)}
        onStarted={() => void mutate()}
      />
    )
  );
}
