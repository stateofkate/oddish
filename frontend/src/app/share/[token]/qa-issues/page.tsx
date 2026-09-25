import { QaIssuesPage } from "@/components/qa-issues-report";
import { ShareNav } from "@/components/share-nav";
import { parseQaIssuesView } from "@/lib/qa-issues";
import { fetchPublicQaIssueReport } from "@/lib/qa-issues-server";

export default async function ShareQaIssuesPage({
  params,
  searchParams,
}: {
  params: Promise<{ token: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { token } = await params;
  const view = parseQaIssuesView(await searchParams);
  const href = `/share/${encodeURIComponent(token)}`;
  return (
    <>
      <ShareNav />
      <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
        <QaIssuesPage
          report={fetchPublicQaIssueReport(token, view)}
          view={view}
          backHref={href}
          baseHref={`${href}/qa-issues`}
          linkTasks={false}
        />
      </main>
    </>
  );
}
