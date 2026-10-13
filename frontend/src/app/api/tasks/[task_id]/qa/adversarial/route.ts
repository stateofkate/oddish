import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

// Forward the version-pinned adversarial request through the existing user auth.
export async function POST(
  request: Request,
  { params }: { params: Promise<{ task_id: string }> },
) {
  try {
    const authObj = await auth();
    if (!authObj || !authObj.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const token = await getClerkToken(authObj.getToken);
    if (!token) {
      return NextResponse.json(
        { error: "Failed to get authentication token" },
        { status: 401 },
      );
    }

    const { task_id } = await params;
    const url = getBackendUrl("tasks", `/${task_id}/qa/adversarial`);
    const res = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: { ...getAuthHeaders(token), "Content-Type": "application/json" },
      body: (await request.text()) || undefined,
    });

    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      return NextResponse.json(body, { status: res.status });
    }
    return NextResponse.json(body);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
