import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

/**
 * `@testing-library/react`'s automatic cleanup only registers itself when it
 * finds a global `afterEach` — the shape Jest hands it for free. Vitest's
 * `globals` option is deliberately off (every test file imports `describe`,
 * `it`, `expect`, etc. from `'vitest'` explicitly, so eslint never has to be
 * taught new globals), so nothing hands `afterEach` to the global scope on
 * its own, and a component rendered in one test would otherwise still be
 * mounted in the next. Explicit, imported `afterEach`, called once, here.
 */
afterEach(() => {
  cleanup();
});
