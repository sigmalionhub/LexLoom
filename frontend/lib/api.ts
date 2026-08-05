import type { components } from './api-types';
import type { GeneratorChoice } from './generator-choice';
import type { SearchMode } from './search-mode';

/**
 * The API is same-origin. In production nginx serves this bundle and proxies
 * `/api` to the FastAPI process; in development `scripts/serve.mjs` does the
 * same job — deliberately, rather than Next's `rewrites()`, which buffers SSE
 * and would make the panel measure a 0 ms gap. Either way the browser sees one
 * origin, which is why the backend carries no CORS middleware — see
 * `docs/api-contract.md` §9.
 *
 * `NEXT_PUBLIC_API_ORIGIN` exists only as an escape hatch for someone hosting
 * the page somewhere else; using it means adding CORS to the API first.
 */
export const API_ORIGIN = process.env.NEXT_PUBLIC_API_ORIGIN ?? '';

/* -------------------------------------------------------------------------- */
/* Request and response bodies: both generated.                               */
/* -------------------------------------------------------------------------- */

/**
 * Every shape below comes from the committed OpenAPI snapshot, so a backend
 * rename breaks this file at compile time rather than at runtime.
 *
 * The response types were once transcribed by hand from `docs/api-contract.md`,
 * because the handlers returned plain dicts and every 2xx in `openapi.json` was
 * declared as `{[key: string]: unknown}` — regenerating changed nothing,
 * because the schema said the same thing at the source. The handlers now carry
 * response models, so the schema describes them and this file no longer holds
 * a second, drifting account of the contract.
 *
 * Refresh both with the stack up:
 *   npm run snapshot:openapi && npm run gen:types
 */
export type DemoLoginRequest = components['schemas']['DemoLoginRequest'];
export type CreateConversationRequest = components['schemas']['CreateConversationRequest'];
export type CreateMessageRequest = components['schemas']['CreateMessageRequest'];
export type StreamTicketRequest = components['schemas']['StreamTicketRequest'];

export type DemoLoginResponse = components['schemas']['DemoLoginResponse'];
export type CreateConversationResponse = components['schemas']['CreateConversationResponse'];
export type CreateMessageResponse = components['schemas']['CreateMessageResponse'];
export type StreamTicketResponse = components['schemas']['StreamTicketResponse'];
export type GeneratorsResponse = components['schemas']['GeneratorsResponse'];
export type EvalsResponse = components['schemas']['EvalsResponse'];
export type EvalsModeMetrics = components['schemas']['EvalsModeMetrics'];

export type IngestRequest = components['schemas']['IngestRequest'];
export type IngestStage = components['schemas']['IngestStage'];
export type IngestStatusResponse = components['schemas']['IngestStatusResponse'];
export type CatalogueEntry = components['schemas']['CatalogueEntry'];
/** Every catalogue source's status for this tenant, keyed by `source_key`. */
export type IngestStatusMap = Record<string, IngestStatusResponse>;

/** One retrieved chunk, exactly as it arrives in the `citations` frame. */
export interface Citation {
  chunk_id: string;
  /** The tenant the row belongs to. The page counts these; see the panel. */
  org_id: string;
  content: string;
  /**
   * Null for a source whose format has no pages — see `locator`. Exactly one of
   * the page pair and `locator` is ever set, and `null` rather than an absent
   * key, so a client can tell "this document has no pages" from "an older
   * server that did not send the field".
   */
  page_from: number | null;
  page_to: number | null;
  /**
   * Which catalogue document this chunk came from. Null only for a row written
   * before the catalogue existed. A question searches the whole tenant corpus
   * with no per-source filter, so this is what makes an answer checkable.
   */
  source_key: string | null;
  /**
   * Where the chunk sits when pages cannot say it: the heading path, e.g.
   * `Authentication Assurance Levels › Reauthentication`.
   */
  locator: string | null;
  /**
   * A relevance score whose *unit* depends on the mode the `citations` frame
   * carries: a raw Postgres `ts_rank` in `fts`, cosine similarity in `vector`,
   * an RRF value in `hybrid`. Comparable inside one response and nowhere
   * else — not across modes, not across questions — so it is rendered as a
   * rank position, never as a percentage.
   */
  score: number;
}

/* -------------------------------------------------------------------------- */
/* Errors                                                                     */
/* -------------------------------------------------------------------------- */

/** RFC 9457. Every error from this service has this shape, including 404s and 500s. */
export interface Problem {
  type?: string;
  title?: string;
  status: number;
  /** Stable, machine-readable. Switch on this and never on `detail`. */
  code: string;
  detail?: string;
  instance?: string;
  request_id?: string;
  errors?: unknown[];
}

