'use client';

import type { Tenant } from '@/lib/use-lexloom';

/**
 * The switcher is the login.
 *
 * `POST /api/v1/auth/demo-login` needs no credentials and mints a throwaway
 * organisation with its own copy of the corpus, so the visitor is signed in
 * before they notice. Pressing "Add a tenant" a second time is what turns the
 * page from a chat demo into an isolation demo: two tenants, two tokens, and a
 * transcript that does not follow you across the boundary — because the API has
 * no way to show you one tenant's conversation while holding the other's token.
 */

interface OrgSwitcherProps {
  tenants: Tenant[];
  activeOrgId: string | null;
  onSwitch: (orgId: string) => void;
  onAdd: () => void;
  creating: boolean;
  disabled: boolean;
}

export function OrgSwitcher({
  tenants,
  activeOrgId,
  onSwitch,
  onAdd,
  creating,
  disabled,
}: OrgSwitcherProps) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {tenants.map((tenant) => {
        const active = tenant.orgId === activeOrgId;
        return (
          <button
            key={tenant.orgId}
            type="button"
            onClick={() => onSwitch(tenant.orgId)}
            aria-pressed={active}
            className={[
              'group flex items-baseline gap-2 rounded-full border px-3.5 py-1.5 transition-colors',
              active
                ? 'border-ink bg-ink text-paper'
                : 'border-rule bg-transparent text-ink-soft hover:border-ink/40 hover:text-ink',
            ].join(' ')}
          >
            <span className="text-[0.9375rem] leading-none">{tenant.orgName}</span>
            <span
              className={`figure text-[0.6875rem] leading-none ${active ? 'text-paper/55' : 'text-ink-soft/55'}`}
            >
              {tenant.orgId.slice(0, 8)}
            </span>
          </button>
        );
      })}

      {tenants.length < 3 && (
        <button
          type="button"
          onClick={onAdd}
          disabled={creating || disabled}
          className="rounded-full border border-dashed border-rule px-3.5 py-1.5 text-[0.9375rem] leading-none text-ink-soft transition-colors hover:border-ink/40 hover:text-ink disabled:opacity-45"
        >
          {creating ? 'Creating…' : tenants.length === 0 ? 'Get a tenant' : 'Add a tenant'}
        </button>
      )}
    </div>
  );
}
