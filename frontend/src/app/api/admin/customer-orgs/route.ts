import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

const PATH = "admin/customer-orgs";

export const GET = (request: NextRequest) =>
  proxyBackendJson({ request, path: PATH });

export const POST = (request: NextRequest) =>
  proxyJsonRequest(request, PATH, "POST");
