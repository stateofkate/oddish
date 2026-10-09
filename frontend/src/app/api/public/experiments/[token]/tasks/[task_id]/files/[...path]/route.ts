import { NextRequest, NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend-config";
import { publicAuthHeaders } from "@/lib/backend-response";

export async function GET(
  request: NextRequest,
  {
    params,
  }: { params: Promise<{ token: string; task_id: string; path: string[] }> },
) {
  try {
    const { token, task_id, path } = await params;
    const filePath = path.join("/");
    const search = request.nextUrl.search;
    const url = getBackendUrl(
      "public/experiments",
      `/${token}/tasks/${task_id}/files/${filePath}${search}`,
    );
    const res = await fetch(url, {
      cache: "no-store",
      headers: await publicAuthHeaders(),
    });

    if (!res.ok) {
      const error = await res.json().catch(() => ({ detail: res.statusText }));
      return NextResponse.json(error, { status: res.status });
    }

    const data = await res.json();
    return NextResponse.json(data, {
      headers: {
        "Cache-Control": "private, max-age=300, stale-while-revalidate=60",
        Vary: "Cookie",
      },
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
