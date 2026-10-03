import { redirect } from "next/navigation";
import { qaIssuesHref, parseQaIssuesView } from "@/lib/qa-issues";
import {
  decodeExperimentRouteParam,
  encodeExperimentRouteParam,
} from "@/lib/utils";

// The QA view lives on the experiment page; old links keep their view.
export default async function ExperimentQaIssuesPage({
  params,
  searchParams,
}: {
  params: Promise<{ experiment: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const experimentId = decodeExperimentRouteParam((await params).experiment);
  redirect(
    qaIssuesHref(
      `/experiments/${encodeExperimentRouteParam(experimentId)}`,
      parseQaIssuesView(await searchParams)
    )
  );
}
