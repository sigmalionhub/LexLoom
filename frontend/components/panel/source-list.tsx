'use client';

import { useEffect, useState } from 'react';
import { api, ApiError, type CatalogueEntry, type IngestStage, type IngestStatusMap } from '@/lib/api';
import { Section } from './primitives';
import type { PanelSectionProps } from './types';

/**
 * The documents this tenant can hold, and what each of them is doing.
 *
 * This replaces a section that named one source in a constant and explained,
 * at length, why a catalogue route would be a mistake. That reasoning was
 * right for one document and wrong for two: hard-coding the pair here would
 * put the catalogue in two places that drift, so the list is fetched.
 *
 * Nothing here is scripted, which is the panel's standing rule. Every stage,
 * every count and every elapsed second is read off `GET /api/v1/ingest/status`
 * for this tenant, and a source this tenant has never touched reports `idle`
 * because the server says so rather than because this component assumed it.
 */

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
  idle: 'Not ingested',
  queued: 'Queued',
  parsing: 'Parsing',
  chunking: 'Chunking',
  embedding: 'Embedding',
  indexing: 'Writing to the corpus',
  complete: 'Complete',
  error: 'Failed',
};

/**
 * The `error` field is a stable code, never a sentence — `app/worker/tasks.py`
 * stopped putting `str(exc)` in the progress hash, because a failing INSERT's
 * own string carried the statement, its bound parameters and the document text,
 * and this panel rendered it straight to the visitor.
 *
 * An unknown code falls through to the raw value rather than to a generic
 * "something went wrong": a code this table has not caught up with is still
 * more use on screen than nothing, and it is short by construction.
 */
const ERROR_MESSAGES: Record<string, string> = {
  ingest_failed: 'The ingest failed. The details are in the server log.',
  ingest_timeout: 'The ingest ran past its time limit and was stopped.',
  ingest_abandoned: 'This demo tenant expired while the ingest was running.',
  already_ingested: 'This tenant already holds this document.',
  capacity_unavailable: 'No ingest capacity right now, or this tenant is about to expire.',
};

function errorMessage(code: string): string {
  return ERROR_MESSAGES[code] ?? code;
}

function errorDetail(error: unknown): string {
  if (error instanceof ApiError) return error.problem.detail ?? error.message;
  return String(error);
}

/**
 * Plain fetch, deliberately not `api.ingestStatus` — the same reasoning as
 * `panel/service-status.tsx`'s own poll: every call through `lib/api.ts` is
 * reported to the panel's REQUESTS block, and a poll every 500 ms would fill a
 * visitor's request log with this section watching itself. The one-shot `POST`
 * that starts an ingest is a real, rare, visitor-initiated action and does go
 * through `api.startIngest`.
 */
