import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

// Every /admin/customer-orgs/<org>/... call; the backend checks operator access.
type Params = { params: Promise<{ path: string[] }> };

// "." and ".." segments are dropped so a path can't leave /admin/customer-orgs.
const backendPath = async (params: Params["params"]) =>
  `admin/customer-orgs/${(await params).path
    .filter((part) => part !== "." && part !== "..")
    .map(encodeURIComponent)
    .join("/")}`;

export const GET = async (request: NextRequest, { params }: Params) =>
  proxyBackendJson({ request, path: await backendPath(params) });

export const DELETE = async (request: NextRequest, { params }: Params) =>
  proxyBackendJson({
    request,
    path: await backendPath(params),
    method: "DELETE",
  });

export const POST = async (request: NextRequest, { params }: Params) =>
  proxyJsonRequest(request, await backendPath(params), "POST");

export const PUT = async (request: NextRequest, { params }: Params) =>
  proxyJsonRequest(request, await backendPath(params), "PUT");

export const PATCH = async (request: NextRequest, { params }: Params) =>
  proxyJsonRequest(request, await backendPath(params), "PATCH");
