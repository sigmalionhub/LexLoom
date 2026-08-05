'use client';

import type { RequestRecord } from '@/lib/api';
import type { IsolationReport, Tenant, Turn } from '@/lib/use-lexloom';
import { StreamTimeline } from './stream-timeline';
import { GeneratorSelector } from './panel/generator-selector';
import { SearchMode } from './panel/search-mode';
import { IngestProgress } from './panel/ingest-progress';
import { ServiceStatus } from './panel/service-status';
import { EvalsTable } from './panel/evals-table';
import type { PanelSectionProps } from './panel/types';
import { Section, Row } from './panel/primitives';
import { TokenClock } from './panel/token-clock';
import { FrameLog } from './panel/frame-log';
import { RequestLog } from './panel/request-log';
import { IsolationBlock } from './panel/isolation-block';

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

  const panelSectionProps: PanelSectionProps = {
    tenant,
    turn,
    requests,
    isolation,
    tenantCount,
  };

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

      <div className="no-scrollbar relative min-h-0 flex-1 px-6 pb-6 lg:overflow-y-auto">
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

        <GeneratorSelector {...panelSectionProps} />
        <SearchMode {...panelSectionProps} />
        <IngestProgress {...panelSectionProps} />
        <ServiceStatus {...panelSectionProps} />
        <EvalsTable {...panelSectionProps} />

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
