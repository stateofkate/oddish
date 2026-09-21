import { NextRequest, NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend-config";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ token: string; task_id: string }> }
) {
  try {
    const { token, task_id } = await params;
    const url = new URL(
      getBackendUrl(
        "public/experiments",
        `/${encodeURIComponent(token)}/tasks/${encodeURIComponent(task_id)}/qa`
      )
    );
    const version = request.nextUrl.searchParams.get("version");
    if (version !== null) url.searchParams.set("version", version);
    const res = await fetch(url, { cache: "no-store" });
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;

    return NextResponse.json(data ?? { error: "Upstream error" }, {
      status: res.status,
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
