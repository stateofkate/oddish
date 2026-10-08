import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ task_id: string; version: string }> }
) {
  const { task_id, version } = await params;
  return proxyBackendJson({
    request,
    path:
      `tasks/${encodeURIComponent(task_id)}/versions/${encodeURIComponent(version)}/reviews` +
      request.nextUrl.search,
  });
}
