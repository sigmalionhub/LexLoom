'use client';

import { useEffect, useState } from 'react';
import type { RequestRecord } from '@/lib/api';
import type { IsolationReport, Tenant, Turn } from '@/lib/use-lexloom';
import type { Frame } from '@/lib/stream';
import { StreamTimeline } from './stream-timeline';

/**
 * The instrument half of the page.
 *
 * Nothing here is scripted. Every millisecond is `performance.now()` around a
 * real request or a real frame, every id is one the service returned, and the
 * isolation figures are counted off the citations that actually arrived. A
 * panel of plausible-looking invented numbers would destroy the one thing it
 * exists to establish, and the first person to open the network tab would find
 * it out.
 */

interface BackendPanelProps {
  tenant: Tenant | null;
  turn: Turn | null;
  requests: RequestRecord[];
  isolation: IsolationReport;
  tenantCount: number;
}

export function BackendPanel({ tenant, turn, requests, isolation, tenantCount }: BackendPanelProps) {
  const gap =
    turn?.timings.citations !== undefined && turn.timings.firstToken !== undefined
      ? turn.timings.firstToken - turn.timings.citations
      : null;

  return (
    <aside className="on-slate flex min-h-0 flex-col bg-slate text-chalk lg:h-full">
      <header className="shrink-0 border-b border-slate-rule px-6 pb-4 pt-6">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="font-mono text-[0.8125rem] uppercase tracking-[0.16em] text-chalk">
            Live backend
          </h2>
          {/* The model name is read off the `message_start` frame. Before a
              stream has run there is nothing to read, so this shows the same
              grey placeholder as every other unmeasured value: a plausible
              literal in a readout styled like a measurement is the exact habit
              this panel exists to avoid. */}
          <span className={`figure text-data ${turn?.model ? 'text-mist' : 'text-mist/40'}`}>
            {turn?.model ?? '—'}
          </span>
        </div>
        <p className="mt-1.5 text-[0.8125rem] leading-snug text-mist">
          Read off this session, not from a script.
        </p>
      </header>

      <div className="min-h-0 flex-1 px-6 pb-6 lg:overflow-y-auto">
        <Section label="Tenant">
          {tenant ? (
            <>
              <Row label="name">{tenant.orgName}</Row>
              <Row label="org_id" wrap>
                {tenant.orgId}
              </Row>
              <Row label="token">
                <TokenClock expiresAt={tenant.tokenExpiresAt} />
              </Row>
            </>
          ) : (
            <p className="font-mono text-data text-mist">Provisioning…</p>
          )}
        </Section>

        <Section label="Sources before the first word">
          {/* Brass means measured, so the placeholder is not brass. */}
          <p
            className={`figure text-[2.125rem] font-medium leading-none ${
              gap === null ? 'text-mist/50' : 'text-brass'
            }`}
          >
            {gap === null ? '—' : `${gap} ms`}
          </p>
          <p className="mt-2 font-mono text-data leading-relaxed text-mist">
            {gap === null
              ? 'The citations frame always precedes the first token. Ask something and the interval is measured here.'
              : `citations at ${turn?.timings.citations} ms · first token at ${turn?.timings.firstToken} ms`}
          </p>
          {turn && turn.frames.length > 0 && (
            <div className="mt-5">
              <StreamTimeline turn={turn} />
            </div>
          )}
        </Section>

        <Section label="Server-sent events, in arrival order">
          <FrameLog turn={turn} />
        </Section>

        {/* Isolation sits above the request log because it is the claim a
            reader came to check; the request log is the supporting evidence
            underneath it. */}
        <Section label="Tenant isolation">
          <IsolationBlock isolation={isolation} tenantCount={tenantCount} />
        </Section>

        <Section label="Requests" last>
          <RequestLog requests={requests} />
        </Section>
      </div>

      {/* No footer credit here on purpose. It linked to the repository and to
          docs/adr, and the repository is private — so both links answered a
          visitor with GitHub's 404 page. A demonstration that offers evidence
          and then cannot produce it is worse than one that offers none. Restore
          the links, not the byline, if and when the repository goes public. */}
    </aside>
  );
}

/* -------------------------------------------------------------------------- */

function Section({
  label,
  children,
  last = false,
}: {
  label: string;
  children: React.ReactNode;
  last?: boolean;
}) {
  return (
    <section className={last ? 'pt-5' : 'border-b border-slate-rule pb-5 pt-5'}>
      <h3 className="label-micro mb-2.5">{label}</h3>
      {children}
    </section>
  );
}

function Row({
  label,
  children,
  wrap = false,
}: {
  label: string;
  children: React.ReactNode;
  wrap?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-[3px]">
      <span className="font-mono text-data text-mist">{label}</span>
      <span
        className={`figure text-data text-chalk ${wrap ? 'break-all text-right' : 'whitespace-nowrap'}`}
      >
        {children}
      </span>
    </div>
  );
}

