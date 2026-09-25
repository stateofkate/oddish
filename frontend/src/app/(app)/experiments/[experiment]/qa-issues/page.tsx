import { QaIssuesPage } from "@/components/qa-issues-report";
import { parseQaIssuesView } from "@/lib/qa-issues";
import { fetchQaIssueReport } from "@/lib/qa-issues-server";
import {
  decodeExperimentRouteParam,
  encodeExperimentRouteParam,
} from "@/lib/utils";

export default async function ExperimentQaIssuesPage({
  params,
  searchParams,
}: {
  params: Promise<{ experiment: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const experimentId = decodeExperimentRouteParam((await params).experiment);
  const view = parseQaIssuesView(await searchParams);
  const href = `/experiments/${encodeExperimentRouteParam(experimentId)}`;
  return (
    <QaIssuesPage
      report={fetchQaIssueReport("experiments", experimentId, view)}
      backHref={href}
      baseHref={`${href}/qa-issues`}
      apiHref={`/api${href}/qa-issues`}
    />
  );
}
