import type { components } from './api-types';

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

/** One retrieved chunk, exactly as it arrives in the `citations` frame. */
export interface Citation {
  chunk_id: string;
  /** The tenant the row belongs to. The page counts these; see the panel. */
  org_id: string;
  content: string;
  page_from: number;
  page_to: number;
  /**
   * A raw Postgres `ts_rank`. Comparable inside one response and nowhere else,
   * so it is rendered as a rank position, never as a percentage.
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

export class ApiError extends Error {
  readonly problem: Problem;
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;

  constructor(problem: Problem) {
    super(problem.detail ?? problem.title ?? `HTTP ${problem.status}`);
    this.name = 'ApiError';
    this.problem = problem;
    this.status = problem.status;
    this.code = problem.code;
    this.requestId = problem.request_id;
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
    throw new ApiError(problem);
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

  /** 202: the question is stored, nothing is generated yet. The answer arrives on the stream. */
  postMessage(token: string, conversationId: string, content: string): Promise<CreateMessageResponse> {
    return request<CreateMessageResponse>('POST', `/api/v1/conversations/${conversationId}/messages`, {
      token,
      body: { content } satisfies CreateMessageRequest,
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
