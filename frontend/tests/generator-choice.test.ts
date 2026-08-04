import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_GENERATOR, readGenerator, writeGenerator } from '@/lib/generator-choice';
import { writeSearchMode } from '@/lib/search-mode';

/**
 * `lib/generator-choice.ts` and `lib/search-mode.ts` are a deliberate twin
 * pair (see the comment at the top of each file for why they are not one
 * shared module). Tested the same way, in two files, on purpose.
 */

function setUrl(search: string): void {
  window.history.replaceState(null, '', `/${search}`);
}

beforeEach(() => {
  setUrl('');
});

describe('readGenerator', () => {
  it('returns the default when the URL has no generator parameter', () => {
    setUrl('');
    expect(readGenerator()).toBe(DEFAULT_GENERATOR);
  });

  it('returns the value when the URL carries a valid one', () => {
    setUrl('?generator=gemini-3.6-flash');
    expect(readGenerator()).toBe('gemini-3.6-flash');
  });

  it('returns the default for a value that is not in the allowed list', () => {
    // A visitor-edited URL is user input, and here an unknown value would
    // also be a request against somebody else's quota if it reached the API.
    setUrl('?generator=sql-injection');
    expect(readGenerator()).toBe(DEFAULT_GENERATOR);
  });
});

describe('writeGenerator', () => {
  it('puts the value in the URL', () => {
    writeGenerator('gemini-3.6-flash');
    expect(new URLSearchParams(window.location.search).get('generator')).toBe('gemini-3.6-flash');
  });

  it('leaves the mode parameter intact when writing the generator', () => {
    // The two carriers share one URL. This is the interaction the twin
    // design makes possible to get wrong: a naive `write*()` that rebuilds
    // the query string from scratch would drop whatever the other carrier
    // had just written.
    writeSearchMode('hybrid');
    writeGenerator('gemini-3.5-flash-lite');
    const params = new URLSearchParams(window.location.search);
    expect(params.get('generator')).toBe('gemini-3.5-flash-lite');
    expect(params.get('mode')).toBe('hybrid');
  });

  it('uses replaceState, not pushState', () => {
    // The comment in generator-choice.ts says why (the generator is a
    // setting, not a place) — a comment is not a control, so this asserts
    // the actual call.
    const pushSpy = vi.spyOn(window.history, 'pushState');
    const replaceSpy = vi.spyOn(window.history, 'replaceState');
    writeGenerator('gemini-3.6-flash');
    expect(replaceSpy).toHaveBeenCalled();
    expect(pushSpy).not.toHaveBeenCalled();
    pushSpy.mockRestore();
    replaceSpy.mockRestore();
  });
});
