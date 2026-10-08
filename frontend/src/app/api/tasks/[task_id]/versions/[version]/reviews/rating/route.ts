import { NextRequest } from "next/server";
import { proxyJsonRequest } from "@/lib/backend-response";

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ task_id: string; version: string }> }
) {
  const { task_id, version } = await params;
  return proxyJsonRequest(
    request,
    `tasks/${encodeURIComponent(task_id)}/versions/${encodeURIComponent(version)}/reviews/rating`,
    "PUT"
  );
}
