import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

// Shared tasks and deliveries: /org-shares/incoming/{tasks,deliveries[/<id>]}.
type Params = { params: Promise<{ path: string[] }> };

export const GET = async (request: NextRequest, { params }: Params) => {
  const { path } = await params;
  // "." and ".." would step out of /org-shares/incoming on the backend.
  if (path.some((part) => part === "." || part === ".."))
    return Response.json({ detail: "Not found" }, { status: 404 });
  return proxyBackendJson({
    request,
    path: `org-shares/incoming/${path.map(encodeURIComponent).join("/")}`,
  });
};
