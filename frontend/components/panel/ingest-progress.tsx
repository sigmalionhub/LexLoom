'use client';

import { useEffect, useState } from 'react';
import { api, ApiError, type IngestStage, type IngestStatusResponse } from '@/lib/api';
import { Row, Section } from './primitives';
import type { PanelSectionProps } from './types';

/**
 * Ingestion progress section.
 *
 * `backend/app/ingest/catalogue.py` carries exactly one source today, and the
 * two routes this section calls (`POST /api/v1/ingest`, `GET
 * .../ingest/status`) deliberately have no third route that lists the
 * catalogue — see task-24-brief.md, Step 2, on why a `job_id` nobody consumes
 * was removed rather than kept "for later"; a `GET /api/v1/catalogue` built
 * for this section's one caller and one value would be the same mistake
 * again. So the key and its title are named here, next to the one place that
 * reads them, rather than fetched.
 */
const SOURCE_KEY = 'gdpr-full';
const SOURCE_TITLE = 'the GDPR (Regulation (EU) 2016/679)';

/**
 * A question the full 88-page regulation answers and the 30-excerpt seed
 * corpus (`backend/seed/gdpr_excerpts.json` — definitions, principles and
 * breach notification only) does not. Verified during this task's own Step 9
 * smoke test: asked for real, against a real ingest, and answered with a
 * citation from a page outside the seed set. A question the seed corpus also
 * answers would prove nothing — the whole point of asking it is that the
 * seed corpus cannot have supplied the citation.
 */
const SUGGESTED_QUESTION = 'What conditions govern transfers of personal data to a third country?';

// Twice a second — the panel's own justification for why `GET .../status` is
// excluded from the request histogram (`app/observability.py`'s
// `_NOT_MEASURED`, checked rather than trusted by
// `test_the_polled_status_route_is_not_in_the_request_histogram`). Polling
// only while a job is actually in flight, and only while the tab is visible,
// is what keeps this rate from being worse than the exclusion assumes.
const POLL_MS = 500;

const ORDERED_STAGES: IngestStage[] = [
  'queued',
  'parsing',
  'chunking',
  'embedding',
  'indexing',
  'complete',
];

const STAGE_LABELS: Record<IngestStage, string> = {
  idle: 'Not started',
  queued: 'Queued',
  parsing: 'Parsing the PDF',
  chunking: 'Chunking',
  embedding: 'Embedding',
  indexing: 'Writing to the corpus',
  complete: 'Complete',
  error: 'Failed',
};

function errorDetail(error: unknown): string {
  if (error instanceof ApiError) return error.problem.detail ?? error.message;
  return String(error);
}

/**
 * Plain fetch, deliberately not `api.ingestStatus` — the same reasoning as
 * `panel/service-status.tsx`'s own poll: every call through `lib/api.ts` is
 * reported to the panel's own REQUESTS block, and a poll every 500 ms would
 * fill a visitor's request log with this section watching itself. The
 * one-shot `POST` that starts the ingest is a real, rare, visitor-initiated
 * action and does go through `api.startIngest` — see below.
 */
async function pollStatus(token: string): Promise<IngestStatusResponse | null> {
  try {
    const response = await fetch('/api/v1/ingest/status', {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) return null;
    return (await response.json()) as IngestStatusResponse;
  } catch {
    return null;
  }
}

export function IngestProgress({ tenant }: PanelSectionProps) {
  const token = tenant?.token ?? null;
  const orgId = tenant?.orgId ?? null;

  const [status, setStatus] = useState<IngestStatusResponse | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  // One check per organisation, not a poll: a status this section did not
  // itself start (the same tenant, reached again after a re-render) still
  // has to render what actually happened rather than default to "not
  // started" until a click.
  useEffect(() => {
    setStatus(null);
    setStartError(null);
    if (!token) return;
    let cancelled = false;
    pollStatus(token).then((body) => {
      if (!cancelled && body) setStatus(body);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId]);

  // Polls only while a job is actually in flight for this organisation —
  // never before a start, never once the job has reached a terminal stage.
  const inFlight = status !== null && status.stage !== 'idle' && status.stage !== 'complete' && status.stage !== 'error';

  useEffect(() => {
    if (!token || !inFlight) return;
    const id = setInterval(() => {
      // A hidden tab polls nothing — the demo box is shared, and a tab left
      // open for a day is otherwise over a hundred thousand requests.
      if (document.visibilityState !== 'visible') return;
      pollStatus(token).then((body) => {
        if (body) setStatus(body);
      });
    }, POLL_MS);
    return () => clearInterval(id);
  }, [token, inFlight]);

  async function start() {
    if (!token) return;
    setStarting(true);
    setStartError(null);
    try {
      setStatus(await api.startIngest(token, SOURCE_KEY));
    } catch (error) {
      setStartError(errorDetail(error));
    } finally {
      setStarting(false);
    }
  }

  if (!tenant) return null;

  const stage = status?.stage ?? 'idle';
  const canStart = stage === 'idle' && !starting;
  const stageIndex = ORDERED_STAGES.indexOf(stage);
  const elapsedSeconds =
    status?.started_at != null ? Math.max(0, Math.round(Date.now() / 1000 - status.started_at)) : null;

  return (
    <Section label="Ingest">
      <Row label="source" wrap>
        {SOURCE_TITLE}
      </Row>

      {stage === 'error' ? (
        <Row label="stage" wrap>
          <span className="text-alarm">{STAGE_LABELS.error}</span>
        </Row>
      ) : (
        <div className="flex flex-col gap-1 py-1">
          {ORDERED_STAGES.map((step, index) => {
            const reached = stageIndex >= 0 && index <= stageIndex;
            const current = step === stage;
            return (
              <div key={step} className="flex items-baseline justify-between gap-3">
                <span
                  className={`font-mono text-data ${current ? 'text-chalk' : reached ? 'text-mist' : 'text-mist/40'}`}
                >
                  {STAGE_LABELS[step]}
                </span>
                {current && <span className="figure text-micro text-mist">current</span>}
              </div>
            );
          })}
        </div>
      )}

      {status && status.chunks_total > 0 && (
        <Row label="chunks">
          {status.chunks_done} / {status.chunks_total}
        </Row>
      )}
      {elapsedSeconds !== null && stage !== 'idle' && <Row label="elapsed">{elapsedSeconds}s</Row>}
      {status?.error && (
        <Row label="error" wrap>
          <span className="text-alarm">{status.error}</span>
        </Row>
      )}

      {canStart && (
        <button
          type="button"
          onClick={start}
          className="mt-2 w-full rounded-md border border-slate-rule px-2.5 py-1.5 text-left font-mono text-data text-chalk transition-colors hover:border-chalk/30"
        >
          Ingest {SOURCE_TITLE}
        </button>
      )}
      {stage === 'idle' && starting && (
        <p className="mt-2 font-mono text-data text-mist">Starting…</p>
      )}
      {startError && <p className="mt-2 text-[0.75rem] leading-snug text-alarm">{startError}</p>}

      {stage === 'complete' && (
        <p className="mt-2 text-[0.75rem] leading-snug text-mist">
          Now try asking about {SOURCE_TITLE} — for example, &ldquo;{SUGGESTED_QUESTION}&rdquo;. The
          seed corpus does not cover this, so a real citation here can only have come from what was
          just ingested.
        </p>
      )}
    </Section>
  );
}
