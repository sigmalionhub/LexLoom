'use client';

import type { CatalogueEntry, IngestStage } from '@/lib/api';
import type { DocumentsState } from '@/lib/use-documents';

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
 * Presentational only: the fetching, the polling and the `POST` live in
 * `lib/use-documents.ts`, because the empty state reads the same status map to
 * decide which questions are worth offering, and two callers of one hook would
 * poll the same route twice.
 *
 * Nothing here is scripted, which is the standing rule for both halves of the
 * page. Every stage, every count and every elapsed second is read off
 * `GET /api/v1/ingest/status` for this tenant, and a source this tenant has
 * never touched reports `idle` because the server says so rather than because
 * this component assumed it.
 */

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

export function Documents({
  documents,
  hasTenant,
}: {
  documents: DocumentsState;
  hasTenant: boolean;
}) {
  const { catalogue, statuses, starting, startError, start } = documents;

  if (!hasTenant || catalogue.length === 0) return null;

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

      {/* What "already loaded" actually means, said rather than implied.
          The GDPR is not sitting in some shared table this tenant is allowed to
          read — it was copied, row by row, into this tenant's own corpus when
          the tenant was created, which is the claim the isolation block on the
          right measures. Saying "preloaded" and leaving it there would let a
          reader assume the cheap version. */}
      <p className="mt-2.5 text-[0.8125rem] leading-relaxed text-ink-soft">
        A filled card is a document in this tenant’s own rows — copied there, not shared, which is
        what the isolation figures on the right are counting. The outlined one is not held yet.
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

  /**
   * Two cards, two natures, and until now one drawing.
   *
   * A document this tenant holds is a *fact*; one it does not is an
   * *invitation*. Rendering both as the same filled box made the pair read as a
   * menu of equals, and the eye had to get all the way down to the state line
   * to find out which was which.
   *
   * So the outline is the signal. Empty is a dashed rule and no fill — the same
   * treatment `components/sources.tsx` already gives "no passage matched",
   * because in this page dashed has meant *absence* since the first milestone.
   * Held is filled, with a brass edge: brass is reserved for measurements, and
   * what fills the card is a measured count of rows.
   */
  const held = stage === 'complete' || running || stage === 'error';
  const edge = stage === 'error' ? 'border-l-alarm' : 'border-l-brass';

  return (
    <li
      className={`flex flex-col rounded-sm px-3.5 py-3 ${
        held ? `border border-l-2 border-rule bg-paper-raised ${edge}` : 'border border-dashed border-rule'
      }`}
    >
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

      {/* The file itself, so a reader can check that a citation is really in the
          document rather than taking this page's word for it.
          `download` and `source_url` both come from the catalogue: the first
          because the copy step and this link must not drift, the second because
          redistribution is conditional on it. EUR-Lex permits reuse *with
          acknowledgement of source* and NIST asks for a credit, so naming the
          publisher is a licence term, not a courtesy — and it is the stronger
          evidence anyway. A file handed out by this demo proves only that the
          demo agrees with itself; the publisher's copy is what it can be
          checked against.
          Served out of the static export by nginx, never through the API: one
          worker, and SSE streams are the resource that must not queue behind a
          megabyte of PDF. */}
      <p className="mt-1.5 flex flex-wrap items-baseline gap-x-2 text-[0.75rem] leading-snug">
        {/* Rendered only when the catalogue actually named a file.
            The generated type says `download: string`, so nothing in the page
            doubts it — and the page still shipped two links to
            `/sources/undefined`, because the API answering it was older than the
            page asking. That is not only a development accident: a static export
            is copied into place before or after the API restarts, so there is
            always a window where the two disagree. A link that 404s is worse
            than no link on the one affordance that exists so a reader does not
            have to take this page's word for anything. */}
        {entry.download && (
          <>
            <a
              href={`/sources/${entry.download}`}
              download
              onClick={(event) => event.stopPropagation()}
              className="text-ink-soft underline decoration-rule underline-offset-2 transition-colors hover:text-ink hover:decoration-ink/40"
            >
              Download the {extension}
            </a>
            <span aria-hidden className="text-ink-soft/40">
              ·
            </span>
          </>
        )}
        <a
          href={entry.source_url}
          target="_blank"
          rel="noreferrer noopener"
          onClick={(event) => event.stopPropagation()}
          className="text-ink-soft underline decoration-rule underline-offset-2 transition-colors hover:text-ink hover:decoration-ink/40"
        >
          from the publisher
        </a>
      </p>

      {/* Content flows from the top; it does not get pushed to the bottom.
          It used to. That was written when the two cards held the same things
          and differed only in title length, and it stopped being true once one
          card carried an ingest button and the other a single line of count:
          the grid equalises heights, `mt-auto` then pushed that one line to the
          floor, and the card showed a hole in its middle that read as something
          having failed to load. The slack now falls below the last line, where
          it says nothing. */}
      <div className="pt-2.5">
        {/* How it got here, not only that it is here. A preloaded document and
            one the visitor ingested an hour ago both read `complete`, and the
            difference is the whole demonstration: one was given, the other was
            watched arriving. The page cannot infer which — `preloaded` comes
            down with the catalogue rather than being a source key spelled out
            in this file. */}
        {stage === 'complete' && (
          <p className="text-[0.8125rem] leading-snug text-ink-soft">
            <span className="figure text-brass">{status?.chunks_done ?? 0}</span> passages ·{' '}
            {entry.preloaded ? 'copied in at sign-in' : 'ingested by this tenant'}
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
