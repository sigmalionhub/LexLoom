'use client';

import { useEffect, useState } from 'react';
import { ApiError, api, type EvalsModeMetrics, type EvalsResponse } from '@/lib/api';
import type { PanelSectionProps } from './types';
import { Section } from './primitives';

/** The four ranked metrics, in the order the table shows them. */
const METRICS = [
  { key: 'precision_at_3', label: 'P@3' },
  { key: 'recall_at_5', label: 'R@5' },
  { key: 'mrr_at_10', label: 'MRR@10' },
  { key: 'ndcg_at_10', label: 'nDCG@10' },
] as const;

type MetricKey = (typeof METRICS)[number]['key'];

/**
 * The one mode whose `answerability_auc` is worth publishing.
 *
 * The other two are omitted on purpose and the omission is labelled in the
 * panel, because a number that measures nothing is more damaging than a wrong
 * one: `hybrid`'s 0.424 reads as "worse than a coin toss", which sounds like a
 * finding about the retriever and is not. Its top-1 score is an RRF value
 * structurally confined to the interval [1/61, 2/61] — with two legs and k=60,
 * the best chunk is rank 1 in at least one leg, so the score cannot fall below
 * 1/61, and it cannot beat rank 1 in both, so it cannot exceed 2/61. The whole
 * sample therefore sits inside one rank step. That is a *span*, not a two-point
 * set: `app/evals/floors.json` records 19 distinct values across the thirty
 * questions. The statistic is computed over a near-constant, which is enough to
 * make it meaningless — a two-valued score would not even reproduce the
 * published 0.4236, since 0.4236 x 144 = 61 wins and a two-valued score over
 * 24 answerable and 6 unanswerable questions can only produce 57, 60, 63 or 66
 * near there. `vector`'s is uninformative for a different reason — its
 * unanswerable scores run straight through its answerable range.
 *
 * The backend agrees and is the source of truth: `app/evals/floors.json` carries
 * an `answerability_auc` floor for `fts` alone, held by
 * `test_no_answerability_floor_is_set_on_a_mode_whose_auc_measures_nothing`. If
 * that ever changes, this changes with it.
 */
const AUC_IS_MEANINGFUL_FOR = 'fts';

/** `null` is a real answer and `0.000` is not — see the note in `api-contract.md` §1. */
function cell(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(3);
}

/** Which mode leads a column, so the reader is not asked to compare nine numbers by eye. */
function bestIn(modes: EvalsResponse['modes'], metric: MetricKey): string | null {
  let best: string | null = null;
  let bestValue = -Infinity;
  for (const [mode, values] of Object.entries(modes)) {
    const value = (values as EvalsModeMetrics)[metric];
    if (value === null || value === undefined) continue;
    if (value > bestValue) {
      bestValue = value;
      best = mode;
    }
  }
  return best;
}

/**
 * Evaluations table section.
 *
 * Reads `GET /api/v1/evals`, which serves a stored measurement rather than
 * computing one — so this fetches once on mount and never polls. There is
 * nothing to poll: the numbers were measured before the process started and
 * change only when someone commits a new `results.json`.
 *
 * Through `lib/api.ts` rather than a bare `fetch`, unlike the service-status
 * section next to it. That section polls, and its calls are kept out of the
 * panel's own request log on purpose; this one fires once, so it belongs in the
 * timeline — and going through `request()` is also what turns the `503` this
 * route answers on a build with no artefact into a message instead of an empty
 * table.
 *
 * The caption under the table is not decoration. Without the corpus size the
 * numbers describe an unknown quantity of text, and without the count of
 * deliberately unanswerable questions a `P@3` of `0.178` reads as "82% broken"
 * when six of the thirty questions are asking for a miss on purpose.
 */
export function EvalsTable(_: PanelSectionProps) {
  const [evals, setEvals] = useState<EvalsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .evals()
      .then((response) => {
        if (live) setEvals(response);
      })
      .catch((cause: unknown) => {
        if (!live) return;
        // The 503 this route answers when a build does not carry the artefact
        // has to read as "the numbers are missing from this build", never as a
        // service that scored zero.
        setError(
          cause instanceof ApiError && cause.code === 'evals_unavailable'
            ? 'not in this build'
            : 'could not be read',
        );
      });
    return () => {
      live = false;
    };
  }, []);

  if (error) {
    return (
      <Section label="Retrieval quality">
        <p className="text-[0.75rem] leading-snug text-mist">
          The measurement {error}. These numbers are a committed artefact (
          <span className="font-mono">backend/app/evals/results.json</span>); a build without it reports
          nothing rather than reporting zeros.
        </p>
      </Section>
    );
  }

  if (!evals) {
    return (
      <Section label="Retrieval quality">
        <p className="font-mono text-data text-mist">loading…</p>
      </Section>
    );
  }

  const modes = Object.keys(evals.modes);
  const sources = Object.entries(evals.corpus.sources)
    .map(([key, count]) => `${key} ${count}`)
    .join(', ');

  return (
    <Section label="Retrieval quality">
      <table className="w-full border-collapse font-mono text-data">
        <thead>
          <tr className="text-mist">
            <th className="py-[3px] text-left font-normal">mode</th>
            {METRICS.map((metric) => (
              <th key={metric.key} className="py-[3px] text-right font-normal">
                {metric.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {modes.map((mode) => (
            <tr key={mode}>
              <td className="py-[3px] text-left text-mist">{mode}</td>
              {METRICS.map((metric) => {
                const leads = bestIn(evals.modes, metric.key) === mode;
                return (
                  <td
                    key={metric.key}
                    className={`figure py-[3px] text-right ${leads ? 'text-chalk' : 'text-mist'}`}
                    aria-label={leads ? `${mode} ${metric.label} (best)` : undefined}
                  >
                    {cell(evals.modes[mode]?.[metric.key])}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>

      <p className="mt-2.5 text-[0.75rem] leading-snug text-mist">
        Answerability AUC — fts{' '}
        <span className="figure text-chalk">
          {cell(evals.modes[AUC_IS_MEANINGFUL_FOR]?.answerability_auc)}
        </span>
        . How well a mode&rsquo;s top score separates a question the corpus answers from one it does not;
        0.5 is a coin toss.
      </p>

      <p className="mt-1.5 text-[0.75rem] leading-snug text-mist">
        <span className="text-chalk">Not reported for vector or hybrid</span>, because for those two it does
        not measure anything: hybrid ranks by reciprocal-rank fusion, whose top score is confined to the
        interval 1/61&ndash;2/61 &mdash; one rank step wide, holding 19 distinct values across the thirty
        questions &mdash; so its AUC is computed over a near-constant; and vector&rsquo;s unanswerable
        questions score 0.490&ndash;0.811, straight through the 0.385&ndash;0.855 its answerable ones score.
        Printing either next to the caption above would invite reading a meaningless number as a verdict on
        the retriever.
      </p>

      <p className="mt-2 text-[0.75rem] leading-snug text-mist">
        Measured over <span className="figure text-chalk">{evals.corpus.chunks}</span> chunks ({sources})
        with <span className="figure text-chalk">{evals.questions.total}</span> hand-written questions,{' '}
        <span className="figure text-chalk">{evals.questions.unanswerable}</span> of which the corpus
        deliberately cannot answer — so precision is not expected to reach 1. Taken{' '}
        {evals.measured_at.slice(0, 10)} at commit{' '}
        <span className="font-mono">{evals.commit.slice(0, 7)}</span>, and recomputed on every push by CI,
        which fails the build if a mode falls below its floor.
      </p>
    </Section>
  );
}
