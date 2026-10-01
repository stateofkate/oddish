import { Suspense } from "react";
import { DashboardClient } from "@/app/(app)/dashboard/dashboard-client";

// Mirrors src/app/(app)/dashboard/page.tsx: DashboardClient reads its filters
// from the URL, so it needs the same Suspense boundary.
export default function DashboardFixture() {
  return (
    <Suspense>
      <DashboardClient />
    </Suspense>
  );
}
