'use client';

import { useEffect, useRef, useState } from 'react';
import type { Tenant, Turn } from '@/lib/use-lexloom';
import { OrgSwitcher } from './org-switcher';
import { Sources } from './sources';

/**
 * Questions that return citations against the seeded corpus. Retrieval is
 * Postgres full-text search rather than an embedding lookup, so a typed
 * question in ordinary words works too — these are a starting point, not a
 * menu of the only things the demo can answer.
 */
const SUGGESTIONS = [
  'What are the processor’s obligations?',
  'How long do we have to report a breach?',
  'When is a data protection impact assessment required?',
];

interface ChatProps {
  tenants: Tenant[];
  activeTenant: Tenant | null;
  turns: Turn[];
  busy: boolean;
  creatingTenant: boolean;
  fatal: { code: string; message: string } | null;
  /** True when another tenant in this session has a transcript and this one does not. */
  elsewhereHasHistory: boolean;
  onAsk: (question: string) => void;
  onCancel: () => void;
  onAddTenant: () => void;
  onSwitchTenant: (orgId: string) => void;
}

export function Chat({
  tenants,
  activeTenant,
  turns,
  busy,
  creatingTenant,
  fatal,
  elsewhereHasHistory,
  onAsk,
  onCancel,
  onAddTenant,
  onSwitchTenant,
}: ChatProps) {
  const [draft, setDraft] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const last = turns.length > 0 ? turns[turns.length - 1] : null;
  const answerLength = last?.answer.length ?? 0;

  // Follow the answer as it types, but stop following the moment the reader
  // scrolls up to look at something.
  useEffect(() => {
    const container = scrollRef.current;
    if (!container) return;
    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    if (distanceFromBottom < 160) container.scrollTop = container.scrollHeight;
  }, [answerLength, turns.length, activeTenant?.orgId]);

  const submit = (question: string) => {
    if (!question.trim() || busy || !activeTenant) return;
    setDraft('');
    onAsk(question);
  };

  return (
    // Below lg the column simply grows and the page scrolls; from lg it becomes
    // a pane with its own scroll, so the composer and the panel stay in view.
    <div className="flex min-h-0 flex-col bg-paper lg:h-full">
      <header className="shrink-0 border-b border-rule px-6 pb-5 pt-7 sm:px-10">
        {/* One measure for the whole document column: header, transcript and
            composer share it, so the page reads as a page rather than as three
            stacked panels. */}
        <div className="mx-auto w-full max-w-[70ch]">
          <div className="flex items-baseline gap-4">
            <h1 className="font-serif text-[1.75rem] leading-none tracking-[-0.015em] text-ink">LexLoom</h1>
            <span aria-hidden className="h-px flex-1 bg-rule" />
          </div>
          <p className="mt-2.5 text-[0.9375rem] leading-snug text-ink-soft">
            Ask the GDPR corpus in your tenant. Sources arrive before the first word — the panel measures by
            how much.
          </p>
          <div className="mt-4">
            <OrgSwitcher
              tenants={tenants}
              activeOrgId={activeTenant?.orgId ?? null}
              onSwitch={onSwitchTenant}
              onAdd={onAddTenant}
              creating={creatingTenant}
              disabled={busy}
            />
          </div>
        </div>
      </header>

      <div ref={scrollRef} className="min-h-0 flex-1 px-6 py-8 sm:px-10 lg:overflow-y-auto">
        <div className="mx-auto w-full max-w-[70ch]">
          {fatal && (
            <div className="mb-7 border-l-2 border-alarm bg-paper-raised px-4 py-3">
              <p className="text-[0.9375rem] leading-relaxed text-ink">{fatal.message}</p>
              <p className="figure mt-1.5 text-[0.6875rem] text-ink-soft/80">{fatal.code}</p>
            </div>
          )}

          {turns.length === 0 ? (
            <EmptyState
              tenant={activeTenant}
              elsewhereHasHistory={elsewhereHasHistory}
              onPick={submit}
              disabled={busy || !activeTenant}
            />
          ) : (
            <div className="space-y-12">
              {turns.map((turn) => (
                <TurnView key={turn.id} turn={turn} />
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="shrink-0 border-t border-rule bg-paper px-6 py-4 sm:px-10">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            submit(draft);
          }}
          className="mx-auto flex w-full max-w-[70ch] items-center gap-2"
        >
          <input
            ref={inputRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={activeTenant ? 'Ask a question' : 'Waiting for a tenant…'}
            aria-label="Ask a question"
            maxLength={8000}
            disabled={!activeTenant}
            className="min-w-0 flex-1 rounded-sm border border-rule bg-paper-raised px-3.5 py-2.5 text-[1.0625rem] text-ink placeholder:text-ink-soft/55 focus:border-ink/30 disabled:opacity-60"
          />
          {busy ? (
            <button
              type="button"
              onClick={onCancel}
              className="shrink-0 rounded-sm border border-rule px-4 py-2.5 text-[0.9375rem] text-ink-soft transition-colors hover:border-ink/40 hover:text-ink"
            >
              Stop
            </button>
          ) : (
            <button
              type="submit"
              disabled={!draft.trim() || !activeTenant}
              className="shrink-0 rounded-sm bg-ink px-5 py-2.5 text-[0.9375rem] text-paper transition-opacity disabled:opacity-35"
            >
              Ask
            </button>
          )}
        </form>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function EmptyState({
  tenant,
  elsewhereHasHistory,
  onPick,
  disabled,
}: {
  tenant: Tenant | null;
  elsewhereHasHistory: boolean;
  onPick: (question: string) => void;
  disabled: boolean;
}) {
  return (
    <div>
      {elsewhereHasHistory && tenant && (
        <p className="mb-7 border-l-2 border-brass bg-paper-raised px-4 py-3 text-[0.9375rem] leading-relaxed text-ink-soft">
          <span className="text-ink">{tenant.orgName}</span> has an empty transcript. The other tenant’s
          conversation is not hidden from this view — it is unreachable with this token, and asking for it
          by id returns the same 404 as a conversation that never existed.
        </p>
      )}

      <p className="font-serif text-[1.25rem] leading-snug text-ink">
        Ask something, and watch the sources land first.
      </p>

      <ul className="mt-5 space-y-2">
        {SUGGESTIONS.map((question) => (
          <li key={question}>
            <button
              type="button"
              disabled={disabled}
              onClick={() => onPick(question)}
              className="group flex w-full items-baseline gap-2.5 rounded-sm border border-rule bg-paper-raised px-3.5 py-2.5 text-left text-[1rem] text-ink transition-colors hover:border-ink/35 disabled:opacity-50"
            >
              <span aria-hidden className="text-brass">
                ›
              </span>
              {question}
            </button>
          </li>
        ))}
      </ul>

      <p className="mt-5 text-[0.875rem] leading-relaxed text-ink-soft">
        Or type your own. Retrieval is Postgres full-text search over this tenant’s private copy of the
        corpus, so ordinary wording works — and a question the corpus does not cover returns no sources
        rather than inventing one.
      </p>

      <CallSequence />
    </div>
  );
}

/**
 * What the page is about to do, listed before it does it.
 *
 * It fills the opening screen with the thing a backend reader actually wants —
 * the shape of the exchange — and it sets up the panel: the same five calls
 * appear there afterwards with their real statuses, durations and request ids.
 */
function CallSequence() {
  const calls: Array<[string, string, string]> = [
    ['POST', '/auth/demo-login', 'already made — that is your tenant'],
    ['POST', '/conversations', '201, committed before it answers'],
    ['POST', '/conversations/{id}/messages', '202 — nothing generated yet'],
    ['POST', '/auth/stream-tickets', '60 s, this one message'],
    ['GET', '…/messages/{id}/stream', 'text/event-stream'],
  ];

  return (
    <section className="mt-9 border-t border-rule pt-5">
      <h3 className="label-micro text-ink-soft/70">The calls behind one answer · /api/v1</h3>
      <ol className="mt-3 space-y-1.5">
        {calls.map(([method, path, note]) => (
          <li key={path} className="flex flex-wrap items-baseline gap-x-3 font-mono text-[0.75rem]">
            <span className="w-9 shrink-0 text-ink-soft/60">{method}</span>
            <span className="text-ink">{path}</span>
            <span className="text-ink-soft/60">{note}</span>
          </li>
        ))}
      </ol>
      <p className="mt-3 text-[0.875rem] leading-relaxed text-ink-soft">
        The extra step is the ticket. <code className="font-mono text-[0.8125rem]">EventSource</code> is
        GET-only and cannot set an <code className="font-mono text-[0.8125rem]">Authorization</code> header,
        so the stream is opened with a short-lived credential bound to a single message instead of putting a
        bearer token in a URL.
      </p>
    </section>
  );
}

/* -------------------------------------------------------------------------- */

function TurnView({ turn }: { turn: Turn }) {
  const streaming = turn.status === 'streaming' || turn.status === 'sending';

  return (
    <article>
      <h2 className="font-serif text-[1.375rem] leading-snug tracking-[-0.01em] text-ink">
        {turn.question}
      </h2>

      {turn.citations !== null && (
        <div className="mt-4">
          <p className="label-micro mb-2 text-ink-soft/70">
            Sources · {turn.citations.length} · arrived at {turn.timings.citations} ms
          </p>
          <Sources citations={turn.citations} orgId={turn.orgId} />
        </div>
      )}

      {(turn.answer || streaming) && (
        <p className="mt-5 whitespace-pre-wrap font-serif text-[1.0625rem] leading-[1.65] text-ink">
          {turn.answer}
          {streaming && (
            <span aria-hidden className="ml-px animate-caret text-brass">
              ▌
            </span>
          )}
        </p>
      )}

      {turn.serverError && (
        <p className="mt-4 border-l-2 border-alarm bg-paper-raised px-4 py-3 text-[0.9375rem] leading-relaxed text-ink-soft">
          {turn.serverError.message}
          <span className="figure block pt-1 text-[0.6875rem] text-ink-soft/70">
            {turn.serverError.code}
          </span>
        </p>
      )}

      {turn.failure && (
        <p className="mt-4 border-l-2 border-alarm bg-paper-raised px-4 py-3 text-[0.9375rem] leading-relaxed text-ink-soft">
          {turn.failure.summary}
          {(turn.failure.code || turn.failure.requestId) && (
            <span className="figure block pt-1 text-[0.6875rem] text-ink-soft/70">
              {turn.failure.code}
              {turn.failure.requestId && ` · x-request-id ${turn.failure.requestId}`}
            </span>
          )}
        </p>
      )}

      {turn.status === 'cancelled' && (
        <p className="mt-3 text-[0.875rem] text-ink-soft">
          Stopped. Nothing was stored, so this question can be answered again.
        </p>
      )}
    </article>
  );
}
