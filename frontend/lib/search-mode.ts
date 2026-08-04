import type { components } from './api-types';

export type SearchMode = components['schemas']['SearchMode'];

// frontend/lib/search-mode.ts — one owner for the parameter name and its
// parsing.
//
// Calling useLexloom() from the section is the shorter-looking route and the
// one thing that must not be done: the hook owns tenants, turns and the open
// stream, so a second instance logs in a second tenant and answers into a
// transcript nobody is looking at.
//
// Deliberately a twin of lib/generator-choice.ts rather than a shared module:
// that file is created in branch A, this one in branch B, and a shared
// import would make one branch's CI depend on which branch merges first. The
// duplication is two constants and two short functions, and it is bounded —
// folding them into one module is a merge-time refactor, done with both
// files visible, not a cross-branch dependency taken on faith.
export const SEARCH_MODE_PARAM = 'mode';
export const DEFAULT_SEARCH_MODE: SearchMode = 'fts';

// `satisfies` and not a hand-written union: the type comes from the generated
// api-types, so the day the backend enum gains a member and this list does
// not, `tsc --noEmit` fails here instead of the UI silently offering two of
// three.
export const SEARCH_MODES = ['fts', 'vector', 'hybrid'] as const satisfies readonly SearchMode[];

export function readSearchMode(): SearchMode {
  if (typeof window === 'undefined') return DEFAULT_SEARCH_MODE;
  const raw = new URLSearchParams(window.location.search).get(SEARCH_MODE_PARAM);
  // The URL is user input. An unknown value falls back rather than travelling
  // to the API and returning as a 422 the visitor has no way to explain.
  return (SEARCH_MODES as readonly string[]).includes(raw ?? '')
    ? (raw as SearchMode)
    : DEFAULT_SEARCH_MODE;
}

export function writeSearchMode(mode: SearchMode): void {
  const url = new URL(window.location.href);
  url.searchParams.set(SEARCH_MODE_PARAM, mode);
  // replaceState, not pushState: the mode is a setting, not a place. pushState
  // makes the browser's Back button undo a toggle instead of leaving the demo.
  window.history.replaceState(null, '', url);
}
