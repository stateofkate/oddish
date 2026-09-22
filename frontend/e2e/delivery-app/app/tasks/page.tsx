import { Suspense } from "react";
import TasksPage from "../../../../src/app/(app)/tasks/page";
import { Providers } from "../../../../src/app/providers";

export default function Page() {
  return (
    <Providers>
      <Suspense>
        <TasksPage />
      </Suspense>
    </Providers>
  );
}
