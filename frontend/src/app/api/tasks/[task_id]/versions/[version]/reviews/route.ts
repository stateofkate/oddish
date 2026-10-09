import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

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

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ task_id: string; version: string }> }
) {
  const { task_id, version } = await params;
  return proxyJsonRequest(
    request,
    `tasks/${encodeURIComponent(task_id)}/versions/${encodeURIComponent(version)}/reviews`,
    "POST"
  );
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ task_id: string; version: string }> }
) {
  const { task_id, version } = await params;
  return proxyJsonRequest(
    request,
    `tasks/${encodeURIComponent(task_id)}/versions/${encodeURIComponent(version)}/reviews`,
    "DELETE"
  );
}
