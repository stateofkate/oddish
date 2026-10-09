import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

type Params = { params: Promise<{ id: string }> };

const path = async (params: Params["params"]) =>
  `org-shares/${encodeURIComponent((await params).id)}`;

export const PATCH = async (request: NextRequest, { params }: Params) =>
  proxyJsonRequest(request, await path(params), "PATCH");

export const DELETE = async (request: NextRequest, { params }: Params) =>
  proxyBackendJson({ request, path: await path(params), method: "DELETE" });
