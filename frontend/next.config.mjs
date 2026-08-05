/**
 * The page is a static export, on purpose.
 *
 * It is a client-side chat that talks to the API from the browser; server
 * rendering would add nothing it can render, and a Next server container costs
 * 200-300 MB of RSS on a box that has 2.2 GB free and no swap. `output: 'export'`
 * turns the build into files that the same nginx already serving the API can
 * hand out: zero extra memory, zero extra units to supervise.
 *
 * That choice is also why there is no CORS anywhere in the backend. In
 * production the page and the API are one origin behind one nginx, so the
 * browser never issues a preflight. Development reproduces that shape with
 * `scripts/serve.mjs` rather than by relaxing the API — see that file for why
 * it is a hand-written proxy and not `rewrites()`.
 */

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',

  // A build and a running dev server must not share a build directory.
  //
  // They did, and it cost a debugging round: `npm run build` writes production
  // output over the incremental artefacts `next dev` is holding open, and the
  // dev server then answers every request with a bare `Internal Server Error`
  // while `tsc`, `eslint` and the build itself all stay green — so nothing
  // points at the cause. Verifying a build while someone has the page open is
  // an ordinary thing to do here, and it should not break their tab.
  //
  // `scripts/serve.mjs` sets `NEXT_DIST_DIR=.next-dev` for the dev server it
  // spawns; a build sets nothing and keeps `.next`. Both are git-ignored.
  distDir: process.env.NEXT_DIST_DIR ?? '.next',

  // Note the two things that are deliberately absent.
  //
  // `rewrites()` would have been the obvious way to reach the API in
  // development, and it silently buffers SSE: the whole stream lands in one
  // chunk and the panel measures a 0 ms gap between the citations frame and the
  // first token. `scripts/serve.mjs` proxies instead, and streams.
  //
  // `trailingSlash: true` would make the dev server answer
  // `/api/v1/auth/demo-login` with a 308 to the same path plus a slash. The
  // export is a single route, so it buys nothing in return.

  // next/image's optimiser is a server. There is no server.
  images: { unoptimized: true },

  reactStrictMode: true,

  // `next lint`'s default is `['app', 'pages', 'components', 'lib', 'src']`,
  // and setting `dirs` *replaces* that list rather than extending it — so
  // this has to repeat every default dir this project actually has (`app`,
  // `components`, `lib`; there is no `pages` or `src` here) alongside the
  // one being added. Without `tests` here, `npm run lint` silently never
  // looks at `tests/`: verified by planting a real `react-hooks/rules-of-hooks`
  // violation in a test file — `next lint` reported nothing for it, and
  // plain `eslint` on the same file caught it immediately.
  eslint: { dirs: ['app', 'components', 'lib', 'tests'] },
};

export default nextConfig;
