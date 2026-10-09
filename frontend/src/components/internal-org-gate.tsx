"use client";

import { useIsCustomerOrg } from "@/lib/use-org-access";

/** Renders its children only once the active org is known not to be a
 *  customer org; ViewerRedirect then moves customers off the page. */
export function InternalOrgGate({ children }: { children: React.ReactNode }) {
  const customer = useIsCustomerOrg();
  return customer === false ? <>{children}</> : null;
}
