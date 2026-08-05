/**
 * One origin in front of two things — which is exactly what nginx does in
 * production, and the reason the API carries no CORS middleware.
 *
 * `npm run dev`     starts Next's dev server on an inner port and proxies to it.
 * `npm run preview` serves the built `out/` directory instead, so the thing you
 *                   are looking at is the artefact that gets deployed.
 *
 * In both modes `/api` and `/health` are streamed to the API, byte for byte,
 * with no buffering anywhere in the path.
 *
 * This exists instead of Next's own `rewrites()`, which would have been one
 * line of configuration. The dev server's rewrite proxy buffers the response
 * body: an SSE stream that takes 1.3 s to arrive is delivered in a single chunk
 * at the end, so the answer appears all at once and the panel measures the gap
 * between the citations frame and the first token as 0 ms. A demo whose whole
 * claim is that those figures are real cannot ship a development mode that
 * quietly invents them. Measured on this stack: 2 distinct answer lengths and a
 * 0 ms gap through `rewrites()`, against 32 lengths and a 28 ms gap through
 * this proxy.
 *
 * Node standard library only. No dependency, in production or out of it.
 */
import { spawn } from 'node:child_process';
import { createReadStream, existsSync, statSync } from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import { dirname, extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..');

const staticMode = process.argv.includes('--static');
const port = Number(process.env.PORT ?? 3000);
const nextPort = Number(process.env.NEXT_PORT ?? port + 1);
const api = new URL(process.env.API_ORIGIN ?? 'http://127.0.0.1:8100');
const outDir = join(ROOT, 'out');

const CONTENT_TYPES = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.txt': 'text/plain; charset=utf-8',
  '.woff2': 'font/woff2',
};

/* -------------------------------------------------------------------------- */

let child = null;
if (!staticMode) {
  const bin = join(ROOT, 'node_modules', '.bin', 'next');
  // Its own build directory, so `npm run build` in another terminal cannot
  // overwrite the artefacts this server is holding open. When it did, every
  // request became a bare `Internal Server Error` with nothing else failing —
  // see the note on `distDir` in `next.config.mjs`.
  child = spawn(bin, ['dev', '-p', String(nextPort)], {
    stdio: 'inherit',
    cwd: ROOT,
    env: { ...process.env, NEXT_DIST_DIR: process.env.NEXT_DIST_DIR ?? '.next-dev' },
  });
  child.on('exit', (code) => process.exit(code ?? 0));
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => {
      child?.kill(signal);
      process.exit(0);
    });
  }
} else if (!existsSync(outDir)) {
  console.error(`No build at ${outDir}. Run \`npm run build\` first.`);
  process.exit(1);
}

/** Pipes a request upstream and the response straight back, without buffering. */
function proxy(req, res, target) {
  const upstream = http.request(
    {
      host: target.hostname,
      port: target.port,
      path: req.url,
      method: req.method,
      headers: { ...req.headers, host: target.host },
    },
    (from) => {
      res.writeHead(from.statusCode ?? 502, from.headers);
      from.pipe(res);
    },
  );
  upstream.on('error', (error) => {
    if (!res.headersSent) res.writeHead(502, { 'content-type': 'text/plain' });
    res.end(`upstream ${target.origin}: ${error.message}`);
  });
  req.pipe(upstream);
}

function serveFile(req, res) {
  const pathname = new URL(req.url, 'http://localhost').pathname;
  const safe = normalize(pathname).replace(/^(\.\.[/\\])+/, '');
  let file = join(outDir, safe);
  if (existsSync(file) && statSync(file).isDirectory()) file = join(file, 'index.html');
  if (!existsSync(file)) file = join(outDir, '404.html');
  if (!existsSync(file)) {
    res.writeHead(404, { 'content-type': 'text/plain' }).end('not found');
    return;
  }
  res.writeHead(200, { 'content-type': CONTENT_TYPES[extname(file)] ?? 'application/octet-stream' });
  createReadStream(file).pipe(res);
}

const server = http.createServer((req, res) => {
  const { pathname } = new URL(req.url, 'http://localhost');
  if (pathname.startsWith('/api/') || pathname.startsWith('/health/') || pathname === '/openapi.json') {
    proxy(req, res, api);
    return;
  }
  if (staticMode) serveFile(req, res);
  else proxy(req, res, new URL(`http://127.0.0.1:${nextPort}`));
});

// Fast Refresh talks over a WebSocket, and an upgrade is not an ordinary
// request — without this the page reloads but never hot-updates.
server.on('upgrade', (req, socket, head) => {
  if (staticMode) {
    socket.destroy();
    return;
  }
  const upstream = net.connect(nextPort, '127.0.0.1', () => {
    upstream.write(
      `${req.method} ${req.url} HTTP/1.1\r\n` +
        Object.entries(req.headers)
          .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : v}\r\n`)
          .join('') +
        '\r\n',
    );
    if (head?.length) upstream.write(head);
    socket.pipe(upstream).pipe(socket);
  });
  upstream.on('error', () => socket.destroy());
  socket.on('error', () => upstream.destroy());
});

server.listen(port, '127.0.0.1', () => {
  const what = staticMode ? `the build in ${outDir}` : `next dev on :${nextPort}`;
  console.log(`\n  LexLoom on http://127.0.0.1:${port} — ${what}, API at ${api.origin}\n`);
});
