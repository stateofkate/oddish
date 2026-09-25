import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import {
  qaIssuesFetchParams,
  type QaIssueReport,
  type QaIssueReportResult,
  type QaIssuesView,
} from "@/lib/qa-issues";

// Started, not awaited, by the page so the report streams into <Suspense>.
// It never rejects: the app has no error boundary for a rejected promise.
// /experiments is public for link previews, so the auth check lives here.
export async function fetchQaIssueReport(
  scope: "experiments" | "deliveries",
  id: string,
  view: QaIssuesView
): Promise<QaIssueReportResult> {
  try {
    const { userId, getToken } = await auth();
    const token = userId ? await getClerkToken(getToken) : null;
    if (!token) return { ok: false, status: 401 };
    const response = await fetch(
      getBackendUrl(
        scope,
        `/${encodeURIComponent(id)}/qa-issues`,
        Object.fromEntries(qaIssuesFetchParams(view))
      ),
      { cache: "no-store", headers: getAuthHeaders(token) }
    );
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      return { ok: false, status: response.status, detail: body?.detail };
    }
    return {
      ok: true,
      report: (await response.json()) as QaIssueReport,
    };
  } catch (error) {
    console.error("[qa-issues] report fetch failed", error);
    return { ok: false, status: 503 };
  }
}

/** A share page's report: no sign-in; the backend strips people and links. */
export async function fetchPublicQaIssueReport(
  token: string,
  view: QaIssuesView
): Promise<QaIssueReportResult> {
  try {
    const response = await fetch(
      getBackendUrl(
        "public",
        `/experiments/${encodeURIComponent(token)}/qa-issues`,
        Object.fromEntries(qaIssuesFetchParams(view))
      ),
      { cache: "no-store" }
    );
    if (!response.ok) return { ok: false, status: response.status };
    return {
      ok: true,
      report: (await response.json()) as QaIssueReport,
    };
  } catch (error) {
    console.error("[qa-issues] public report fetch failed", error);
    return { ok: false, status: 503 };
  }
}
