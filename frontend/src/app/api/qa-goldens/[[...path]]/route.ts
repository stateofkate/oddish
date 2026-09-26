import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

// The backend serves the golden list, the labeling queue and trial labels at
// the same paths under /qa-goldens.
type Params = { params: Promise<{ path?: string[] }> };

async function backendPath(request: NextRequest, { params }: Params) {
  const { path = [] } = await params;
  return (
    ["qa-goldens", ...path].map(encodeURIComponent).join("/") +
    request.nextUrl.search
  );
}

export async function GET(request: NextRequest, context: Params) {
  return proxyBackendJson({
    request,
    path: await backendPath(request, context),
    signal: request.signal,
  });
}

export async function PUT(request: NextRequest, context: Params) {
  return proxyJsonRequest(request, await backendPath(request, context), "PUT");
}
