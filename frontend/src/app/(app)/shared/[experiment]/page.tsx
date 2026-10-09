"use client";

import { useParams } from "next/navigation";
import { SharedExperiment } from "@/components/shared-experiment";

/** An experiment shared with this org, inside the app (by experiment id). */
export default function SharedExperimentPage() {
  const params = useParams();
  const id = Array.isArray(params.experiment)
    ? params.experiment[0]
    : params.experiment;
  return (
    <SharedExperiment
      shareKey={id}
      pageHref={`/shared/${encodeURIComponent(id ?? "")}`}
    />
  );
}
