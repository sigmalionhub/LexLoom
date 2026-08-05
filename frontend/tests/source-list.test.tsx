/**
 * The documents section, and the two things it must never do.
 *
 * This file lives in `tests/` and not in `components/panel/__tests__/` because
 * `vitest.config.mts` includes exactly `tests/ **\/*.test.{ts,tsx}`. A test put
 * anywhere else is never collected, `npm test` stays green, and nothing says a
 * word — which is how a plan for this milestone nearly shipped a whole suite
 * that could not run.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SourceList } from '@/components/panel/source-list';
import type { PanelSectionProps } from '@/components/panel/types';

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

function statusMap(overrides: Record<string, unknown> = {}) {
  return { 'gdpr-full': { ...IDLE }, 'nist-sp-800-63b': { ...IDLE }, ...overrides };
}

const tenant = {
  orgId: '11111111-1111-1111-1111-111111111111',
  orgName: 'Test tenant',
  token: 'test-token',
  tokenExpiresAt: Date.now() + 3_600_000,
} as unknown as PanelSectionProps['tenant'];

const props = {
  tenant,
  turn: null,
  requests: [],
  isolation: { chunks: 0, foreign: 0 },
  tenantCount: 1,
} as unknown as PanelSectionProps;

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
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve(statusMap()) })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('SourceList', () => {
  it('renders one row per catalogue source, with its format visible', async () => {
    render(<SourceList {...props} />);

    expect(await screen.findByText('gdpr-full')).toBeTruthy();
    expect(screen.getByText('nist-sp-800-63b')).toBeTruthy();

    // The owner asked for the second document to be recognisable *by its file
    // extension*. Rendering the raw media type would technically show it and
    // would answer nobody.
    expect(screen.getByText('PDF')).toBeTruthy();
    expect(screen.getByText('DOCX')).toBeTruthy();
  });

  it('shows a source the tenant has never touched as not ingested, not as loaded', async () => {
    render(<SourceList {...props} />);
    await screen.findByText('gdpr-full');
    expect(screen.getAllByText('Not ingested')).toHaveLength(2);
  });

  it('reports the real state of an ingested source rather than assuming it', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve(
              statusMap({
                'gdpr-full': {
                  stage: 'complete',
                  chunks_done: 978,
                  chunks_total: 978,
                  error: null,
                  started_at: null,
                },
              }),
            ),
        }),
      ),
    );

    render(<SourceList {...props} />);
    expect(await screen.findByText('Complete')).toBeTruthy();
    expect(screen.getByText('978 / 978')).toBeTruthy();
    // The other one is untouched and says so — the server sends every key, so
    // the panel never has to decide what an absent entry meant.
    expect(screen.getByText('Not ingested')).toBeTruthy();
  });

  it('starts an ingest for the source that was clicked, and only that one', async () => {
    render(<SourceList {...props} />);
    const buttons = await screen.findAllByRole('button', { name: /^Ingest this/ });
    expect(buttons).toHaveLength(2);

    fireEvent.click(buttons[1]);

    await waitFor(() => expect(startIngest).toHaveBeenCalledTimes(1));
    expect(startIngest).toHaveBeenCalledWith('test-token', 'nist-sp-800-63b');
  });

  it('offers no button for a source that is already complete', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve(
              statusMap({
                'gdpr-full': {
                  stage: 'complete',
                  chunks_done: 978,
                  chunks_total: 978,
                  error: null,
                  started_at: null,
                },
              }),
            ),
        }),
      ),
    );

    render(<SourceList {...props} />);
    await screen.findByText('Complete');
    expect(screen.getAllByRole('button', { name: /^Ingest this/ })).toHaveLength(1);
  });

  it('renders an error code as a sentence, and an unknown one as itself', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve(
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
            ),
        }),
      ),
    );

    render(<SourceList {...props} />);
    expect(await screen.findByText(/The ingest failed/)).toBeTruthy();
    // A code this table has not caught up with is still more use on screen than
    // a generic apology.
    expect(screen.getByText('something_new')).toBeTruthy();
  });
});
