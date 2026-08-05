/**
 * The citation row, against both shapes of source in the catalogue.
 *
 * This file exists because of what shipped without it. The row was written when
 * every citation came from the seeded GDPR excerpts: a title on the left, a
 * short `p. 52` on the right. A `.docx` has no pages and carries a heading path
 * instead, and the row then printed that path *twice* — once as the title,
 * once in the page slot it fell back to — while squeezing `nist-sp-800-63b`
 * into four stacked lines and truncating the title to "Authenticator an…".
 *
 * Every assertion below is one of the things that went wrong on screen.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Sources } from '@/components/sources';
import type { Citation } from '@/lib/api';

const ORG = '348baf7f-0000-0000-0000-000000000000';

/** A NIST chunk: no pages, a heading path, verbatim prose with no citation lede. */
const PAGELESS: Citation = {
  chunk_id: 'c1',
  org_id: ORG,
  content:
    'When processing a request to establish or change a password, verifiers SHALL compare the ' +
    'prospective secret against a blocklist.',
  score: 0.9,
  page_from: null,
  page_to: null,
  locator:
    'Authenticator and Verifier Requirements › Requirements by Authenticator Type › ' +
    'Passwords › Password Verifiers',
  source_key: 'nist-sp-800-63b',
} as unknown as Citation;

/** A seeded GDPR excerpt: pages, and a citation before the dash. */
const PAGED: Citation = {
  chunk_id: 'c2',
  org_id: ORG,
  content:
    'Article 33(1) GDPR — In the case of a personal data breach, the controller shall without ' +
    'undue delay notify the supervisory authority.',
  score: 0.5,
  page_from: 52,
  page_to: 52,
  locator: null,
  source_key: 'gdpr-full',
} as unknown as Citation;

describe('Sources', () => {
  it('titles a pageless citation with its leaf section, not the whole path', () => {
    render(<Sources citations={[PAGELESS]} orgId={ORG} />);

    // "Password Verifiers" is what identifies this passage. The full path as a
    // title truncated to "Authenticator an…", which names the chapter and tells
    // a reader nothing about the chunk they are looking at.
    expect(screen.getByText('Password Verifiers')).toBeTruthy();
  });

  it('shows the ancestors once, as context, and never as a second copy', () => {
    render(<Sources citations={[PAGELESS]} orgId={ORG} />);

    const ancestors = screen.getAllByText(/Requirements by Authenticator Type/);
    expect(ancestors).toHaveLength(1);
    // The leaf belongs to the title and must not be repeated in the context
    // line — that repetition is what filled half the row on screen.
    expect(ancestors[0].textContent).not.toContain('Password Verifiers');
  });

  it('prints no page reference for a source that has no pages', () => {
    render(<Sources citations={[PAGELESS]} orgId={ORG} />);

    // `null === null` is true, so the equality branch alone rendered `p. null`.
    expect(screen.queryByText(/p\. /)).toBeNull();
    expect(screen.queryByText(/null/)).toBeNull();
  });

  it('still names the document a passage came from', () => {
    render(<Sources citations={[PAGELESS]} orgId={ORG} />);
    // Retrieval searches the whole corpus with no per-source filter, so this is
    // the only thing telling a reader which of two documents answered.
    expect(screen.getByText('nist-sp-800-63b')).toBeTruthy();
  });

  it('keeps the page reference for a source that has one', () => {
    render(<Sources citations={[PAGED]} orgId={ORG} />);

    expect(screen.getByText('p. 52')).toBeTruthy();
    // The excerpt leads with its own citation, so that is the title and there
    // is no context line to add under it.
    expect(screen.getByText('Article 33(1) GDPR')).toBeTruthy();
  });

  it('presents a titleless chunk as a fragment, not as a heading cut mid-word', () => {
    // The third shape, and the one that only appeared once every tenant held
    // the whole GDPR rather than thirty excerpts of it: packed to a character
    // budget, so it begins and ends mid-sentence; placed by page, so no
    // locator; no citation lede either. There is no title to find here.
    const raw = {
      ...PAGED,
      chunk_id: 'c3',
      content:
        'appropriate technical and organisational measures in such a manner that the ' +
        'processing will meet the requirements of this Regulation',
      page_from: 50,
      page_to: 50,
    } as unknown as Citation;

    render(<Sources citations={[raw]} orgId={ORG} />);

    const shown = screen.getByText(/appropriate technical/);
    const text = shown.textContent ?? '';
    // Opened and closed as a fragment, so it reads as quoted text rather than
    // as a heading the document does not have.
    expect(text.startsWith('…')).toBe(true);
    expect(text.endsWith('…')).toBe(true);
    // Cut at a word boundary. The old fixed 64-character slice ended this exact
    // sentence on "in such a mann".
    expect(text).not.toMatch(/mann…$/);
    expect(text).toContain('manner');
  });

  it('marks a chunk belonging to another tenant, which is the claim on trial', () => {
    render(<Sources citations={[PAGELESS]} orgId="11111111-1111-1111-1111-111111111111" />);
    expect(screen.getByText('foreign tenant')).toBeTruthy();
  });
});
