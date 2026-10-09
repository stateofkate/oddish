import { useSyncExternalStore } from "react";

// Separate local fixture app; production authentication is unchanged.
// A spec may set window.__fixtureClerk (isSignedIn, isLoaded) before load.
const overrides =
  typeof window === "undefined"
    ? {}
    : ((window as { __fixtureClerk?: object }).__fixtureClerk ?? {});
let identity = {
  orgRole: "org:admin",
  userId: "maya",
  orgId: "fixture",
  isLoaded: true,
  ...overrides,
};
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};
export function setFixtureAccount(userId: string, orgId: string) {
  identity = { orgRole: "org:admin", userId, orgId, isLoaded: true };
  listeners.forEach((listener) => listener());
}
export const useAuth = () =>
  useSyncExternalStore(
    subscribe,
    () => identity,
    () => identity
  );
export const useOrganization = () => ({ organization: null, isLoaded: true });

// Public dataset navigation renders a signed-out account in this fixture.
export const useUser = () => ({
  user: null,
  isLoaded: true,
  isSignedIn: false,
});
export const useClerk = () => ({ signOut: async () => {} });
export const OrganizationSwitcher = () => null;
export const SignInButton = ({
  children,
}: {
  children: import("react").ReactNode;
}) => children;
