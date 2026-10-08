import { NextRequest } from "next/server";
import { proxyJsonRequest } from "@/lib/backend-response";

export async function PATCH(
  request: NextRequest,
  {
    params,
  }: {
    params: Promise<{ task_id: string; version: string; comment_id: string }>;
  }
) {
  const { task_id, version, comment_id } = await params;
  return proxyJsonRequest(
    request,
    `tasks/${encodeURIComponent(task_id)}/versions/${encodeURIComponent(version)}/reviews/comments/${encodeURIComponent(comment_id)}`,
    "PATCH"
  );
}