/**
 * `Retry-After`, in seconds.
 *
 * `429 rate_limited` and `503 capacity_unavailable` are the two refusals this
 * service sends, and both carry the number here rather than in the body — the
 * `detail` of each says "Retry after Retry-After seconds." literally, because
 * prose is not a contract (`docs/api-contract.md` §6.1). A client that renders
 * `detail` shows a visitor that sentence, so the number has to be read here.
 *
 * RFC 9110 also permits an HTTP-date. This service always sends a delta, but a
 * proxy in front of it need not, so both forms are accepted. `undefined` when
 * the header is absent or unreadable — say "in a moment", do not invent one.
 */
export function retryAfterSeconds(response: Response): number | undefined {
  const raw = response.headers.get('retry-after')?.trim();
  if (!raw) return undefined;
  const delta = Number(raw);
  if (Number.isFinite(delta) && delta >= 0) return Math.ceil(delta);
  const at = Date.parse(raw);
  if (Number.isNaN(at)) return undefined;
  return Math.max(0, Math.ceil((at - Date.now()) / 1000));
}

export class ApiError extends Error {
  readonly problem: Problem;
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;
  /** Present on `429` and `503`. See `retryAfterSeconds`. */
  readonly retryAfter?: number;

  constructor(problem: Problem, retryAfter?: number) {
    super(problem.detail ?? problem.title ?? `HTTP ${problem.status}`);
    this.name = 'ApiError';
    this.problem = problem;
    this.status = problem.status;
    this.code = problem.code;
    this.requestId = problem.request_id;
    this.retryAfter = retryAfter;
  }
}

/* -------------------------------------------------------------------------- */
/* Request log — the raw material for the panel's REQUESTS block              */
/* -------------------------------------------------------------------------- */

export interface RequestRecord {
  id: number;
  method: string;
  path: string;
  status: number;
  /** Wall-clock duration of the fetch, measured in the browser. */
  ms: number;
  /** `X-Request-Id`, the join key into the service's structured logs. */
  requestId?: string;
  code?: string;
  at: number;
}

export type RequestObserver = (record: Omit<RequestRecord, 'id'>) => void;

let observer: RequestObserver | null = null;

/** The panel registers here so that every call this module makes is recorded. */
export function observeRequests(fn: RequestObserver | null): void {
  observer = fn;
}

/* -------------------------------------------------------------------------- */
/* The client                                                                 */
/* -------------------------------------------------------------------------- */

