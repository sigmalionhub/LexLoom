'use client';

import { useEffect, useState } from 'react';
import { api, ApiError, type CatalogueEntry, type IngestStage, type IngestStatusMap } from '@/lib/api';
import type { Tenant } from '@/lib/use-lexloom';

/**
 * The documents this tenant can hold, and what each of them is doing.
 *
 * **Why this lives on paper and not in the panel.** It used to be a panel
 * section, and that was a category error: every other section of the panel
 * reports, while this one *acts* — its buttons rewrite the tenant's corpus.
 * It also has to be seen without being looked for, and the panel is the denser,
 * scrolling half. So it sits in the document column's header — above the
 * transcript, below the tenant switcher — which is `shrink-0` and therefore
 * still on screen after the tenth question. Putting it in the transcript would
 * have scrolled it away after the first one.
 *
 * Nothing here is scripted, which is the standing rule for both halves of the
 * page. Every stage, every count and every elapsed second is read off
 * `GET /api/v1/ingest/status` for this tenant, and a source this tenant has
 * never touched reports `idle` because the server says so rather than because
 * this component assumed it.
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
 * and this section rendered it straight to the visitor.
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

export function Documents({ tenant }: { tenant: Tenant | null }) {
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
    <section className="mt-5">
      <h2 className="label-micro text-ink-soft/70">Documents in this tenant</h2>

      {/* Two abreast from `sm`, because the requirement was that the second
          document be visible *beside* the first rather than below a fold. */}
      <ul className="mt-2.5 grid gap-2 sm:grid-cols-2">
        {catalogue.map((entry) => (
          <DocumentCard
            key={entry.key}
            entry={entry}
            status={statuses[entry.key]}
            starting={starting === entry.key}
            onStart={() => start(entry.key)}
          />
        ))}
      </ul>

      {startError && <p className="mt-2 text-[0.8125rem] leading-snug text-alarm">{startError}</p>}

      {/* The seed corpus, named for what it is and never counted as the
          document. A fresh tenant holds 30 excerpts of the GDPR, not the
          regulation; showing the GDPR card as "loaded" because of them would be
          the exact substitution this page exists to refuse. The number is the
          one the backend pins in four places, not a literal invented here. */}
      <p className="mt-2.5 text-[0.8125rem] leading-relaxed text-ink-soft">
        Every tenant starts with 30 seeded GDPR excerpts — pages 33–83, definitions and breach
        notification only. That is not the document: ingesting it is what puts the whole of it in
        this tenant’s corpus.
      </p>
    </section>
  );
}

/* -------------------------------------------------------------------------- */

interface DocumentCardProps {
  entry: CatalogueEntry;
  status:
    | {
        stage: IngestStage;
        chunks_done: number;
        chunks_total: number;
        error: string | null;
        started_at: number | null;
      }
    | undefined;
  starting: boolean;
  onStart: () => void;
}

function DocumentCard({ entry, status, starting, onStart }: DocumentCardProps) {
  const stage = status?.stage ?? 'idle';
  const running = stage !== 'idle' && stage !== 'complete' && stage !== 'error';
  const extension = extensionOf(entry.media_type);
  const elapsed =
    status?.started_at != null
      ? Math.max(0, Math.round(Date.now() / 1000 - status.started_at))
      : null;

  return (
    <li className="flex flex-col rounded-sm border border-rule bg-paper-raised px-3.5 py-3">
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate font-mono text-[0.8125rem] text-ink">
          {entry.key}
        </span>
        {/* The format, in the words a visitor uses. The owner asked for the
            second document to be recognisable by its extension, and a bordered
            tag says "this is a property of the file" rather than "this is a
            measurement" — brass is not spent here. */}
        <span className="shrink-0 rounded-[2px] border border-rule px-1.5 py-px font-mono text-[0.625rem] uppercase tracking-[0.09em] text-ink-soft">
          {extension}
        </span>
      </div>

      <p className="mt-1.5 line-clamp-2 text-[0.8125rem] leading-snug text-ink-soft">
        {entry.title}
      </p>

      {/* The state line is pushed to the bottom so two cards of unequal title
          length still align along the thing a reader is scanning for. */}
      <div className="mt-auto pt-2.5">
        {stage === 'complete' && (
          <p className="text-[0.8125rem] leading-snug text-ink-soft">
            <span className="figure text-brass">{status?.chunks_done ?? 0}</span> passages in this
            tenant’s corpus
          </p>
        )}

        {running && <Progress status={status} stage={stage} elapsed={elapsed} />}

        {stage === 'error' && status?.error && (
          <p className="text-[0.8125rem] leading-snug text-alarm">{errorMessage(status.error)}</p>
        )}

        {/* The state is said, not left to be inferred from the presence of a
            button. A reader has to be able to see that one document is in the
            corpus and the one beside it is not — that was the whole request. */}
        {stage === 'idle' && (
          <>
            <p className="text-[0.8125rem] leading-snug text-ink-soft">Not ingested</p>
            <button
              type="button"
              onClick={onStart}
              disabled={starting}
              className="mt-1.5 flex w-full items-baseline gap-2.5 rounded-sm border border-rule bg-paper px-3 py-2 text-left text-[0.9375rem] text-ink transition-colors hover:border-ink/35 disabled:opacity-50"
            >
              <span aria-hidden className="text-brass">
                ›
              </span>
              {starting ? 'Starting…' : `Ingest this ${extension}`}
            </button>
          </>
        )}
      </div>
    </li>
  );
}

/**
 * What the worker is doing, and how far through it is.
 *
 * The bar is filled from `chunks_done / chunks_total`, which the worker writes
 * as it writes rows — so it is a measurement and gets the brass. Before the
 * first chunk exists the fraction is genuinely zero, and the bar shows zero
 * rather than a decorative animation standing in for progress that has not
 * happened. The stage name and the elapsed seconds carry the liveness instead;
 * both change under a 500 ms poll.
 */
function Progress({
  status,
  stage,
  elapsed,
}: {
  status: DocumentCardProps['status'];
  stage: IngestStage;
  elapsed: number | null;
}) {
  const total = status?.chunks_total ?? 0;
  const done = status?.chunks_done ?? 0;
  const fraction = total > 0 ? Math.min(1, done / total) : 0;
  const step = ORDERED_STAGES.indexOf(stage) + 1;

  return (
    <div>
      <div
        role="progressbar"
        aria-label={`Ingesting, ${STAGE_LABELS[stage].toLowerCase()}`}
        aria-valuemin={0}
        aria-valuemax={total > 0 ? total : undefined}
        aria-valuenow={total > 0 ? done : undefined}
        aria-valuetext={total > 0 ? `${done} of ${total} chunks` : STAGE_LABELS[stage]}
        className="h-[3px] w-full overflow-hidden rounded-full bg-rule"
      >
        <span
          className="block h-full rounded-full bg-brass transition-[width] duration-300"
          style={{ width: `${fraction * 100}%` }}
        />
      </div>

      <p className="mt-1.5 flex flex-wrap items-baseline gap-x-2 text-[0.8125rem] leading-snug text-ink-soft">
        <span className="text-ink">{STAGE_LABELS[stage]}</span>
        {total > 0 && (
          <span className="figure text-[0.6875rem]">
            {done} / {total}
          </span>
        )}
        <span className="figure text-[0.6875rem] text-ink-soft/70">
          step {step} of {ORDERED_STAGES.length}
        </span>
        {elapsed !== null && <span className="figure text-[0.6875rem] text-ink-soft/70">{elapsed}s</span>}
      </p>
    </div>
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
