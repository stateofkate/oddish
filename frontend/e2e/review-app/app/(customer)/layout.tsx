import { Nav } from "@/components/nav";
import { ViewerRedirect } from "@/components/viewer-redirect";

// The app shell's org gating, around the real customer pages.
export default function CustomerLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <>
      <ViewerRedirect />
      <Nav />
      {children}
    </>
  );
}
