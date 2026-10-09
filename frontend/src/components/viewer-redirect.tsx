"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useIsViewerOrg } from "@/lib/use-org-access";
import { useAppPathname, useOrgHref } from "@/lib/use-org-href";

// View-only customer orgs own no data; every other page would be empty or 403.
const VIEWER_PATHS = ["/shared", "/settings"];

export function ViewerRedirect() {
  const viewer = useIsViewerOrg();
  const pathname = useAppPathname();
  const orgHref = useOrgHref();
  const router = useRouter();
  const allowed = VIEWER_PATHS.some((path) => pathname.startsWith(path));
  useEffect(() => {
    if (viewer && !allowed) router.replace(orgHref("/shared"));
  }, [viewer, allowed, orgHref, router]);
  return null;
}