async function pollStatus(token: string): Promise<IngestStatusMap | null> {
  try {
    const response = await fetch('/api/v1/ingest/status', {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) return null;
    return (await response.json()) as IngestStatusMap;
  } catch {
    return null;
  }
}

export function SourceList({ tenant }: PanelSectionProps) {
  const token = tenant?.token ?? null;
  const orgId = tenant?.orgId ?? null;

  const [catalogue, setCatalogue] = useState<CatalogueEntry[]>([]);
  const [statuses, setStatuses] = useState<IngestStatusMap>({});
  const [starting, setStarting] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);

  // The catalogue is a property of the deployment, not of the tenant, so it is
  // fetched once and never re-fetched on a tenant switch.
  useEffect(() => {
    let cancelled = false;
    api
      .catalogue()
      .then((entries) => {
        if (!cancelled) setCatalogue(entries);
      })
      .catch(() => {
        /* The section renders nothing rather than a guess; see below. */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // One read per organisation, not a poll: a status this section did not itself
  // start — the same tenant reached again after a re-render — still has to show
  // what actually happened rather than default to "not ingested" until a click.
  useEffect(() => {
    setStatuses({});
    setStartError(null);
    if (!token) return;
    let cancelled = false;
    pollStatus(token).then((body) => {
      if (!cancelled && body) setStatuses(body);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId]);

  const inFlight = Object.values(statuses).some(
    (status) => status.stage !== 'idle' && status.stage !== 'complete' && status.stage !== 'error',
  );

  useEffect(() => {
    if (!token || !inFlight) return;
    const id = setInterval(() => {
      // A hidden tab polls nothing — the demo box is shared, and a tab left
      // open for a day is otherwise over a hundred thousand requests.
      if (document.visibilityState !== 'visible') return;
      pollStatus(token).then((body) => {
        if (body) setStatuses(body);
      });
    }, POLL_MS);
    return () => clearInterval(id);
  }, [token, inFlight]);

  async function start(sourceKey: string) {
    if (!token) return;
    setStarting(sourceKey);
    setStartError(null);
    try {
      const status = await api.startIngest(token, sourceKey);
      setStatuses((current) => ({ ...current, [sourceKey]: status }));
    } catch (error) {
      setStartError(errorDetail(error));
    } finally {
      setStarting(null);
    }
  }

  if (!tenant || catalogue.length === 0) return null;

  return (
    <Section label="Documents">
      <ul className="flex flex-col gap-2">
        {catalogue.map((entry) => (
          <SourceRow
            key={entry.key}
            entry={entry}
            status={statuses[entry.key]}
            starting={starting === entry.key}
            onStart={() => start(entry.key)}
          />
        ))}
      </ul>

      {startError && <p className="mt-2 text-[0.75rem] leading-snug text-alarm">{startError}</p>}

      {/* The seed corpus, named for what it is and never counted as the
          document. A fresh tenant holds 30 excerpts of the GDPR, not the
          regulation; showing the GDPR row as "loaded" because of them would be
          the exact substitution this panel exists to refuse. The number is the
          one the backend pins in four places, not a literal invented here. */}
      <p className="mt-3 text-[0.75rem] leading-relaxed text-mist">
        Every tenant starts with 30 seeded GDPR excerpts — pages 33–83, definitions and breach
        notification only. That is not the document: ingesting it is what puts the whole of it in
        this tenant’s corpus.
      </p>
    </Section>
  );
}

/* -------------------------------------------------------------------------- */

interface SourceRowProps {
  entry: CatalogueEntry;
  status: { stage: IngestStage; chunks_done: number; chunks_total: number; error: string | null; started_at: number | null } | undefined;
  starting: boolean;
  onStart: () => void;
}

function SourceRow({ entry, status, starting, onStart }: SourceRowProps) {
  const stage = status?.stage ?? 'idle';
  const stageIndex = ORDERED_STAGES.indexOf(stage);
  const running = stage !== 'idle' && stage !== 'complete' && stage !== 'error';
  const elapsed =
    status?.started_at != null ? Math.max(0, Math.round(Date.now() / 1000 - status.started_at)) : null;

  return (
    <li className="border border-slate-rule px-2.5 py-2">
      <div className="flex items-baseline justify-between gap-3">
        <span className="min-w-0 font-mono text-data text-chalk">{entry.key}</span>
        <span className="figure shrink-0 text-micro uppercase tracking-wide text-mist">
          {extensionOf(entry.media_type)}
        </span>
      </div>
      <p className="mt-1 line-clamp-2 text-[0.75rem] leading-snug text-mist">{entry.title}</p>

      <div className="mt-2 flex items-baseline justify-between gap-3">
        <span
          className={`font-mono text-data ${
            stage === 'error' ? 'text-alarm' : stage === 'complete' ? 'text-brass' : 'text-mist'
          }`}
        >
          {STAGE_LABELS[stage]}
        </span>
        {status && status.chunks_total > 0 && (
          <span className="figure text-micro text-mist">
            {status.chunks_done} / {status.chunks_total}
          </span>
        )}
        {elapsed !== null && stage !== 'idle' && (
          <span className="figure text-micro text-mist">{elapsed}s</span>
        )}
      </div>

      {running && (
        <div className="mt-2 flex flex-col gap-0.5">
          {ORDERED_STAGES.map((step, index) => (
            <div key={step} className="flex items-baseline justify-between gap-3">
              <span
                className={`font-mono text-micro ${
                  step === stage
                    ? 'text-chalk'
                    : stageIndex >= 0 && index <= stageIndex
                      ? 'text-mist'
                      : 'text-mist/40'
                }`}
              >
                {STAGE_LABELS[step]}
              </span>
              {step === stage && <span className="figure text-micro text-mist">current</span>}
            </div>
          ))}
        </div>
      )}

      {status?.error && (
        <p className="mt-1.5 text-[0.75rem] leading-snug text-alarm">{errorMessage(status.error)}</p>
      )}

      {stage === 'idle' && (
        <button
          type="button"
          onClick={onStart}
          disabled={starting}
          className="mt-2 w-full rounded-md border border-slate-rule px-2.5 py-1.5 text-left font-mono text-data text-chalk transition-colors hover:border-chalk/30 disabled:opacity-50"
        >
          {starting ? 'Starting…' : `Ingest this ${extensionOf(entry.media_type)}`}
        </button>
      )}
    </li>
  );
}

/**
 * What a visitor calls the format. The media type is what the server dispatches
 * on — see `Source.media_type` — but nobody reads
 * `vnd.openxmlformats-officedocument.wordprocessingml.document` and thinks
 * "Word file". Unknown types fall through to the raw value rather than to a
 * guess.
 */
function extensionOf(mediaType: string): string {
  if (mediaType === 'application/pdf') return 'PDF';
  if (mediaType.includes('wordprocessingml')) return 'DOCX';
  return mediaType;
}
