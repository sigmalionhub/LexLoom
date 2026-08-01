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
};

export default nextConfig;
