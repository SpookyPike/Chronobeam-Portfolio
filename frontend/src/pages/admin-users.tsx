import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";

import {
  ADMIN_ROLE_LABELS,
  ALL_ADMIN_ROLES,
  assignAdminRole,
  fetchAdminUser,
  fetchAdminUsers,
  provisionAdminUser,
  revokeAdminRole,
  updateAdminUser,
  type AdminRoleName,
  type AdminUserSortKey,
  type AdminUserSummary,
  type SortDirection,
} from "../lib/api";
import { ConfirmAction, ConfirmStatus, useConfirmArming } from "../lib/confirm";
import { berlinInstantWithYear } from "../lib/format";
import { refusalText } from "../lib/notice";

interface ProvisionForm {
  legal_name: string;
  external_employee_id: string;
  idp_subject: string;
  email: string;
}

const EMPTY_FORM: ProvisionForm = {
  legal_name: "",
  external_employee_id: "",
  idp_subject: "",
  email: "",
};

/** The expression the browser itself applies to `type="email"` (WHATWG HTML).
 *  The app must state the same rule FIRST: a native mismatch blocks the `submit`
 *  event with no request, no message and no `aria-invalid`, so a rule the app
 *  does not state is a rule the operator cannot see (acceptance E3). Native
 *  validation stays as the backstop, never as the only voice. */
const EMAIL_PATTERN =
  /^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$/;

function formatInstant(value: string): string {
  return berlinInstantWithYear(value);
}

/** The table's columns, in display order. Every sortable one carries the server's
 *  own sort vocabulary — the ordering happens in SQL, so it is correct for the whole
 *  matching set rather than for the page that was fetched (the default page is 25
 *  rows, which on a large installation is not the list). Readiness has no server sort
 *  key and is deliberately not sortable: it is derived from three presence flags and
 *  sits next to Status, so the two state columns read together.
 *
 *  Account state precedes contact detail: Status and Readiness are the two columns a
 *  reader scans for, and putting them third and fourth keeps them inside the first
 *  visible width when the table scrolls on a narrow screen. */
const TABLE_COLUMNS: Array<{ key: AdminUserSortKey | null; label: string }> = [
  { key: "name", label: "Name" },
  { key: "employee_id", label: "Employee ID" },
  { key: "status", label: "Status" },
  { key: null, label: "Readiness" },
  { key: "email", label: "Email" },
  { key: "role", label: "Role" },
  { key: "provisioned", label: "Provisioned" },
];

/** What an account is still missing before it can actually be used.
 *
 *  Three provisioning PREREQUISITES, in the words the operator uses for them: the
 *  identity binding that lets SSO resolve a subject to this row, and the two pieces
 *  of HR master data the calculation needs. Presence facts only — nothing here
 *  reads a work record, a balance, a finding or an interval.
 *
 *  ONE vocabulary for the fact, shared by the list column and the detail view, so
 *  "Missing contract" in the list and "no contract" in the audit view cannot look
 *  like two different problems. */
function missingMasterData(row: AdminUserSummary): string[] {
  const missing: string[] = [];
  if (!row.has_effective_contract) missing.push("Missing contract");
  if (!row.has_effective_schedule) missing.push("Missing schedule");
  return missing;
}

function missingPrerequisites(row: AdminUserSummary): string[] {
  return row.has_idp_subject ? missingMasterData(row) : ["Missing SSO", ...missingMasterData(row)];
}

/** The sort affordance: an authored chevron, not a unicode triangle standing in
 *  for an icon set. This repo ships no icon library, and a glyph would be the
 *  costume rather than the icon. `aria-sort` on the `th` carries the state for
 *  assistive technology, so this drawing is decorative and points the way the
 *  column is actually ordered. */
