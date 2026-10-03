import { redirect } from "next/navigation";
import { qaIssuesHref, parseQaIssuesView } from "@/lib/qa-issues";

// The QA view lives on the shared experiment page; old links keep their view.
export default async function ShareQaIssuesPage({
  params,
  searchParams,
}: {
  params: Promise<{ token: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { token } = await params;
  redirect(
    qaIssuesHref(
      `/share/${encodeURIComponent(token)}`,
      parseQaIssuesView(await searchParams)
    )
  );
}
