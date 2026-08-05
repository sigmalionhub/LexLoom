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
import { useDocuments } from '@/lib/use-documents';
import type { Tenant } from '@/lib/use-lexloom';

const NIST_QUESTION = 'What cryptography must an AAL3 authenticator use?';

const CATALOGUE = [
  {
    key: 'gdpr-full',
    title: 'Regulation (EU) 2016/679 …',
    media_type: 'application/pdf',
    download: 'gdpr-full.pdf',
    source_url: 'https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:32016R0679',
    preloaded: true,
    suggestions: [],
  },
  {
    key: 'nist-sp-800-63b',
    title: 'NIST Special Publication 800-63B rev 4 …',
    media_type:
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    download: 'nist-sp-800-63b.docx',
    source_url: 'https://pages.nist.gov/800-63-4/sp800-63b.html',
    preloaded: false,
    suggestions: [NIST_QUESTION],
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

/**
 * `Documents` is presentational; the fetching lives in `useDocuments`. The
 * harness is the smallest thing that wires the two together, so these tests
 * still exercise the poll, the catalogue fetch and the `POST` rather than a
 * hand-built props object that could drift from what the hook really returns.
 *
 * It also reports `suggesting`, which is the hook's other consumer: the empty
 * state offers a document's own questions only once that document has landed.
 */
function Harness() {
  const documents = useDocuments(tenant);
  return (
    <>
      <Documents documents={documents} hasTenant />
      <ul data-testid="suggesting">
        {documents.suggesting.flatMap((entry) =>
          entry.suggestions.map((question) => <li key={question}>{question}</li>),
        )}
      </ul>
    </>
  );
}

beforeEach(() => {
  startIngest.mockClear();
  serveStatus(statusMap());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Documents', () => {
  it('renders one card per catalogue source, with its format visible', async () => {
    render(<Harness />);

    expect(await screen.findByText('gdpr-full')).toBeTruthy();
    expect(screen.getByText('nist-sp-800-63b')).toBeTruthy();

    // The owner asked for the second document to be recognisable *by its file
    // extension*. Rendering the raw media type would technically show it and
    // would answer nobody.
    expect(screen.getByText('PDF')).toBeTruthy();
    expect(screen.getByText('DOCX')).toBeTruthy();
  });

  it('shows a source the tenant has never touched as not ingested, not as loaded', async () => {
    render(<Harness />);
    await screen.findByText('gdpr-full');
    expect(screen.getAllByText('Not ingested')).toHaveLength(2);
  });

  it('reports the real state of an ingested source rather than assuming it', async () => {
    serveStatus(statusMap({ 'gdpr-full': COMPLETE_GDPR }));

    render(<Harness />);

    // The completion statement is the count, not the word "complete": a reader
    // comparing two cards needs to see what is actually in the corpus.
    expect(await screen.findByText('978')).toBeTruthy();
    expect(screen.getByText(/passages ·/)).toBeTruthy();
    // The other one is untouched and says so — the server sends every key, so
    // this section never has to decide what an absent entry meant.
    expect(screen.getByText('Not ingested')).toBeTruthy();
  });

  it('says how a held document got there, which is the whole demonstration', async () => {
    serveStatus(
      statusMap({
        'gdpr-full': COMPLETE_GDPR,
        'nist-sp-800-63b': {
          stage: 'complete',
          chunks_done: 746,
          chunks_total: 746,
          error: null,
          started_at: null,
        },
      }),
    );

    render(<Harness />);
    await screen.findByText('978');

    // Both read `complete`, and the page must not present them as the same
    // thing: one was handed over at sign-in, the other was watched arriving.
    // The distinction comes down with the catalogue — a source key spelled out
    // in the component would be the drift the catalogue route exists to stop.
    expect(screen.getByText(/copied in at sign-in/)).toBeTruthy();
    expect(screen.getByText(/ingested by this tenant/)).toBeTruthy();
  });

  it('offers the file itself, and names the publisher beside it', async () => {
    render(<Harness />);
    await screen.findByText('gdpr-full');

    // The download exists so a citation can be checked against the document
    // rather than believed. The path comes from the catalogue, not assembled
    // here, so the copy step and this link cannot drift apart.
    const file = screen.getByRole('link', { name: /Download the PDF/ });
    expect(file.getAttribute('href')).toBe('/sources/gdpr-full.pdf');
    expect(file.hasAttribute('download')).toBe(true);

    // Naming the source is a licence term for redistributing either document,
    // not decoration — and the publisher's copy is the stronger evidence, since
    // a file this page hands out proves only that it agrees with itself.
    const publisher = screen.getAllByRole('link', { name: /from the publisher/ })[0];
    expect(publisher.getAttribute('href')).toContain('eur-lex.europa.eu');
    expect(publisher.getAttribute('rel')).toContain('noopener');
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

    render(<Harness />);

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

    render(<Harness />);

    const bar = await screen.findByRole('progressbar');
    // No count exists yet, so none is claimed. `aria-valuenow` on a bar with no
    // maximum would announce a number the worker never wrote.
    expect(bar.getAttribute('aria-valuenow')).toBeNull();
    expect(bar.getAttribute('aria-valuetext')).toBe('Parsing');
  });

  it('starts an ingest for the source that was clicked, and only that one', async () => {
    render(<Harness />);
    const buttons = await screen.findAllByRole('button', { name: /Ingest this/ });
    expect(buttons).toHaveLength(2);

    fireEvent.click(buttons[1]);

    await waitFor(() => expect(startIngest).toHaveBeenCalledTimes(1));
    expect(startIngest).toHaveBeenCalledWith('test-token', 'nist-sp-800-63b');
  });

  it('offers no button for a source that is already complete', async () => {
    serveStatus(statusMap({ 'gdpr-full': COMPLETE_GDPR }));

    render(<Harness />);
    await screen.findByText(/passages ·/);
    expect(screen.getAllByRole('button', { name: /Ingest this/ })).toHaveLength(1);
  });

  it('offers a document’s own questions only once the tenant holds it', async () => {
    render(<Harness />);
    await screen.findByText('nist-sp-800-63b');

    // Idle. Asking this now would retrieve nothing, and "no passage matched" is
    // not what a visitor should get from a question the page put in front of
    // them.
    expect(screen.queryByText(NIST_QUESTION)).toBeNull();
  });

  it('offers them as soon as that document has landed', async () => {
    serveStatus(
      statusMap({
        'nist-sp-800-63b': {
          stage: 'complete',
          chunks_done: 746,
          chunks_total: 746,
          error: null,
          started_at: null,
        },
      }),
    );

    render(<Harness />);
    expect(await screen.findByText(NIST_QUESTION)).toBeTruthy();
  });

  it('offers nothing for a complete document that carries no questions', async () => {
    // `gdpr-full` deliberately ships none: every tenant is seeded with excerpts
    // of it, so the page already offers GDPR questions from the first paint.
    serveStatus(statusMap({ 'gdpr-full': COMPLETE_GDPR }));

    render(<Harness />);
    await screen.findByText(/passages ·/);
    expect(screen.getByTestId('suggesting').children).toHaveLength(0);
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

    render(<Harness />);
    expect(await screen.findByText(/The ingest failed/)).toBeTruthy();
    // A code this table has not caught up with is still more use on screen than
    // a generic apology.
    expect(screen.getByText('something_new')).toBeTruthy();
  });
});
