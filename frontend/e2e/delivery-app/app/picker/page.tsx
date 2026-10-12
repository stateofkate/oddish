"use client";

import { StrictMode, Suspense, useState } from "react";
import { TestAuthProvider } from "../../clerk";
import { SWRConfig } from "swr";
import TasksPage from "../../../../src/app/(app)/tasks/page";

export default function Picker() {
  const [generation, setGeneration] = useState(0);
  const [orgId, setOrgId] = useState("org-1");
  return (
    <StrictMode>
      <Suspense>
        <SWRConfig value={{ provider: () => new Map() }}>
          {/* Mirrors the app shell: cancels the fixture layout's padding, a
              56px sticky bar standing in for <Nav>, and <main>'s classes from
              app/(app)/layout.tsx, so sticky offsets match production. */}
          <div style={{ margin: -24 }}>
            <div className="bg-background sticky top-0 z-40 flex h-14 items-center gap-2 px-4">
              <button onClick={() => setGeneration((value) => value + 1)}>
                Remount picker
              </button>
              <button
                onClick={() => setOrgId(orgId === "org-1" ? "org-2" : "org-1")}
              >
                Switch organization
              </button>
            </div>
            <div className="mx-auto w-full max-w-(--breakpoint-2xl) px-3 py-3 sm:px-4 sm:py-4">
              <TestAuthProvider
                value={{
                  orgRole: "org:admin",
                  isLoaded: true,
                  userId: "user-1",
                  orgId,
                }}
              >
                <TasksPage key={generation} />
              </TestAuthProvider>
            </div>
          </div>
        </SWRConfig>
      </Suspense>
    </StrictMode>
  );
}
