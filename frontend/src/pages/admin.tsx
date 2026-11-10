import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useRef, type KeyboardEvent } from "react";

import { fetchIdentity } from "../lib/api";
import { AdminSystemPage } from "./admin-system";
import { AdminUsersPage } from "./admin-users";

export type AdminTab = "users" | "system";

/**
 * UI-025: the selected panel lives in the route search param, so
 * `/admin?tab=system` opens on System after a reload, a bookmark or a link
 * pasted to a colleague. The param stays optional so a plain `/admin` link
 * keeps working; anything unrecognised is ignored rather than rendered blank.
 */
export function adminTabSearch(search: Record<string, unknown>): { tab?: AdminTab } {
  return search.tab === "system" || search.tab === "users" ? { tab: search.tab } : {};
}

/**
 * Compact Administration area. Tab visibility is capability-driven
 * (ACCESS_ADMIN -> Users & Access, SYSTEM_STATUS_READ -> System); the shell
 * shows the entry point when either capability is present. Server routes
 * remain authoritative — this page is presentation only.
 *
 * The route is the ONLY tab state: there is no local copy that could drift
 * away from the address bar, the Back button or a deep link.
 */
export function AdminPage() {
  const identity = useQuery({ queryKey: ["identity"], queryFn: fetchIdentity });
  const capabilities = new Set(identity.data?.capabilities ?? []);
  const canManageUsers = capabilities.has("ACCESS_ADMIN");
  const canReadSystem = capabilities.has("SYSTEM_STATUS_READ");
  const { tab: requestedTab } = useSearch({ from: "/admin" });
  const tab: AdminTab = requestedTab ?? "users";
  const navigate = useNavigate();
  const tabRefs = useRef<Partial<Record<AdminTab, HTMLButtonElement | null>>>({});

  if (!canManageUsers && !canReadSystem) {
    return (
      <section>
        <h1>Administration</h1>
        <p className="status-error">Access to this area is restricted to administrators.</p>
      </section>
    );
  }
  // A link may name a panel this account cannot read; the capability wins.
  const effectiveTab: AdminTab =
    tab === "system" && !canReadSystem
      ? "users"
      : tab === "users" && !canManageUsers
        ? "system"
        : tab;

  const available: AdminTab[] = [
    ...(canManageUsers ? (["users"] as AdminTab[]) : []),
    ...(canReadSystem ? (["system"] as AdminTab[]) : []),
  ];

  const select = (next: AdminTab) => void navigate({ to: "/admin", search: { tab: next } });

  // Arrow keys move selection AND focus, which is what `role="tablist"`
  // promises. Selection goes through the URL like every other change, so Back
  // returns to the previous panel.
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (step === 0 || available.length < 2) return;
    event.preventDefault();
    const index = available.indexOf(effectiveTab);
    const next = available[(index + step + available.length) % available.length];
    if (next === effectiveTab) return;
    select(next);
    tabRefs.current[next]?.focus();
  };

  return (
    <section className="admin-page">
      <h1>Administration</h1>
      <nav aria-label="Administration" className="admin-tabs" role="tablist" onKeyDown={onKeyDown}>
        {canManageUsers && (
          <button
            type="button"
            role="tab"
            id="admin-tab-users"
            ref={(node) => {
              tabRefs.current.users = node;
            }}
            aria-controls="admin-tabpanel"
            aria-selected={effectiveTab === "users"}
            tabIndex={effectiveTab === "users" ? 0 : -1}
            className={effectiveTab === "users" ? "admin-tab active" : "admin-tab"}
            onClick={() => select("users")}
          >
            Users &amp; Access
          </button>
        )}
        {canReadSystem && (
          <button
            type="button"
            role="tab"
            id="admin-tab-system"
            ref={(node) => {
              tabRefs.current.system = node;
            }}
            aria-controls="admin-tabpanel"
            aria-selected={effectiveTab === "system"}
            tabIndex={effectiveTab === "system" ? 0 : -1}
            className={effectiveTab === "system" ? "admin-tab active" : "admin-tab"}
            onClick={() => select("system")}
          >
            System
          </button>
        )}
      </nav>
      {/* role="tab" only means anything with a panel it controls: without the
       *  tabpanel association the two buttons promise a relationship that
       *  assistive technology cannot follow. */}
      <div
        role="tabpanel"
        id="admin-tabpanel"
        aria-labelledby={`admin-tab-${effectiveTab}`}
        tabIndex={0}
      >
        {effectiveTab === "users" ? <AdminUsersPage /> : <AdminSystemPage />}
      </div>
    </section>
  );
}
