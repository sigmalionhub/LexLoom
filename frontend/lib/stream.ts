import { ApiError, api, retryAfterSeconds, streamUrl, type Citation, type Problem } from './api';
import type { SearchMode } from './search-mode';

/**
 * Consuming the answer stream.
 *
 * `EventSource` is used rather than `fetch` + `ReadableStream` because it is
 * what the browser gives for free: reconnection policy, CRLF framing, comment
 * handling. The price is two behaviours that bite in exactly the wrong moment,
 * and both are handled below rather than hoped away.
 */

export type FrameName =
  | 'message_start'
  | 'citations'
  | 'generator_changed'
  | 'token'
  | 'error'
  | 'message_end';

/** Every named frame, in arrival order, with the offset the panel plots. */
export interface Frame {
  name: FrameName;
  /** Milliseconds since the `EventSource` was constructed. */
  at: number;
}

export type StreamOutcome =
  /** `message_end` arrived. `finishReason` is `"stop"` or `"error"`. */
  | { kind: 'complete'; finishReason: string; at: number }
  /** The connection failed or was refused. `EventSource` hides the HTTP status. */
  | { kind: 'transport'; readyState: number; at: number; framesSeen: number }
  /** No frame at all, or no frame for a long time. */
  | { kind: 'timeout'; reason: 'connect' | 'idle'; at: number }
  /** The caller closed it: a new question, or the component going away. */
  | { kind: 'aborted'; at: number };

export interface StreamHandlers {
  /** Fires for every named frame before the specific handler below. */
  onFrame?(frame: Frame): void;
  onStart?(data: { message_id: string; model: string }, at: number): void;
  /**
   * `mode` is the retrieval path the sources were actually fetched with — the
   * shape matches `onStart`, so a later field on this frame will not need
   * another arity change. The panel shows this rather than the switch's own
   * position, so a broken carrier is visible instead of invisible.
   */
  onCitations?(data: { chunks: Citation[]; mode: SearchMode }, at: number): void;
  /**
   * The provider named on `message_start` could not answer and the server
   * fell back mid-stream — today that only ever means a Gemini choice hit its
   * free-tier quota (HTTP 429) and the answer continued on the deterministic
   * provider. Always arrives after `onCitations` and before the first
   * `onToken`, matching the wire contract in `docs/api-contract.md` §4.2.
   */
  onGeneratorChanged?(data: { model: string; reason: string }, at: number): void;
  onToken?(text: string, at: number): void;
  /** The server's own `event: error` frame. `message_end` still follows it. */
  onServerError?(problem: { code: string; message: string }, at: number): void;
  onEnd?(outcome: StreamOutcome): void;
}

export interface StreamHandle {
  close(): void;
}

/**
 * A stream that has started but produced no frame for this long is treated as
 * dead. It has to be a frame watchdog rather than a byte watchdog: the service
 * sends a `: ping` comment every 15 s, but the EventSource parser swallows
 * comments without dispatching anything, so a client on this API cannot see
 * them. The bundled provider finishes a whole answer in about 1.3 s, so this
 * bound is two orders of magnitude clear of normal.
 */
const IDLE_LIMIT_MS = 30_000;

