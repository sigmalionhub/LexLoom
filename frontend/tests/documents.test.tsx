/**
 * The documents section, and the things it must never do.
 *
 * This file lives in `tests/` and not in `components/__tests__/` because
 * `vitest.config.mts` includes exactly `tests/ **\/*.test.{ts,tsx}`. A test put
 * anywhere else is never collected, `npm test` stays green, and nothing says a
 * word — which is how a plan for this milestone nearly shipped a whole suite
 * that could not run.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Documents } from '@/components/documents';
import type { Tenant } from '@/lib/use-lexloom';

const CATALOGUE = [
  { key: 'gdpr-full', title: 'Regulation (EU) 2016/679 …', media_type: 'application/pdf' },
  {
    key: 'nist-sp-800-63b',
    title: 'NIST Special Publication 800-63B rev 4 …',
    media_type:
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  },
];

const IDLE = { stage: 'idle', chunks_done: 0, chunks_total: 0, error: null, started_at: null };

const COMPLETE_GDPR = {
  stage: 'complete',
  chunks_done: 978,
  chunks_total: 978,
  error: null,
  started_at: null,
};

function statusMap(overrides: Record<string, unknown> = {}) {
  return { 'gdpr-full': { ...IDLE }, 'nist-sp-800-63b': { ...IDLE }, ...overrides };
}

/** Stubs `fetch` so the status poll answers with exactly this map. */
function serveStatus(body: Record<string, unknown>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve(body) })),
  );
}

const tenant = {
  orgId: '11111111-1111-1111-1111-111111111111',
  orgName: 'Test tenant',
  token: 'test-token',
  tokenExpiresAt: Date.now() + 3_600_000,
} as unknown as Tenant;

const startIngest = vi.fn();

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    api: {
      catalogue: () => Promise.resolve(CATALOGUE),
      startIngest: (...args: unknown[]) => {
        startIngest(...args);
        return Promise.resolve({ ...IDLE, stage: 'queued', started_at: 1 });
      },
    },
  };
});

beforeEach(() => {
  startIngest.mockClear();
  serveStatus(statusMap());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Documents', () => {
  it('renders one card per catalogue source, with its format visible', async () => {
    render(<Documents tenant={tenant} />);

    expect(await screen.findByText('gdpr-full')).toBeTruthy();
    expect(screen.getByText('nist-sp-800-63b')).toBeTruthy();

    // The owner asked for the second document to be recognisable *by its file
    // extension*. Rendering the raw media type would technically show it and
    // would answer nobody.
    expect(screen.getByText('PDF')).toBeTruthy();
    expect(screen.getByText('DOCX')).toBeTruthy();
  });

  it('shows a source the tenant has never touched as not ingested, not as loaded', async () => {
    render(<Documents tenant={tenant} />);
    await screen.findByText('gdpr-full');
    expect(screen.getAllByText('Not ingested')).toHaveLength(2);
  });

  it('reports the real state of an ingested source rather than assuming it', async () => {
    serveStatus(statusMap({ 'gdpr-full': COMPLETE_GDPR }));

    render(<Documents tenant={tenant} />);

    // The completion statement is the count, not the word "complete": a reader
    // comparing two cards needs to see what is actually in the corpus.
    expect(await screen.findByText('978')).toBeTruthy();
    expect(screen.getByText(/passages in this tenant/)).toBeTruthy();
    // The other one is untouched and says so — the server sends every key, so
    // this section never has to decide what an absent entry meant.
    expect(screen.getByText('Not ingested')).toBeTruthy();
  });

  it('draws the progress bar from the chunk counts the worker reported', async () => {
    serveStatus(
      statusMap({
        'nist-sp-800-63b': {
          stage: 'chunking',
          chunks_done: 373,
          chunks_total: 746,
          error: null,
          started_at: null,
        },
      }),
    );

    render(<Documents tenant={tenant} />);

    const bar = await screen.findByRole('progressbar');
    // Brass means measured, so the bar has to be fed by the counts rather than
    // by a stage index dressed up as progress.
    expect(bar.getAttribute('aria-valuenow')).toBe('373');
    expect(bar.getAttribute('aria-valuemax')).toBe('746');
    expect(screen.getByText('Chunking')).toBeTruthy();
    expect(screen.getByText('373 / 746')).toBeTruthy();
  });

  it('leaves the bar unfilled and unmeasured before the first chunk exists', async () => {
    serveStatus(
      statusMap({
        'nist-sp-800-63b': {
          stage: 'parsing',
          chunks_done: 0,
          chunks_total: 0,
          error: null,
          started_at: null,
        },
      }),
    );

    render(<Documents tenant={tenant} />);

    const bar = await screen.findByRole('progressbar');
    // No count exists yet, so none is claimed. `aria-valuenow` on a bar with no
    // maximum would announce a number the worker never wrote.
    expect(bar.getAttribute('aria-valuenow')).toBeNull();
    expect(bar.getAttribute('aria-valuetext')).toBe('Parsing');
  });

  it('starts an ingest for the source that was clicked, and only that one', async () => {
    render(<Documents tenant={tenant} />);
    const buttons = await screen.findAllByRole('button', { name: /Ingest this/ });
    expect(buttons).toHaveLength(2);

    fireEvent.click(buttons[1]);

    await waitFor(() => expect(startIngest).toHaveBeenCalledTimes(1));
    expect(startIngest).toHaveBeenCalledWith('test-token', 'nist-sp-800-63b');
  });

  it('offers no button for a source that is already complete', async () => {
    serveStatus(statusMap({ 'gdpr-full': COMPLETE_GDPR }));

    render(<Documents tenant={tenant} />);
    await screen.findByText(/passages in this tenant/);
    expect(screen.getAllByRole('button', { name: /Ingest this/ })).toHaveLength(1);
  });

  it('renders an error code as a sentence, and an unknown one as itself', async () => {
    serveStatus(
      statusMap({
        'gdpr-full': {
          stage: 'error',
          chunks_done: 0,
          chunks_total: 0,
          error: 'ingest_failed',
          started_at: null,
        },
        'nist-sp-800-63b': {
          stage: 'error',
          chunks_done: 0,
          chunks_total: 0,
          error: 'something_new',
          started_at: null,
        },
      }),
    );

    render(<Documents tenant={tenant} />);
    expect(await screen.findByText(/The ingest failed/)).toBeTruthy();
    // A code this table has not caught up with is still more use on screen than
    // a generic apology.
    expect(screen.getByText('something_new')).toBeTruthy();
  });
});