function SortChevron({ direction }: { direction: SortDirection }) {
  return (
    <svg className="sort-chevron" viewBox="0 0 12 8" aria-hidden="true" focusable="false">
      <path
        d={direction === "asc" ? "M1 6.5 6 1.5l5 5" : "M1 1.5l5 5 5-5"}
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Users & Access: list/search, provision, activate/deactivate,
 *  roles, effective capabilities. Identity/access only — never work-time. */
export function AdminUsersPage() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [submittedSearch, setSubmittedSearch] = useState("");
  const [activeFilter, setActiveFilter] = useState<"all" | "active" | "inactive">("all");
  // Defaults match the server's own default order, so the first render and a
  // reload agree instead of the client silently re-imposing a different one.
  const [sort, setSort] = useState<AdminUserSortKey>("provisioned");
  const [direction, setDirection] = useState<SortDirection>("asc");
  // 0 is the server's explicit "every matching user" — the All page size.
  const [pageSize, setPageSize] = useState(25);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<ProvisionForm>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // The row action and the panel it opens are ~1000px apart, so opening one without
  // moving the reader there looks like nothing happened. Only a SELECTION moves the
  // view: a refetch after a mutation must not yank the page around.
  const pendingFocus = useRef<string | null>(null);
  const detailRef = useRef<HTMLElement | null>(null);
  const detailHeadingRef = useRef<HTMLHeadingElement | null>(null);

  const users = useQuery({
    queryKey: ["admin-users", submittedSearch, activeFilter, sort, direction, pageSize],
    queryFn: () =>
      fetchAdminUsers({
        search: submittedSearch || undefined,
        active: activeFilter === "all" ? undefined : activeFilter === "active",
        sort,
        direction,
        limit: pageSize,
      }),
  });
  const detail = useQuery({
    queryKey: ["admin-user", selectedId],
    queryFn: () => fetchAdminUser(selectedId as string),
    enabled: selectedId !== null,
  });

  /** Clicking the column already sorted flips its direction; clicking a different
   *  column starts it ascending, which is the order a reader expects on first
   *  contact with a list of names. */
  const toggleSort = (key: AdminUserSortKey) => {
    if (key === sort) {
      setDirection((current) => (current === "asc" ? "desc" : "asc"));
      return;
    }
    setSort(key);
    setDirection("asc");
  };

  const invalidate = (id = selectedId) => {
    void queryClient.invalidateQueries({ queryKey: ["admin-users"] });
    if (id) void queryClient.invalidateQueries({ queryKey: ["admin-user", id] });
  };

  const provision = useMutation({
    mutationFn: provisionAdminUser,
    onSuccess: (created) => {
      setCreating(false);
      setForm(EMPTY_FORM);
      setFormError(null);
      setSelectedId(created.id);
      setNotice(`${created.legal_name} provisioned — the account starts inactive.`);
      invalidate(created.id);
    },
    onError: (error) => setFormError(refusalText(error)),
  });

  const setActive = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      updateAdminUser(id, { active }),
    onSuccess: (_data, variables) => {
      setNotice(variables.active ? "Account activated." : "Account deactivated.");
      invalidate();
    },
    onError: (error) => setFormError(refusalText(error)),
  });

  const assign = useMutation({
    mutationFn: ({ id, role }: { id: string; role: AdminRoleName }) => assignAdminRole(id, role),
    onSuccess: (_data, variables) => {
      setNotice(`Role "${ADMIN_ROLE_LABELS[variables.role]}" assigned.`);
      invalidate();
    },
  });

  const revoke = useMutation({
    mutationFn: ({ id, role }: { id: string; role: AdminRoleName }) => revokeAdminRole(id, role),
    onSuccess: (_data, variables) => {
      setNotice(`Role "${ADMIN_ROLE_LABELS[variables.role]}" revoked.`);
      invalidate();
    },
  });

  function submitProvision(event: FormEvent) {
    event.preventDefault();
    if (!form.legal_name.trim()) {
      setFormError("A legal name is required.");
      return;
    }
    setFormError(null);
    provision.mutate({
      legal_name: form.legal_name.trim(),
      external_employee_id: form.external_employee_id || null,
      idp_subject: form.idp_subject || null,
      email: form.email || null,
    });
  }

  const user = detail.data;
  useEffect(() => {
    if (!user || pendingFocus.current !== user.id) return;
    pendingFocus.current = null;
    detailRef.current?.scrollIntoView?.({ block: "start" });
    detailHeadingRef.current?.focus();
  }, [user]);

  // The primary's own validity, stated in the form's words. The legal name is
  // the only required field; the email is optional but must be well-formed,
  // because a native `type="email"` mismatch would otherwise block the submit
  // silently (acceptance E3).
  const legalNameMissing = form.legal_name.trim() === "";
  const emailMalformed = form.email !== "" && !EMAIL_PATTERN.test(form.email);
  const requirement = legalNameMissing
    ? "A legal name is required."
    : emailMalformed
      ? "Email must look like name@example.com, or be empty."
      : null;
  const provisionValid = requirement === null;

  return (
    <div className="admin-users">
      <div className="admin-toolbar">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            setSubmittedSearch(search.trim());
          }}
          className="admin-search"
        >
          <label htmlFor="admin-search">Search</label>
          <span className="admin-search-row">
            <input
              id="admin-search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Name, employee ID, email, or SSO subject"
            />
            <button type="submit">Search</button>
          </span>
        </form>
        <label>
          Status
          <select
            value={activeFilter}
            onChange={(event) => setActiveFilter(event.target.value as typeof activeFilter)}
          >
            <option value="all">All</option>
            <option value="active">Active</option>
            <option value="inactive">Inactive</option>
          </select>
        </label>
        <label>
          Rows
          <select value={pageSize} onChange={(event) => setPageSize(Number(event.target.value))}>
            <option value={25}>25</option>
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={0}>All</option>
          </select>
        </label>
        <button
          type="button"
          className={creating ? "quick-action" : "primary"}
          onClick={() => setCreating((value) => !value)}
        >
          {creating ? "Close provisioning" : "Provision user"}
        </button>
      </div>

      {creating && (
        <form className="admin-create" onSubmit={submitProvision}>
          <h2>Provision user</h2>
          <p className="private-note">
            Created inactive. Contract and schedule master data is imported separately.
          </p>
          <div className="admin-form-grid">
            <label>
              Legal name *
              <input
                required
                value={form.legal_name}
                aria-invalid={legalNameMissing}
                onChange={(event) => setForm({ ...form, legal_name: event.target.value })}
              />
            </label>
            <label>
              External employee id
              <input
                value={form.external_employee_id}
                onChange={(event) => setForm({ ...form, external_employee_id: event.target.value })}
              />
            </label>
            <label>
              Identity subject
              <input
                value={form.idp_subject}
                onChange={(event) => setForm({ ...form, idp_subject: event.target.value })}
                placeholder="Mapped later if unknown"
              />
            </label>
            <label>
              Email
              <input
                type="email"
                value={form.email}
                aria-invalid={emailMalformed}
                onChange={(event) => setForm({ ...form, email: event.target.value })}
              />
            </label>
          </div>
          {formError && <p className="status-error">{formError}</p>}
          <div className="workspace-actions">
            <button
              type="submit"
              className="primary"
              disabled={provision.isPending || !provisionValid}
              aria-describedby={provisionValid ? undefined : "provision-requirement"}
            >
              {provision.isPending ? "Provisioning…" : "Provision user"}
            </button>
            {requirement && (
              <p className="field-requirement" id="provision-requirement" role="status">
                {requirement}
              </p>
            )}
            {provision.isError && !formError && (
              <p className="status-error">Provisioning failed.</p>
            )}
          </div>
        </form>
      )}

      {notice && (
        <p role="status" className="status-ok">
          {notice}
        </p>
      )}

      {users.isPending && <p role="status">Loading users…</p>}
      {users.isError && <p className="status-error">Could not load users.</p>}
      {users.isSuccess && users.data.length === 0 && (
        <p className="empty-state">No users match the current search.</p>
      )}
      {users.isSuccess && users.data.length > 0 && (
        <div className="admin-user-list-wrap">
          <table className="admin-user-list" aria-label="Users and access">
            {/* UI-021: this is tabular data, so it is a table. One run-on metadata
             * string became columns a reader can scan down, with the row action
             * in its own column. */}
            <thead>
              <tr>
                {TABLE_COLUMNS.map((column) => {
                  const key = column.key;
                  if (key === null) {
                    // Not a control, so it carries NO `aria-sort` at all: a column
                    // that cannot be ordered is not a column that is not ordered,
                    // and claiming "none" would promise a sort that does not exist.
                    return (
                      <th key={column.label} scope="col">
                        {column.label}
                      </th>
                    );
                  }
                  const active = sort === key;
                  return (
                    <th
                      key={key}
                      scope="col"
                      aria-sort={
                        active ? (direction === "asc" ? "ascending" : "descending") : "none"
                      }
                    >
                      <button type="button" className="sort-header" onClick={() => toggleSort(key)}>
                        {column.label}
                        {active && <SortChevron direction={direction} />}
                      </button>
                    </th>
                  );
                })}
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.data.map((row) => {
                const missing = missingPrerequisites(row);
                return (
                  <tr key={row.id}>
                    <td className="admin-user-name">{row.legal_name}</td>
                    <td>{row.external_employee_id ?? "—"}</td>
                    <td className={row.active ? "status-ok" : "status-muted"}>
                      {row.active ? "Active" : "Inactive"}
                    </td>
                    {/* WCAG 2.2 SC 1.4.1: readiness is text, never colour alone, and
                     *  the missing prerequisites are named one by one — a screen
                     *  reader hears "Missing SSO, Missing schedule" exactly as a
                     *  sighted reader scans it.
                     *
                     *  The tone encodes URGENCY, not completeness. An ACTIVE account
                     *  that cannot work is the support call; the same gap on an
                     *  INACTIVE account is ordinary provisioning in progress, so it
                     *  stays quiet. An inactive account with complete master data is
                     *  "Ready" — it is complete and simply switched off, which the
                     *  Status column already says, so account state is not repeated
                     *  here. */}
                    <td
                      className={
                        missing.length > 0 && row.active ? "status-warning" : "status-muted"
                      }
                    >
                      {missing.length === 0 ? "Ready" : missing.join(", ")}
                    </td>
                    <td>{row.email ?? "—"}</td>
                    <td className="admin-user-roles">
                      {row.roles.length === 0
                        ? "—"
                        : row.roles.map((role) => ADMIN_ROLE_LABELS[role] ?? role).join(", ")}
                    </td>
                    <td className="admin-user-provisioned">{formatInstant(row.created_at)}</td>
                    <td>
                      {/* WCAG 2.2 SC 4.1.2 (Name, Role, Value): the row toggle is a
                       *  toggle, so its selected state is exposed as `aria-pressed`
                       *  instead of being implied by the word on the button. The
                       *  label is the state, not a spinner: "Viewing" carries no
                       *  ellipsis, because nothing is loading — clicking another
                       *  row's View moves the selection here (acceptance A3). */}
                      <button
                        type="button"
                        aria-pressed={selectedId === row.id}
                        onClick={() => {
                          pendingFocus.current = row.id;
                          setSelectedId(row.id);
                        }}
                      >
                        {selectedId === row.id ? "Viewing" : "View"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {user && (
        <section className="admin-user-detail" ref={detailRef}>
          <div className="title-row">
            {/* Focus lands here when a row is opened, so the keyboard user arrives
             *  with the panel instead of hunting for it (tabIndex -1: focusable
             *  target, not a tab stop). */}
            <h2 ref={detailHeadingRef} tabIndex={-1}>
              {user.legal_name}
            </h2>
            <span className={user.active ? "status-ok" : "status-muted"}>
              {user.active ? "Active" : "Inactive"}
            </span>
          </div>
          <dl className="admin-detail-list">
            <div>
              <dt>External employee id</dt>
              <dd>{user.external_employee_id ?? "—"}</dd>
            </div>
            <div>
              <dt>Email</dt>
              <dd>{user.email ?? "—"}</dd>
            </div>
            <div>
              <dt>Identity subject</dt>
              <dd>{user.idp_subject ?? "not mapped"}</dd>
            </div>
            <div>
              <dt>Provisioned</dt>
              <dd>{formatInstant(user.created_at)}</dd>
            </div>
            <div>
              <dt>Profile data</dt>
              <dd>{missingMasterData(user).join(", ") || "Contract and schedule present"}</dd>
            </div>
          </dl>
          <h3>Roles</h3>
          {user.role_assignments.length === 0 && <p>No roles assigned.</p>}
          <ul className="admin-role-list">
            {user.role_assignments.map((assignment) => (
              <li key={assignment.id}>
                <strong>{ADMIN_ROLE_LABELS[assignment.role] ?? assignment.role}</strong>
                <span>
                  {assignment.revoked_at
                    ? `revoked ${formatInstant(assignment.revoked_at)}`
                    : `granted ${formatInstant(assignment.granted_at)} by ${assignment.granted_by_principal}`}
                </span>
                {!assignment.revoked_at && (
                  <ConfirmAction
                    label="Revoke"
                    confirmLabel={`Revoke ${ADMIN_ROLE_LABELS[assignment.role] ?? assignment.role}?`}
                    busy={revoke.isPending}
                    onConfirm={() => revoke.mutate({ id: user.id, role: assignment.role })}
                  />
                )}
              </li>
            ))}
          </ul>
          <div className="admin-assign-row">
            <RoleAssignSelect
              user={user}
              onAssign={(role) => assign.mutate({ id: user.id, role })}
            />
            {assign.isError && <p className="status-error">Could not assign role.</p>}
          </div>

          <h3>Effective capabilities</h3>
          {user.effective_capabilities.length === 0 ? (
            <p>None — this account has no application access.</p>
          ) : (
            <ul className="admin-cap-list">
              {user.effective_capabilities.map((capability) => (
                <li key={capability}>{capability}</li>
              ))}
            </ul>
          )}

          <div className="admin-account-actions">
            {missingPrerequisites(user).length > 0 && (
              <p className="status-muted">
                Import master data with <code>python -m app.master_data_admin import</code>; rebind
                identity with <code>python -m app.identity_admin set-binding</code>.
              </p>
            )}
            {user.active ? (
              <ConfirmAction
                label="Deactivate account"
                confirmLabel={`Deactivate ${user.legal_name}?`}
                busy={setActive.isPending}
                onConfirm={() => setActive.mutate({ id: user.id, active: false })}
              />
            ) : (
              <ConfirmAction
                label="Activate account"
                confirmLabel={`Activate ${user.legal_name}?`}
                busy={setActive.isPending}
                onConfirm={() => setActive.mutate({ id: user.id, active: true })}
              />
            )}
          </div>
          {setActive.isError && <p className="status-error">Could not update the account.</p>}
        </section>
      )}
    </div>
  );
}

function RoleAssignSelect({
  user,
  onAssign,
}: {
  user: { id: string; role_assignments: Array<{ role: AdminRoleName; revoked_at: string | null }> };
  onAssign: (role: AdminRoleName) => void;
}) {
  const [role, setRole] = useState<AdminRoleName | "">("");
  const [arming, setArming] = useState(false);
  // The armed row only exists once a role is chosen, so the announcement and the
  // focus move are keyed on that — the same behaviour every other inline confirm
  // gets, from the same hook.
  const armed = arming && role !== "";
  const armedLabel = role === "" ? "" : ADMIN_ROLE_LABELS[role];
  const { armedRef, cancelRef, restingRef, arm, cancel } = useConfirmArming(armed, setArming);
  const activeRoles = new Set(
    user.role_assignments.filter((row) => !row.revoked_at).map((row) => row.role),
  );
  const available = ALL_ADMIN_ROLES.filter((candidate) => !activeRoles.has(candidate));
  if (available.length === 0)
    return <p className="status-muted">All roles are currently assigned.</p>;
  return (
    <span className="admin-assign-controls">
      <label htmlFor="admin-role-select">Assign role</label>
      <select
        id="admin-role-select"
        value={role}
        onChange={(event) => setRole(event.target.value as AdminRoleName)}
      >
        <option value="">Select…</option>
        {available.map((candidate) => (
          <option key={candidate} value={candidate}>
            {ADMIN_ROLE_LABELS[candidate]}
          </option>
        ))}
      </select>
      <button ref={restingRef} type="button" disabled={role === ""} onClick={arm}>
        Assign
      </button>
      <ConfirmStatus arming={armed} confirmLabel={`Assign ${armedLabel}`} />
      {armed && (
        <span className="confirm-row">
          <button
            ref={armedRef}
            type="button"
            className="danger"
            onClick={() => {
              onAssign(role);
              setRole("");
              setArming(false);
            }}
          >
            Assign {armedLabel}?
          </button>
          <button ref={cancelRef} type="button" onClick={cancel}>
            Cancel
          </button>
        </span>
      )}
    </span>
  );
}
