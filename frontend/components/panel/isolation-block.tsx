import type { IsolationReport } from '@/lib/use-lexloom';
import { Row } from './primitives';

export function IsolationBlock({ isolation, tenantCount }: { isolation: IsolationReport; tenantCount: number }) {
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
              <span className="text-chalk">{probe.intruder}</span>&apos;s token, posting to{' '}
              <span className="text-chalk">{probe.target}</span>&apos;s conversation
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
              ? "Add a second tenant to send one tenant's token at the other tenant's data."
              : "Ask a question first, so there is something for the other tenant to fail to reach."}
          </p>
        )}
      </div>
    </div>
  );
}
