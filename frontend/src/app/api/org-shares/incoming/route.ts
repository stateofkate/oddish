import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export const GET = (request: NextRequest) =>
  proxyBackendJson({ request, path: "org-shares/incoming" });
