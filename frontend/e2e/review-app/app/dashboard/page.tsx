import { DashboardClient } from "@/app/(app)/dashboard/dashboard-client";

export default async function DashboardFixture({
  searchParams,
}: {
  searchParams: Promise<{ author?: string }>;
}) {
  const { author = "all" } = await searchParams;
  return (
    <DashboardClient
      initialAuthor={author}
      experimentsPromise={Promise.resolve({
        experiments: [],
        hasMore: false,
        ok: true,
      })}
    />
  );
}
