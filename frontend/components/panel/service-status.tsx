'use client';

import { useEffect, useState } from 'react';
import type { components } from '@/lib/api-types';
import type { PanelSectionProps } from './types';
import { Section, Row } from './primitives';

type ServiceStatusPayload = components['schemas']['ServiceStatus'];

// Plain fetch, deliberately not the client in `lib/api.ts`. Every call that goes
// through it is reported to the panel's own REQUESTS block (`api.ts:139-142`), so
// a poll on a timer would fill the visitor's request log with the panel watching
// itself. It also keeps this task out of a file three other branches are editing.
//
// 10 s, not the 500 ms `/api/v1/ingest/status` polls at — this panel is a
// process-wide, minute-grained figure, not a progress bar, and `_NOT_MEASURED`
// in `app/observability.py` exists precisely so this poll does not become the
// figure it displays.
const POLL_MS = 10_000;

/**
 * Service status section.
 *
 * Reads `GET /api/v1/status`: the running commit, uptime, aggregate request
 * counts and two latency distributions (ordinary requests and SSE stream
 * duration, kept apart because a stream's duration is mostly the model). See
 * `backend/app/observability.py` for what is measured and why, and
 * `docs/api-contract.md` §9 for what this payload deliberately does not carry
 * — no stream occupancy, nothing per-organisation.
 */
export function ServiceStatus(_: PanelSectionProps) {
  const [status, setStatus] = useState<ServiceStatusPayload | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const load = () => {
      // A hidden tab polls nothing. The demo box is shared with someone else's
      // production, and a tab left open for a day is otherwise 8640 requests.
      if (document.visibilityState !== 'visible') return;
      fetch('/api/v1/status', { signal: controller.signal })
        .then((r) => (r.ok ? r.json() : null))
        .then(setStatus)
        .catch(() => {});
    };
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      controller.abort();
      clearInterval(id);
    };
  }, []);

  // `null` renders as the same grey placeholder every unmeasured value in this
  // panel uses. `backend-panel.tsx` is explicit about why: "a panel of
  // plausible-looking invented numbers would destroy the one thing it exists to
  // establish", and 0 ms for a window with no traffic is exactly such a number.
  const ms = (value: number | null | undefined, above: number, count: number) =>
    value !== null && value !== undefined ? `${value} ms` : count > 0 ? `> ${above} ms` : '—';

  return (
    <Section label="Service">
      <Row label="commit">{status?.commit ?? '—'}</Row>
      <Row label="uptime">{status ? `${Math.floor(status.uptime_seconds / 60)} min` : '—'}</Row>
      <Row label="requests / 5 min">{status?.requests.total ?? '—'}</Row>
      <Row label="time to first byte p50">
        {ms(
          status?.time_to_response_start.p50_ms,
          status?.time_to_response_start.above_ms ?? 0,
          status?.time_to_response_start.above_count ?? 0
        )}
      </Row>
      <Row label="stream duration p50">
        {ms(
          status?.stream_duration.p50_ms,
          status?.stream_duration.above_ms ?? 0,
          status?.stream_duration.above_count ?? 0
        )}
      </Row>
      <p className="mt-2 text-[0.75rem] leading-snug text-mist">
        Streams are timed separately and excluded from the request figures: a stream is open for
        as long as the model talks, and counting that as latency would make this number mean
        &ldquo;how long you watched&rdquo;. n={status?.time_to_response_start.samples ?? 0}.
      </p>
    </Section>
  );
}
