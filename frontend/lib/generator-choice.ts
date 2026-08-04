import type { components } from './api-types';

export type GeneratorChoice = components['schemas']['GeneratorChoice'];

// frontend/lib/generator-choice.ts — one owner for the parameter name, its
// allowed values and its parsing.
//
// Deliberately a twin of lib/search-mode.ts rather than a shared module: that
// file is created in branch B, this one in branch A, and a shared import would
// make one branch's CI depend on which branch merges first. The duplication is
// two constants and two five-line functions, and it is bounded — folding them
// into one module is a merge-time refactor, done with both files visible, not a
// cross-branch dependency taken on faith.
export const GENERATOR_PARAM = 'generator';
export const DEFAULT_GENERATOR: GeneratorChoice = 'deterministic';

// `satisfies` and not a hand-written union: the type comes from the generated
// api-types, so the day the backend enum gains a member and this list does not,
// `tsc --noEmit` fails here instead of the UI silently offering two of three.
export const GENERATORS = [
  'deterministic', 'gemini-3.5-flash-lite', 'gemini-3.6-flash',
] as const satisfies readonly GeneratorChoice[];

export function readGenerator(): GeneratorChoice {
  if (typeof window === 'undefined') return DEFAULT_GENERATOR;
  const raw = new URLSearchParams(window.location.search).get(GENERATOR_PARAM);
  // The URL is user input. An unknown value falls back rather than travelling to
  // the API and returning as a 422 the visitor has no way to explain — and here
  // it would also be a request against somebody else's quota.
  return (GENERATORS as readonly string[]).includes(raw ?? '')
    ? (raw as GeneratorChoice)
    : DEFAULT_GENERATOR;
}

export function writeGenerator(choice: GeneratorChoice): void {
  const url = new URL(window.location.href);
  url.searchParams.set(GENERATOR_PARAM, choice);
  // replaceState, not pushState: the generator is a setting, not a place.
  window.history.replaceState(null, '', url);
}
