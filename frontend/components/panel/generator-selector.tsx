'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api';
import {
  DEFAULT_GENERATOR,
  GENERATORS,
  readGenerator,
  writeGenerator,
  type GeneratorChoice,
} from '@/lib/generator-choice';
import { Row, Section } from './primitives';
import type { PanelSectionProps } from './types';

/**
 * The latency figures below are a real measurement, not an estimate: each
 * model was asked one long question (~180 words requested, ~350-500 words
 * answered — a short question is not representative, see below) and timed
 * end to end, three runs each. They are deliberately a range, not a mean:
 * `gemini-3.5-flash-lite` measured 0.60 s on a 66-word answer and 14.14 s on
 * a 157-word one during this task's first pass, and reporting only one of
 * those numbers would have been a plausible-looking lie.
 *
 * Measured from this machine (2026-08-04), not from the deployed box: the
 * task that requires this figure could not reach the production deployment
 * from where it ran, and RTT to Google's API differs by host. Re-measure
 * from the deployed box and update this comment and the two `note` strings
 * together when that becomes possible.
 */
const OPTIONS: Record<GeneratorChoice, { label: string; note: string }> = {
  deterministic: { label: 'Deterministic', note: 'canned text — no external call' },
  'gemini-3.5-flash-lite': {
    label: 'Flash-Lite',
    note: '~2.2–2.5s for a long answer (measured locally, not on the deploy box)',
  },
  'gemini-3.6-flash': {
    label: 'Flash',
    note: '~9.5–12.0s for a long answer (measured locally, not on the deploy box)',
  },
};

/**
 * Generator selection section.
 *
 * The chosen value travels as a URL parameter (`lib/generator-choice.ts`),
 * the same mechanism `use-lexloom.ts` reads from when it asks — this
 * component and that hook never talk to each other directly, on purpose (see
 * `generator-choice.ts` for why a second `useLexloom()` instance here would
 * be worse).
 */
/** No key confirmed yet == assume only the one option that never needs one.
 * Starting optimistic (all three enabled) would let a visitor click a Gemini
 * option for the instant before `GET /api/v1/generators` answers, and that
 * click is exactly the "error on click" this task exists to avoid. */
const ONLY_DETERMINISTIC: ReadonlySet<GeneratorChoice> = new Set(['deterministic']);

export function GeneratorSelector({ turn }: PanelSectionProps) {
  // Starts at the default and never reads the URL during the first render:
  // this page is a static export prerendered with no query string, so
  // reading `window.location.search` in the `useState` initialiser would
  // make this render disagree with the server's and React would discard the
  // tree with a hydration error.
  const [choice, setChoice] = useState<GeneratorChoice>(DEFAULT_GENERATOR);
  useEffect(() => setChoice(readGenerator()), []);

  // What `GET /api/v1/generators` actually lists as working right now — not
  // "all three, some disabled": the endpoint answers with only the choices
  // that would succeed, so a clone with no `GEMINI_API_KEY` gets exactly one
  // back and this selector renders the other two disabled instead of ever
  // sending a click for them.
  const [available, setAvailable] = useState<ReadonlySet<GeneratorChoice>>(ONLY_DETERMINISTIC);
  useEffect(() => {
    let cancelled = false;
    api
      .listGenerators()
      .then((response) => {
        if (!cancelled) setAvailable(new Set(response.generators.map((g) => g.value)));
      })
      // A failed fetch here leaves the safe default (deterministic only) in
      // place rather than throwing — this row is a convenience, not a gate:
      // the stream endpoint refuses an unavailable choice on its own with
      // `422 generator_unavailable` either way.
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  function select(next: GeneratorChoice) {
    if (!available.has(next)) return;
    // Both, always, in one handler. `setChoice` alone would repaint this
    // button and leave the URL untouched — and the URL is what `ask()`
    // reads, so the request would keep going out for whatever was selected
    // before. That silent-toggle failure is the reason this task exists.
    writeGenerator(next);
    setChoice(next);
  }

  return (
    <Section label="Generator">
      <div className="flex flex-col gap-1.5" role="radiogroup" aria-label="Answer generator">
        {GENERATORS.map((option) => {
          const active = option === choice;
          const enabled = available.has(option);
          return (
            <button
              key={option}
              type="button"
              role="radio"
              aria-checked={active}
              aria-disabled={!enabled}
              disabled={!enabled}
              onClick={() => select(option)}
              className={[
                'flex items-baseline justify-between gap-3 rounded-md border px-2.5 py-1.5 text-left transition-colors',
                !enabled && 'cursor-not-allowed opacity-40',
                enabled && active
                  ? 'border-chalk/50 bg-slate-raised text-chalk'
                  : enabled
                    ? 'border-slate-rule text-mist hover:border-chalk/30 hover:text-chalk'
                    : 'border-slate-rule text-mist',
              ]
                .filter(Boolean)
                .join(' ')}
            >
              <span className="font-mono text-data">{OPTIONS[option].label}</span>
              <span className="figure text-micro text-mist">
                {/* Not an error on click — disabled, with the reason stated
                    right where the click would have gone. Runs with zero API
                    keys and this is why: the clone still starts, still
                    chats, and simply says why the other two are grey. */}
                {enabled ? OPTIONS[option].note : 'no Gemini API key configured on this server'}
              </span>
            </button>
          );
        })}
      </div>

      {/* Read off `message_start`, not off `choice` above: if the URL carrier
          ever breaks, the selector would still show the click that was made
          while every answer keeps coming back `deterministic` — this row is
          what makes that gap visible instead of silent. */}
      {turn?.model && (
        <Row label="last answer generated by" wrap>
          {turn.model}
        </Row>
      )}
      {/* The stream itself degraded mid-answer — Gemini's free-tier quota was
          exhausted and the server fell back rather than failing the request.
          `turn.model` above already reflects the provider that actually
          answered; this row is what explains why it does not match `choice`. */}
      {turn?.degradedFrom && (
        <Row label="fell back from" wrap>
          {turn.degradedFrom.model} ({turn.degradedFrom.reason.replaceAll('_', ' ')})
        </Row>
      )}
    </Section>
  );
}
