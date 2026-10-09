import { NextRequest } from "next/server";
import { proxyJsonRequest } from "@/lib/backend-response";

async function changeComment(
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
    request.method === "DELETE" ? "DELETE" : "PATCH"
  );
}

export const PATCH = changeComment;
export const DELETE = changeComment;