async function request<T>(
  method: 'GET' | 'POST',
  path: string,
  options: { token?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const started = performance.now();
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  if (options.token) headers.Authorization = `Bearer ${options.token}`;

  let response: Response;
  try {
    response = await fetch(`${API_ORIGIN}${path}`, {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (cause) {
    // A transport failure has no status and no request id; record it as 0 so
    // the panel shows the attempt rather than silently dropping it.
    observer?.({ method, path, status: 0, ms: Math.round(performance.now() - started), at: Date.now() });
    throw new ApiError({ status: 0, code: 'network_error', detail: String(cause) });
  }

  const ms = Math.round(performance.now() - started);
  const requestId = response.headers.get('x-request-id') ?? undefined;

  if (!response.ok) {
    const problem = await readProblem(response, requestId);
    observer?.({
      method,
      path,
      status: response.status,
      ms,
      requestId,
      code: problem.code,
      at: Date.now(),
    });
    throw new ApiError(problem, retryAfterSeconds(response));
  }

  observer?.({ method, path, status: response.status, ms, requestId, at: Date.now() });
  return (await response.json()) as T;
}

/**
 * Every error leaves the service as `application/problem+json`. The fallback
 * below is for the one case the service cannot cause: a proxy or a captive
 * portal answering in its place with HTML.
 */
async function readProblem(response: Response, requestId?: string): Promise<Problem> {
  try {
    const body = (await response.json()) as Partial<Problem>;
    if (typeof body?.code === 'string') {
      return { ...body, status: body.status ?? response.status, code: body.code } as Problem;
    }
  } catch {
    /* fall through */
  }
  return {
    status: response.status,
    code: 'unexpected_response',
    detail: `The service answered ${response.status} without a problem+json body.`,
    request_id: requestId,
  };
}

export const api = {
  /** Provisions a throwaway tenant with its own copy of the corpus, and a token for it. */
  demoLogin(label: string): Promise<DemoLoginResponse> {
    return request<DemoLoginResponse>('POST', '/api/v1/auth/demo-login', {
      body: { label } satisfies DemoLoginRequest,
    });
  },

  createConversation(token: string, title: string): Promise<CreateConversationResponse> {
    return request<CreateConversationResponse>('POST', '/api/v1/conversations', {
      token,
      body: { title } satisfies CreateConversationRequest,
    });
  },

  /**
   * 202: the question is stored, nothing is generated yet. The answer arrives on the stream.
   *
   * `options` is a bag, not a positional parameter: `probeCrossTenant` below calls this with
   * three positional arguments, and CI runs `tsc --noEmit` — a required fourth positional
   * parameter would break that call site. `mode` and `generator` are both optional, so
   * neither caller has to know about the other's field.
   */
  postMessage(
    token: string,
    conversationId: string,
    content: string,
    options: { mode?: SearchMode; generator?: GeneratorChoice } = {},
  ): Promise<CreateMessageResponse> {
    return request<CreateMessageResponse>('POST', `/api/v1/conversations/${conversationId}/messages`, {
      token,
      body: { content, ...options } satisfies CreateMessageRequest,
    });
  },

  /**
   * Mint immediately before opening the stream, never when the message is
   * created: the ticket is bound to this one message and lives `expires_in`
   * seconds.
   */
  streamTicket(token: string, messageId: string): Promise<StreamTicketResponse> {
    return request<StreamTicketResponse>('POST', '/api/v1/auth/stream-tickets', {
      token,
      body: { message_id: messageId } satisfies StreamTicketRequest,
    });
  },

  /**
   * Which generators would actually work right now — not all three with a
   * flag, only the ones that would. With no `GEMINI_API_KEY` on the server
   * this is `[{ value: 'deterministic', ... }]`, which is what lets a clone
   * with zero API keys still offer a working chat and simply not offer a
   * Gemini option to click. No token: this route is public, like `/status`.
   */
  listGenerators(): Promise<GeneratorsResponse> {
    return request<GeneratorsResponse>('GET', '/api/v1/generators');
  },

  /**
   * Admits, queues and dispatches an ingest of one catalogue source into the
   * caller's organisation. Returns the same shape `ingestStatus` does — the
   * initial `queued` snapshot, not a job id (see `backend/app/api/v1/ingest.py`
   * for why `job_id` was deliberately removed). `409 already_ingested` if this
   * organisation already holds *this* document — a tenant may hold every
   * source in the catalogue, one ingest per (tenant, source).
   */
  startIngest(token: string, sourceKey: string): Promise<IngestStatusResponse> {
    return request<IngestStatusResponse>('POST', '/api/v1/ingest', {
      token,
      body: { source_key: sourceKey } satisfies IngestRequest,
    });
  },

  /**
   * Every catalogue source's ingest status for this organisation, keyed by
   * source. Meant to be polled — see `components/documents.tsx`, which
   * polls it twice a second and is the reason this route is excluded from the
   * request histogram (`app/observability.py`'s `_NOT_MEASURED`).
   *
   * One request describes the whole catalogue, which is what keeps that
   * exclusion honest: a `?source_key=` route would have cost one request per
   * document at the same poll rate.
   */
  ingestStatus(token: string): Promise<IngestStatusMap> {
    return request<IngestStatusMap>('GET', '/api/v1/ingest/status', { token });
  },

  /**
   * The documents this deployment can ingest. Unauthenticated: it names no
   * organisation and reads no org-scoped table — see the route's own docstring
   * and its entry in `EXPECTED_ROUTES`.
   */
  catalogue(): Promise<CatalogueEntry[]> {
    return request<CatalogueEntry[]>('GET', '/api/v1/catalogue');
  },

  /**
   * The committed retrieval measurement. No token: it holds numbers about this
   * project's own copy of the public corpus, not about any tenant.
   *
   * Through `request` rather than a bare `fetch`, and that is not a style
   * preference. `request` feeds the observer the panel's request timeline is
   * built from, and it parses `application/problem+json` — so a direct `fetch`
   * would drop this call out of the timeline and turn the `503` this route
   * answers when a build lacks the artefact into an empty table with no
   * explanation.
   */
  evals(): Promise<EvalsResponse> {
    return request<EvalsResponse>('GET', '/api/v1/evals');
  },
};

export function streamUrl(conversationId: string, messageId: string, ticket: string): string {
  return (
    `${API_ORIGIN}/api/v1/conversations/${conversationId}` +
    `/messages/${messageId}/stream?ticket=${encodeURIComponent(ticket)}`
  );
}

/* -------------------------------------------------------------------------- */
/* Isolation probe                                                            */
/* -------------------------------------------------------------------------- */

export interface ProbeResult {
  status: number;
  code: string;
  requestId?: string;
  detail?: string;
}

/**
 * Asks the API, with tenant B's own valid token, for a conversation that
 * belongs to tenant A.
 *
 * The expected answer is `404 conversation_not_found` — byte-identical to the
 * answer for a conversation that never existed, because the lookup runs inside
 * B's row-level-security context and A's row is simply not in the result set.
 * A `403` here would be a worse answer: it would confirm the resource exists.
 *
 * This is a real request against the live service, and its `request_id` is the
 * one printed in the panel. Nothing about it is simulated.
 */
export async function probeCrossTenant(
  intruderToken: string,
  foreignConversationId: string,
): Promise<ProbeResult> {
  try {
    await api.postMessage(intruderToken, foreignConversationId, 'cross-tenant isolation probe');
    // Reaching here means the write succeeded across tenants. That is the
    // failure case, and the panel must say so rather than quietly showing 404.
    return { status: 202, code: 'isolation_breach' };
  } catch (error) {
    if (error instanceof ApiError) {
      return {
        status: error.status,
        code: error.code,
        requestId: error.requestId,
        detail: error.problem.detail,
      };
    }
    throw error;
  }
}
