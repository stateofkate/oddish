import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

// The backend serves /qa-evals (start a run) and its reads at the same paths.
type Params = { params: Promise<{ path?: string[] }> };

async function backendPath(request: NextRequest, { params }: Params) {
  const { path = [] } = await params;
  return (
    ["qa-evals", ...path].map(encodeURIComponent).join("/") +
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

export async function POST(request: NextRequest, context: Params) {
  return proxyJsonRequest(request, await backendPath(request, context), "POST");
}
