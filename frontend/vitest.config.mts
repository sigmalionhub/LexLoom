import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * Vitest, not Jest: this project is Next 15 / React 19 / TypeScript 5.7 with
 * ESM, and Jest needs a transform chain (babel or SWC config,
 * `moduleNameMapper` for CSS and path aliases) that is itself a thing to
 * maintain. Vitest reads `tsconfig.json`'s `paths` (via the alias below,
 * mirrored from there) and runs `.tsx` without additional configuration.
 *
 * `jsdom`, not `happy-dom`: the tests that matter here click a real button and
 * read `window.location.search` and `window.history` back — that is exactly
 * the DOM surface jsdom implements closest to a browser's.
 *
 * `.mts`, not `.ts`. `package.json` declares no `"type": "module"`, so Vite's
 * CJS config loader took this file as CommonJS and printed on every single run:
 * "The CJS build of Vite's Node API is deprecated"/"ESM syntax is not allowed
 * in a CommonJS module". It works today only because Vite transpiles it, and
 * that stops when `configLoader: 'native'` becomes the default. The `.mts`
 * extension makes the file's module system explicit at the filesystem level
 * rather than depending on a `package.json` field this Next app has reasons not
 * to set. Vitest resolves `vitest.config.mts` by default; nothing references
 * the old path, and `tsconfig.json`'s `include` gained an `.mts` glob so the
 * file stays type-checked.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // Mirrors tsconfig.json's `"@/*": ["./*"]`.
      '@': fileURLToPath(new URL('.', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.{ts,tsx}'],
    setupFiles: ['tests/setup.ts'],
    css: false,
    // Never watch in CI: `npm test` is `vitest run`, but a stray `--watch`
    // flag from a local invocation must still not hang here.
    watch: false,
  },
});
