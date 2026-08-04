import { beforeEach, describe, expect, it, vi } from 'vitest';
import { writeGenerator } from '@/lib/generator-choice';
import { DEFAULT_SEARCH_MODE, readSearchMode, writeSearchMode } from '@/lib/search-mode';

/**
 * `lib/search-mode.ts` and `lib/generator-choice.ts` are a deliberate twin
 * pair (see the comment at the top of each file for why they are not one
 * shared module). Tested the same way, in two files, on purpose.
 */

function setUrl(search: string): void {
  window.history.replaceState(null, '', `/${search}`);
}

beforeEach(() => {
  setUrl('');
});

describe('readSearchMode', () => {
  it('returns the default when the URL has no mode parameter', () => {
    setUrl('');
    expect(readSearchMode()).toBe(DEFAULT_SEARCH_MODE);
  });

  it('returns the value when the URL carries a valid one', () => {
    setUrl('?mode=vector');
    expect(readSearchMode()).toBe('vector');
  });

  it('returns the default for a value that is not in the allowed list', () => {
    // A visitor-edited URL is user input. This is the branch that keeps a
    // junk value from reaching the API and coming back as a 422 the visitor
    // has no way to explain.
    setUrl('?mode=sql-injection');
    expect(readSearchMode()).toBe(DEFAULT_SEARCH_MODE);
  });
});

describe('writeSearchMode', () => {
  it('puts the value in the URL', () => {
    writeSearchMode('hybrid');
    expect(new URLSearchParams(window.location.search).get('mode')).toBe('hybrid');
  });

  it('leaves the generator parameter intact when writing the mode', () => {
    // The two carriers share one URL. This is the interaction the twin
    // design makes possible to get wrong: a naive `write*()` that rebuilds
    // the query string from scratch would drop whatever the other carrier
    // had just written.
    writeGenerator('gemini-3.5-flash-lite');
    writeSearchMode('vector');
    const params = new URLSearchParams(window.location.search);
    expect(params.get('mode')).toBe('vector');
    expect(params.get('generator')).toBe('gemini-3.5-flash-lite');
  });

  it('uses replaceState, not pushState', () => {
    // The comment in search-mode.ts says why (a setting is not a place, and
    // Back should leave the demo rather than undo a toggle) — a comment is
    // not a control, so this asserts the actual call.
    const pushSpy = vi.spyOn(window.history, 'pushState');
    const replaceSpy = vi.spyOn(window.history, 'replaceState');
    writeSearchMode('vector');
    expect(replaceSpy).toHaveBeenCalled();
    expect(pushSpy).not.toHaveBeenCalled();
    pushSpy.mockRestore();
    replaceSpy.mockRestore();
  });
});
