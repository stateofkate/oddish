"use client";

import { useParams } from "next/navigation";
import { ShareNav } from "@/components/share-nav";
import { SharedExperiment } from "@/components/shared-experiment";

export default function PublicExperimentPage() {
  const params = useParams();
  const token = Array.isArray(params.token) ? params.token[0] : params.token;
  return (
    <>
      <ShareNav />
      <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
        <SharedExperiment
          shareKey={token}
          pageHref={`/share/${encodeURIComponent(token ?? "")}`}
        />
      </main>
    </>
  );
}
