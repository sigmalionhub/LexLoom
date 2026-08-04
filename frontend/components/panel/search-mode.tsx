'use client';

import { useEffect, useState } from 'react';
import {
  DEFAULT_SEARCH_MODE,
  SEARCH_MODES,
  readSearchMode,
  writeSearchMode,
  type SearchMode as SearchModeValue,
} from '@/lib/search-mode';
import { Row, Section } from './primitives';
import type { PanelSectionProps } from './types';

const LABELS: Record<SearchModeValue, { label: string; note: string }> = {
  fts: { label: 'Full-text', note: 'lexical match, ranked by ts_rank' },
  vector: { label: 'Vector', note: 'semantic match, ranked by cosine similarity' },
  hybrid: { label: 'Hybrid', note: 'both, fused by reciprocal rank' },
};

/**
 * Search mode selection section.
 *
 * The chosen value travels as a URL parameter (`lib/search-mode.ts`), the
 * same mechanism `use-lexloom.ts` reads from when it asks — this component
 * and that hook never talk to each other directly, on purpose (see
 * `search-mode.ts` for why a second `useLexloom()` instance here would be
 * worse).
 */
export function SearchMode({ turn }: PanelSectionProps) {
  // Initialised to the default and synced from the URL in an effect, never in
  // the useState initialiser. next.config.mjs sets `output: 'export'`, so the
  // HTML is prerendered at build time with no query string; reading the URL
  // during the first client render makes the markup disagree with the
  // server's and React throws the tree away with a hydration error — in the
  // panel whose entire job is to look trustworthy.
  const [mode, setMode] = useState<SearchModeValue>(DEFAULT_SEARCH_MODE);
  useEffect(() => setMode(readSearchMode()), []);

  // Both, always, in one handler. `setMode` alone repaints the button and
  // leaves the URL untouched — and the URL is the carrier, so `ask()` would
  // keep reading the previous value. A toggle that looks like it works and
  // changes nothing is exactly the bug revision 1 shipped and this task
  // exists to undo.
  function select(next: SearchModeValue) {
    writeSearchMode(next);
    setMode(next);
  }

  return (
    <Section label="Search mode">
      <div className="flex flex-col gap-1.5" role="radiogroup" aria-label="Retrieval mode">
        {SEARCH_MODES.map((option) => {
          const active = option === mode;
          return (
            <button
              key={option}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => select(option)}
              className={[
                'flex items-baseline justify-between gap-3 rounded-md border px-2.5 py-1.5 text-left transition-colors',
                active
                  ? 'border-chalk/50 bg-slate-raised text-chalk'
                  : 'border-slate-rule text-mist hover:border-chalk/30 hover:text-chalk',
              ]
                .filter(Boolean)
                .join(' ')}
            >
              <span className="font-mono text-data">{LABELS[option].label}</span>
              <span className="figure text-micro text-mist">{LABELS[option].note}</span>
            </button>
          );
        })}
      </div>

      {/* Read off the `citations` frame, not off `mode` above: if the URL
          carrier ever breaks, the selector would still show the click that
          was made while every answer kept retrieving in `fts` — this row is
          what makes that gap visible instead of silent. */}
      {turn?.searchMode && (
        <Row label="last answer retrieved with" wrap>
          {LABELS[turn.searchMode].label}
        </Row>
      )}
    </Section>
  );
}
