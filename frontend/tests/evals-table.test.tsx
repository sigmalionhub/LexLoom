import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EvalsTable } from '@/components/panel/evals-table';
import type { PanelSectionProps } from '@/components/panel/types';

/**
 * The evals section reads a *stored* measurement, and the two things worth
 * testing are the two a reader could be misled by:
 *
 *   1. a `503` must read as "the numbers are not in this build", never as a
 *      table of zeros — a panel showing 0.000 across the board says retrieval
 *      is broken, which would be a false claim about a working service;
 *   2. a metric that is `null` must render as an em dash, because `null` here
 *      means "undefined on these questions", not "scored nothing".
 *
 * Rendered through the real component and the real `lib/api.ts` client, with
 * only `fetch` replaced. Mocking `api.evals()` instead would keep passing with
 * the request removed from the component entirely.
 */

function panelProps(): PanelSectionProps {
  return {
    tenant: null,
    turn: null,
    requests: [],
    isolation: {
      orgsSeen: [],
      rows: 0,
      tenantsAsked: 0,
      foreignRows: 0,
      probe: null,
      probeRunning: false,
    },
    tenantCount: 0,
  };
}

function jsonResponse(body: unknown, status = 200, contentType = 'application/json'): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'content-type': contentType }),
    json: async () => body,
  } as Response;
}

const RESULTS = {
  schema: 1,
  measured_at: '2026-08-04T22:35:28Z',
  commit: 'ee89affd5c7a8ba734c571eebf43c27fa2c2f90e',
  provenance: {
    goldset_sha256: 'cca1a0f6',
    corpus_fingerprint: '2563978b',
    embedding_model: 'sentence-transformers/all-MiniLM-L6-v2',
    limit: 10,
    modes: ['fts', 'vector', 'hybrid'],
  },
  corpus: { sources: { 'gdpr-full': 978, 'seed:gdpr-excerpts': 30 }, chunks: 1008 },
  questions: { total: 30, answerable: 24, unanswerable: 6 },
  modes: {
    fts: {
      precision_at_3: 0.1778,
      recall_at_5: 0.1479,
      mrr_at_10: 0.4196,
      ndcg_at_10: 0.2401,
      answerability_auc: 0.7222,
      by_kind: {},
    },
    vector: {
      precision_at_3: 0.1111,
      recall_at_5: 0.1521,
      mrr_at_10: 0.3082,
      ndcg_at_10: 0.2099,
      answerability_auc: 0.4931,
      by_kind: {},
    },
    hybrid: {
      precision_at_3: 0.1444,
      recall_at_5: 0.1761,
      mrr_at_10: 0.3154,
      ndcg_at_10: null,
      answerability_auc: 0.4236,
      by_kind: {},
    },
  },
};

describe('the evals panel section', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(cleanup);

  it('shows every mode and marks the leader of each column', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(RESULTS)),
    );

    render(<EvalsTable {...panelProps()} />);

    await waitFor(() => expect(screen.getAllByText('fts').length).toBeGreaterThan(0));
    expect(screen.getAllByText('vector').length).toBeGreaterThan(0);
    expect(screen.getAllByText('hybrid').length).toBeGreaterThan(0);
    // fts leads MRR@10; hybrid leads R@5. Both are labelled so the highlight is
    // not carried by colour alone.
    expect(screen.getByLabelText('fts MRR@10 (best)').textContent).toBe('0.420');
    expect(screen.getByLabelText('hybrid R@5 (best)').textContent).toBe('0.176');
  });

  it('renders an undefined metric as a dash rather than as zero', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(RESULTS)),
    );

    render(<EvalsTable {...panelProps()} />);

    await waitFor(() => expect(screen.getAllByText('hybrid').length).toBeGreaterThan(0));
    const dashes = screen.getAllByText('—');
    expect(dashes.length).toBe(1);
  });

  it('says how many questions have no answer by design', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(RESULTS)),
    );

    render(<EvalsTable {...panelProps()} />);

    // Without this line `P@3 = 0.178` reads as "82% broken".
    await waitFor(() => expect(screen.getByText(/deliberately cannot answer/)).toBeTruthy());
    expect(screen.getAllByText('6').length).toBe(1);
  });

  it('reports a 503 as a missing artefact and never as a table of zeros', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          {
            status: 503,
            code: 'evals_unavailable',
            detail: 'app/evals/results.json is missing from this build',
          },
          503,
          'application/problem+json',
        ),
      ),
    );

    render(<EvalsTable {...panelProps()} />);

    await waitFor(() => expect(screen.getByText(/not in this build/)).toBeTruthy());
    expect(screen.queryByText('0.000')).toBeNull();
    expect(screen.queryAllByText('fts').length).toBe(0);
  });
});