/** The demo token really does expire; `expires_in` from the login response says when. */
function TokenClock({ expiresAt }: { expiresAt: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const remaining = Math.max(0, Math.round((expiresAt - now) / 1000));
  if (remaining === 0) return <span className="text-alarm">expired</span>;
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  return (
    <span className={remaining < 120 ? 'text-alarm' : undefined}>
      valid {minutes}:{String(seconds).padStart(2, '0')}
    </span>
  );
}

/* -------------------------------------------------------------------------- */

interface FrameGroup {
  name: Frame['name'];
  at: number;
  until?: number;
  count: number;
}

/**
 * Forty-one consecutive `token` rows would bury the four frames whose order is
 * the point, so runs of tokens collapse into one row that keeps the count and
 * the span. Every other frame is listed individually, in the order it arrived.
 */
function groupFrames(frames: Frame[]): FrameGroup[] {
  const groups: FrameGroup[] = [];
  for (const frame of frames) {
    const last = groups[groups.length - 1];
    if (frame.name === 'token' && last?.name === 'token') {
      last.count += 1;
      last.until = frame.at;
      continue;
    }
    groups.push({ name: frame.name, at: frame.at, count: 1 });
  }
  return groups;
}

function FrameLog({ turn }: { turn: Turn | null }) {
  if (!turn || turn.frames.length === 0) {
    return <p className="font-mono text-data text-mist">No stream open.</p>;
  }

  const groups = groupFrames(turn.frames);
  return (
    <ol className="space-y-[3px]">
      {groups.map((group, index) => (
        <li
          key={`${group.name}-${index}`}
          className="flex animate-rise-in items-baseline gap-3 font-mono text-data"
        >
          <span
            aria-hidden
            className={`mt-[1px] size-[6px] shrink-0 rounded-full ${
              group.name === 'citations' ? 'bg-brass' : group.name === 'error' ? 'bg-alarm' : 'bg-chalk/55'
            }`}
          />
          <span className="min-w-0 flex-1 truncate text-chalk">
            {group.name}
            {group.count > 1 && <span className="text-mist"> ×{group.count}</span>}
          </span>
          <span className="figure shrink-0 text-mist">
            {group.until !== undefined ? `${group.at}–${group.until}` : group.at} ms
          </span>
        </li>
      ))}
      {turn.status === 'complete' && (
        <li className="flex items-baseline gap-3 pt-1 font-mono text-data text-mist">
          <span className="w-[6px] shrink-0" />
          <span className="flex-1">
            finish_reason <span className="text-chalk">{turn.finishReason}</span>
          </span>
          <span className="figure">{turn.tokens} tokens</span>
        </li>
      )}
    </ol>
  );
}

/* -------------------------------------------------------------------------- */

/** Long uuids are shortened for width; the panel still shows the full org_id above. */
function shortPath(path: string): string {
  return path
    .replace('/api/v1', '')
    .replace(/\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g, '/…');
}

function RequestLog({ requests }: { requests: RequestRecord[] }) {
  if (requests.length === 0) {
    return <p className="font-mono text-data text-mist">Nothing sent yet.</p>;
  }
  const recent = requests.slice(-7).reverse();
  return (
    <ol className="space-y-2">
      {recent.map((record) => (
        <li key={record.id} className="font-mono text-data">
          <div className="flex items-baseline gap-2">
            <span
              className={`figure shrink-0 ${
                record.status >= 200 && record.status < 300 ? 'text-chalk' : 'text-alarm'
              }`}
            >
              {record.status === 0 ? 'ERR' : record.status}
            </span>
            <span className="min-w-0 flex-1 truncate text-mist">
              <span className="text-chalk/80">{record.method}</span> {shortPath(record.path)}
            </span>
            <span className="figure shrink-0 text-mist">{record.ms} ms</span>
          </div>
          {record.requestId && (
            <div className="figure pl-8 text-[0.6875rem] text-mist/70">
              x-request-id {record.requestId.slice(0, 8)}…
              {record.code && <span className="text-alarm/80"> · {record.code}</span>}
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}

/* -------------------------------------------------------------------------- */

function IsolationBlock({ isolation, tenantCount }: { isolation: IsolationReport; tenantCount: number }) {
  const { orgsSeen, rows, tenantsAsked, foreignRows, probe, probeRunning } = isolation;
  const clean = foreignRows === 0;

  return (
    <div className="space-y-4">
      <div>
        <Row label="citation rows received">{rows}</Row>
        {/* Printed next to each other on purpose: the org_id count matching the
            number of tenants that actually asked is what says each tenant saw
            only its own rows. Either number alone would be ambiguous, and
            counting a tenant that never asked would make the pair meaningless. */}
        <Row label="tenants that asked">{tenantsAsked}</Row>
        <Row label="org_id values in them">{orgsSeen.length}</Row>
        <div className="mt-2 flex items-baseline justify-between gap-4 border-t border-slate-rule pt-3">
          <span className="font-mono text-data text-mist">rows from another tenant</span>
          <span
            className={`figure text-2xl font-medium leading-none ${clean ? 'text-brass' : 'text-alarm'}`}
          >
            {foreignRows}
          </span>
        </div>
      </div>

      <div>
        <h4 className="label-micro mb-2">Cross-tenant probe</h4>
        {probe ? (
          <>
            <p className="font-mono text-data leading-relaxed text-mist">
              <span className="text-chalk">{probe.intruder}</span>’s token, posting to{' '}
              <span className="text-chalk">{probe.target}</span>’s conversation
            </p>
            <p className="mt-1.5 font-mono text-data">
              <span className={probe.result.status === 404 ? 'text-brass' : 'text-alarm'}>
                {probe.result.status} {probe.result.code}
              </span>
              {probe.result.requestId && (
                <span className="text-mist/70"> · {probe.result.requestId.slice(0, 8)}…</span>
              )}
            </p>
            <p className="mt-1.5 font-mono text-[0.6875rem] leading-relaxed text-mist/80">
              {probe.result.status === 404
                ? 'A 404, identical to the answer for a conversation that never existed. A 403 would have confirmed it was there.'
                : 'This is not the expected answer. The boundary did not hold.'}
            </p>
          </>
        ) : probeRunning ? (
          <p className="font-mono text-data text-mist">Sending…</p>
        ) : (
          <p className="font-mono text-data leading-relaxed text-mist">
            {tenantCount < 2
              ? 'Add a second tenant to send one tenant’s token at the other tenant’s data.'
              : 'Ask a question first, so there is something for the other tenant to fail to reach.'}
          </p>
        )}
      </div>
    </div>
  );
}
