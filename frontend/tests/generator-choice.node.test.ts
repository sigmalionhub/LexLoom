// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { DEFAULT_GENERATOR, readGenerator } from '@/lib/generator-choice';

/**
 * `readGenerator()`'s `typeof window === 'undefined'` branch exists for the
 * static export's server-side render, where there is no `window` at all. The
 * rest of `generator-choice.test.ts` runs under jsdom, which defines `window`
 * as part of the environment itself — stubbing it away there risks
 * corrupting jsdom's own state for whichever test runs next in that file. A
 * plain Node environment has no `window` to begin with, so this one case
 * gets its own file rather than fighting jsdom for it.
 */
describe('readGenerator (no window)', () => {
  it('returns the default when window is undefined', () => {
    expect(typeof window).toBe('undefined');
    expect(readGenerator()).toBe(DEFAULT_GENERATOR);
  });
});
