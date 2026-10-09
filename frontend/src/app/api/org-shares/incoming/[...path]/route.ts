import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

// Shared tasks and deliveries: /org-shares/incoming/{tasks,deliveries[/<id>]}.
type Params = { params: Promise<{ path: string[] }> };

export const GET = async (request: NextRequest, { params }: Params) =>
  proxyBackendJson({
    request,
    path: `org-shares/incoming/${(await params).path.map(encodeURIComponent).join("/")}`,
  });
