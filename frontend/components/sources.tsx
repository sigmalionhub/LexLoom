'use client';

import { useState } from 'react';
import type { Citation } from '@/lib/api';

/**
 * The sources block renders before the answer, because it arrives before the
 * answer. Nothing here is held back to make that look better.
 */

interface SourcesProps {
  citations: Citation[];
  /** The tenant that asked. Every chunk should carry this same org_id. */
  orgId: string;
}

export function Sources({ citations, orgId }: SourcesProps) {
  const [expanded, setExpanded] = useState<string | null>(null);

  if (citations.length === 0) {
    return (
      <div className="rounded border border-dashed border-rule px-4 py-3 text-[0.9375rem] text-ink-soft">
        No passage in this tenant’s corpus matched. The answer below is written without a source, and the
        citations frame still arrived first — empty.
      </div>
    );
  }

  // `score` is a raw ts_rank: comparable inside this one response and nowhere
  // else. So it is drawn as a share of the best hit *in this response*, and
  // never printed as a percentage or compared across questions.
  const best = Math.max(...citations.map((citation) => citation.score));

  return (
    <ol className="divide-y divide-rule overflow-hidden rounded border border-rule bg-paper-raised">
      {citations.map((citation, index) => {
        const { heading, body } = splitCitation(citation.content);
        // `splitCitation`'s dash pattern was written against GDPR chunks
        // ("Article 33(1) GDPR — In the case of…"). NIST's text is verbatim
        // prose with no such lede, so every one of its citations would render
        // a truncated first sentence as its title. The heading path is what
        // that document has instead, and it is the better title anyway.
        const title = heading ?? citation.locator ?? body.slice(0, 64);
        const isOpen = expanded === citation.chunk_id;
        const foreign = citation.org_id !== orgId;

        return (
          <li key={citation.chunk_id}>
            <button
              type="button"
              onClick={() => setExpanded(isOpen ? null : citation.chunk_id)}
              aria-expanded={isOpen}
              className="flex w-full items-baseline gap-3 px-3.5 py-2.5 text-left transition-colors hover:bg-paper"
            >
              <span className="figure w-6 shrink-0 text-[0.6875rem] text-ink-soft/70">
                {String(index + 1).padStart(2, '0')}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[0.9375rem] leading-snug text-ink">
                  {title}
                </span>
                <span className="mt-1.5 flex items-center gap-2">
                  <span
                    aria-hidden
                    // A hairline read as a divider rather than as a gauge, and
                    // adjacent scores were indistinguishable at full width. The
                    // bar carries real information, so it gets enough weight to
                    // be seen.
                    className="h-[3px] w-[72px] shrink-0 overflow-hidden rounded-full bg-rule"
                    title={`rank ${index + 1} of ${citations.length} in this response`}
                  >
                    <span
                      className="block h-full rounded-full bg-brass"
                      style={{
                        width: `${Math.max(8, (citation.score / best) * 100)}%`,
                      }}
                    />
                  </span>
                  <span className="figure text-[0.6875rem] text-ink-soft/70">
                    org {citation.org_id.slice(0, 8)}
                  </span>
                  {/* Which document answered. Retrieval runs over the tenant's
                      whole corpus with no per-source filter, so without this a
                      reader holding two documents cannot tell which one a
                      passage came from — and the demo's whole claim is that an
                      answer is checkable. The short key rather than the title:
                      the GDPR's catalogue title is 280 characters. */}
                  {citation.source_key && (
                    <span className="figure text-[0.6875rem] text-ink-soft/70">
                      {citation.source_key}
                    </span>
                  )}
                  {foreign && (
                    <span className="figure rounded bg-alarm px-1.5 text-[0.6875rem] text-paper">
                      foreign tenant
                    </span>
                  )}
                </span>
              </span>
              <span className="figure shrink-0 whitespace-nowrap text-[0.6875rem] text-ink-soft/70">
                {locatorLabel(citation)}
              </span>
            </button>

            {isOpen && (
              <p className="animate-rise-in border-t border-rule px-3.5 pb-3.5 pt-3 font-serif text-[0.9375rem] leading-relaxed text-ink-soft">
                {body}
              </p>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/**
 * Chunks in this corpus read `Article 33(1) GDPR — In the case of a personal
 * data breach, …`. Lifting the part before the dash gives the list a citation
 * to show instead of a truncated sentence; a chunk that is not shaped that way
 * simply keeps its first line.
 */
function splitCitation(content: string): {
  heading: string | null;
  body: string;
} {
  const match = /^(.{3,72}?)\s+—\s+/.exec(content);
  if (!match) return { heading: null, body: content };
  return { heading: match[1], body: content.slice(match[0].length) };
}

/**
 * Where this chunk is, in whatever way its document can say it.
 *
 * The null test comes first and is not a nicety: `null === null` is true, so
 * the equality branch alone rendered `p. null` for every chunk of a source
 * whose format has no pages. A `.docx` built from a web publication is exactly
 * that, and it carries `locator` — the heading path — instead.
 *
 * Long paths are truncated at the end rather than the start. Measured over the
 * NIST document's 145 headings: median 88 characters, longest 166. The tail is
 * the leaf section, which is the part that identifies the passage, so the head
 * is what gives way.
 */
const LOCATOR_MAX = 64;

function locatorLabel(citation: Citation): string {
  if (citation.page_from === null || citation.page_to === null) {
    const locator = citation.locator;
    if (!locator) return '—';
    if (locator.length <= LOCATOR_MAX) return locator;
    return `…${locator.slice(locator.length - LOCATOR_MAX + 1)}`;
  }
  return citation.page_from === citation.page_to
    ? `p. ${citation.page_from}`
    : `pp. ${citation.page_from}–${citation.page_to}`;
}
