import { Suspense } from "react";
import TasksPage from "../../../../src/app/(app)/tasks/page";
import { Providers } from "../../../../src/app/providers";

export default function Page() {
  return (
    <Providers>
      {/* Stands in for the app nav: the Tasks header pins 3.5rem from the top,
          so without it the header sits over the first rows. */}
      <div aria-hidden style={{ height: "3.5rem" }} />
      <Suspense>
        <TasksPage />
      </Suspense>
    </Providers>
  );
}