export function openAnswerStream(
  url: string,
  ticketLifetimeSeconds: number,
  handlers: StreamHandlers,
): StreamHandle {
  const openedAt = performance.now();
  const since = () => Math.round(performance.now() - openedAt);

  const source = new EventSource(url);
  let settled = false;
  let framesSeen = 0;
  let idleTimer: ReturnType<typeof setTimeout> | undefined;
  let connectTimer: ReturnType<typeof setTimeout> | undefined;

  /**
   * Every terminal path runs through here, and every terminal path closes the
   * connection.
   *
   * This is not tidiness. `EventSource` reconnects whenever the connection
   * ends — including when it ends because the answer finished — and it does so
   * roughly every three seconds with a ticket that is already spent. On this
   * API the replay guard answers `409` and a non-200 permanently fails an
   * `EventSource`, so today the cost of forgetting is one wasted request. That
   * is a measurement of the current backend, not a property of the client, and
   * it would stop holding the moment an error path started answering `200`.
   * So: close explicitly, on completion, on failure, on timeout, on abort.
   */
  const settle = (outcome: StreamOutcome) => {
    if (settled) return;
    settled = true;
    if (idleTimer) clearTimeout(idleTimer);
    if (connectTimer) clearTimeout(connectTimer);
    source.close();
    handlers.onEnd?.(outcome);
  };

  /**
   * Nothing at all has arrived and the ticket has expired, so reconnecting
   * could not succeed even if we let it. Timed off the ticket's real
   * `expires_in` rather than a hard-coded 60.
   */
  connectTimer = setTimeout(
    () => {
      if (framesSeen === 0) settle({ kind: 'timeout', reason: 'connect', at: since() });
    },
    Math.max(5_000, ticketLifetimeSeconds * 1000),
  );

  const armIdleTimer = () => {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => settle({ kind: 'timeout', reason: 'idle', at: since() }), IDLE_LIMIT_MS);
  };

  const record = (name: FrameName): number => {
    framesSeen += 1;
    armIdleTimer();
    const at = since();
    handlers.onFrame?.({ name, at });
    return at;
  };

  /** A frame whose `data` is not the JSON the contract promises is a bug, not a stream state. */
  const parse = <T>(raw: string, name: FrameName): T | null => {
    try {
      return JSON.parse(raw) as T;
    } catch {
      console.error(`lexloom: unparseable data on "${name}" frame`, raw);
      return null;
    }
  };

  /** `data` is always a single line of JSON on this API, so no reassembly is needed. */
  const dataOf = (event: Event): string => (event as MessageEvent<string>).data;

  source.addEventListener('message_start', (event) => {
    const at = record('message_start');
    const data = parse<{ message_id: string; model: string }>(dataOf(event), 'message_start');
    if (data) handlers.onStart?.(data, at);
  });

  source.addEventListener('citations', (event) => {
    const at = record('citations');
    const data = parse<{ chunks: Citation[]; mode: SearchMode }>(dataOf(event), 'citations');
    // An empty array is a normal answer, not a failure: the question's terms
    // are simply not in this tenant's corpus.
    if (data) handlers.onCitations?.({ chunks: data.chunks ?? [], mode: data.mode }, at);
  });

  source.addEventListener('generator_changed', (event) => {
    const at = record('generator_changed');
    const data = parse<{ model: string; reason: string }>(dataOf(event), 'generator_changed');
    if (data) handlers.onGeneratorChanged?.(data, at);
  });

  source.addEventListener('token', (event) => {
    const at = record('token');
    const data = parse<{ text: string }>(dataOf(event), 'token');
    // Concatenated verbatim: the leading and trailing spaces are payload.
    if (data) handlers.onToken?.(data.text, at);
  });

  /**
   * BUG THE FIRST — this listener receives two unrelated things.
   *
   * The server's failure notice arrives here as a `MessageEvent` carrying
   * `data`. But `EventSource` also dispatches its *own* connection-failure
   * event with the type `"error"`, to this same listener list, and that one is
   * a plain `Event` with no `data` at all. `JSON.parse(event.data)` therefore
   * throws precisely when the network has dropped — the moment a second,
   * unrelated exception is least welcome.
   *
   * They are told apart structurally, by whether the event carries data. The
   * alternative — renaming the server's frame to something that cannot collide —
   * would change a documented wire contract to work around a client-side
   * ambiguity, so the client absorbs it.
   */
  source.addEventListener('error', (event) => {
    if (!('data' in event)) {
      // Transport. readyState 0 = CONNECTING, meaning the browser intends to
      // retry; settle() closes it before it can. readyState 2 = CLOSED, the
      // browser has already given up (a non-200 response does this).
      settle({ kind: 'transport', readyState: source.readyState, at: since(), framesSeen });
      return;
    }
    const at = record('error');
    const data = parse<{ code: string; message: string }>(dataOf(event), 'error');
    // Deliberately not terminal: the contract guarantees `message_end` still
    // arrives, with `finish_reason: "error"`, and the tokens produced before
    // the failure are kept. Closing here would throw away that ending.
    if (data) handlers.onServerError?.(data, at);
  });

  source.addEventListener('message_end', (event) => {
    const at = record('message_end');
    const data = parse<{ finish_reason: string }>(dataOf(event), 'message_end');
    settle({ kind: 'complete', finishReason: data?.finish_reason ?? 'unknown', at });
  });

  return {
    close: () => settle({ kind: 'aborted', at: since() }),
  };
}

/* -------------------------------------------------------------------------- */
/* Turning an opaque transport failure into a real diagnosis                  */
/* -------------------------------------------------------------------------- */

export interface StreamDiagnosis {
  status: number;
  code: string;
  requestId?: string;
  detail?: string;
  /** Seconds, from `Retry-After`. Present on `429` and `503`. */
  retryAfter?: number;
}

/**
 * `EventSource` gives a client no way to read the status or the body of a
 * refused stream: a `409` and a pulled network cable produce the same bare
 * `Event`. When one comes back, this re-asks the same question with `fetch`,
 * which does expose both, and reports what the service actually said.
 *
 * It costs one extra request and it is safe to make: a stream that is aborted
 * before it completes stores nothing and leaves the message re-streamable
 * (`docs/api-contract.md` §5.3), so the `AbortController` below fires as soon
 * as the headers are in and never reads a byte of the body on the happy path.
 *
 * A fresh ticket is minted because the original may well have expired — which
 * is one of the things this is trying to find out.
 */
export async function diagnoseStreamFailure(
  token: string,
  conversationId: string,
  messageId: string,
): Promise<StreamDiagnosis> {
  let ticket: string;
  try {
    ticket = (await api.streamTicket(token, messageId)).ticket;
  } catch (error) {
    if (error instanceof ApiError) {
      return {
        status: error.status,
        code: error.code,
        requestId: error.requestId,
        detail: error.problem.detail,
        retryAfter: error.retryAfter,
      };
    }
    throw error;
  }

  const controller = new AbortController();
  try {
    const response = await fetch(streamUrl(conversationId, messageId, ticket), {
      headers: { Accept: 'text/event-stream' },
      signal: controller.signal,
    });
    const requestId = response.headers.get('x-request-id') ?? undefined;

    if (response.ok) {
      controller.abort();
      return {
        status: response.status,
        code: 'stream_available',
        requestId,
        detail: 'The stream reopens, so the failure was in the connection, not in the request.',
      };
    }

    const problem = (await response.json()) as Partial<Problem>;
    return {
      status: response.status,
      code: problem.code ?? 'unexpected_response',
      requestId: problem.request_id ?? requestId,
      detail: problem.detail,
      // A stream refused for capacity carries the wait here and nowhere else:
      // `detail` names the header rather than a number.
      retryAfter: retryAfterSeconds(response),
    };
  } catch (cause) {
    controller.abort();
    return { status: 0, code: 'network_error', detail: String(cause) };
  }
}
