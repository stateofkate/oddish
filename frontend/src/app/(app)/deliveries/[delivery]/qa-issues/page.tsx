import { QaIssuesPage } from "@/components/qa-issues-report";
import { parseQaIssuesView } from "@/lib/qa-issues";
import { fetchQaIssueReport } from "@/lib/qa-issues-server";

export default async function DeliveryQaIssuesPage({
  params,
  searchParams,
}: {
  params: Promise<{ delivery: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { delivery } = await params;
  const view = parseQaIssuesView(await searchParams);
  const href = `/deliveries/${encodeURIComponent(delivery)}`;
  return (
    <QaIssuesPage
      report={fetchQaIssueReport("deliveries", delivery, view)}
      backHref={href}
      baseHref={`${href}/qa-issues`}
      apiHref={`/api${href}/qa-issues`}
    />
  );
}
