"use client";

import { useParams } from "next/navigation";
import { CustomerOrgPage } from "@/components/customer-org-page";

export default function CustomerOrgRoute() {
  const params = useParams();
  const orgId = Array.isArray(params.org) ? params.org[0] : params.org;
  return orgId ? <CustomerOrgPage orgId={orgId} /> : null;
}
