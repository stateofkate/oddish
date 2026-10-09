import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

export const GET = (request: NextRequest) =>
  proxyBackendJson({ request, path: `org-shares${request.nextUrl.search}` });

export const POST = (request: NextRequest) =>
  proxyJsonRequest(request, "org-shares", "POST");
