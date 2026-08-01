'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ApiError,
  api,
  observeRequests,
  probeCrossTenant,
  streamUrl,
  type Citation,
  type ProbeResult,
  type RequestRecord,
} from './api';
import { diagnoseStreamFailure, openAnswerStream, type Frame, type StreamHandle } from './stream';

/**
 * All the session state the page has, and the only place that talks to the API.
 *
 * There are no read endpoints on this service, so this hook *is* the history:
 * a reload loses the transcript even though the rows survive in the database.
 * That is a property of the API (`docs/api-contract.md` §9), not an oversight
 * here.
 */

export type TurnStatus = 'sending' | 'streaming' | 'complete' | 'failed' | 'cancelled';

export interface Turn {
  id: string;
  /** The tenant that asked. Citations are checked against this. */
  orgId: string;
  question: string;
  messageId?: string;
  model?: string;
  citations: Citation[] | null;
  answer: string;
  tokens: number;
  /** Named frames in arrival order, with their measured offsets. */
  frames: Frame[];
  /**
   * Measured offsets from the moment the `EventSource` was constructed. `start`
   * is the arrival of `message_start`, and it is not zero: it carries the
   * connection setup, which is why the timeline plots it where it landed rather
   * than at the origin.
   */
  timings: { start?: number; citations?: number; firstToken?: number; lastToken?: number; end?: number };
  status: TurnStatus;
  finishReason?: string;
  /** The server's own `event: error` frame, if one arrived. */
  serverError?: { code: string; message: string };
  /** A connection-level failure, with whatever diagnosis could be recovered. */
  failure?: { summary: string; code?: string; requestId?: string; detail?: string };
}

export interface Tenant {
  orgId: string;
  orgName: string;
  token: string;
  /** Epoch ms, from the login response's `expires_in`. */
  tokenExpiresAt: number;
  conversationId?: string;
}

export interface IsolationReport {
  /** Distinct `org_id` values seen on any citation, across every tenant used. */
  orgsSeen: string[];
  /** Citation rows received in total. */
  rows: number;
  /**
   * Tenants that actually received citations — not tenants created. It is
   * printed next to `orgsSeen.length`, and the two matching is what says each
   * tenant saw only its own rows, so counting an idle tenant here would make
   * the pair say nothing.
   */
  tenantsAsked: number;
  /** Rows whose `org_id` was not the asking tenant's. This is the number that must stay 0. */
  foreignRows: number;
  probe: { intruder: string; target: string; result: ProbeResult } | null;
  probeRunning: boolean;
}

const TENANT_LABELS = ['Acme Legal', 'Brightwater Partners', 'Halloran & Voss'];

export function useLexloom() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [activeOrgId, setActiveOrgId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [requests, setRequests] = useState<RequestRecord[]>([]);
  const [probe, setProbe] = useState<IsolationReport['probe']>(null);
  const [probeRunning, setProbeRunning] = useState(false);
  // No `busy` state: it is derived per tenant, further down. A single global
  // flag meant that switching tenants mid-answer showed the newly selected
  // tenant a Stop button wired to somebody else's stream.
  const [creatingTenant, setCreatingTenant] = useState(false);
  const [fatal, setFatal] = useState<{ code: string; message: string } | null>(null);

  const streamRef = useRef<StreamHandle | null>(null);
  const bootstrapped = useRef(false);
  const requestSeq = useRef(0);

  // Read inside event handlers, which always run after the effect below has
  // flushed, so these are current whenever a handler fires.
  const tenantsRef = useRef(tenants);
  const activeOrgRef = useRef(activeOrgId);
  useEffect(() => {
    tenantsRef.current = tenants;
  }, [tenants]);
  useEffect(() => {
    activeOrgRef.current = activeOrgId;
  }, [activeOrgId]);

  /* ---------------------------------------------------------------------- */
  /* Every HTTP call this page makes is recorded, including the failed ones. */
  /* ---------------------------------------------------------------------- */
  useEffect(() => {
    observeRequests((record) => {
      requestSeq.current += 1;
      const entry: RequestRecord = { ...record, id: requestSeq.current };
      setRequests((prev) => [...prev.slice(-40), entry]);
    });
    return () => observeRequests(null);
  }, []);

  const patchTurn = useCallback(
    (turnId: string, patch: Partial<Turn> | ((turn: Turn) => Partial<Turn>)) => {
      setTurns((prev) =>
        prev.map((turn) =>
          turn.id === turnId ? { ...turn, ...(typeof patch === 'function' ? patch(turn) : patch) } : turn,
        ),
      );
    },
    [],
  );

  /* ---------------------------------------------------------------------- */
  /* Tenants                                                                */
  /* ---------------------------------------------------------------------- */

  const addTenant = useCallback(async (): Promise<Tenant | null> => {
    setCreatingTenant(true);
    try {
      const label = TENANT_LABELS[tenantsRef.current.length] ?? `Tenant ${tenantsRef.current.length + 1}`;
      const login = await api.demoLogin(label);
      const tenant: Tenant = {
        orgId: login.org_id,
        orgName: login.org_name,
        token: login.access_token,
        tokenExpiresAt: Date.now() + login.expires_in * 1000,
      };
      setTenants((prev) => [...prev, tenant]);
      setActiveOrgId((prev) => prev ?? tenant.orgId);
      setFatal(null);
      return tenant;
    } catch (error) {
      setFatal(describe(error, 'The service would not create a demo tenant.'));
      return null;
    } finally {
      setCreatingTenant(false);
    }
  }, []);

  useEffect(() => {
    // Strict Mode invokes effects twice in development; without this guard the
    // visitor would silently get two tenants before touching anything.
    if (bootstrapped.current) return;
    bootstrapped.current = true;
    void addTenant();
  }, [addTenant]);

  useEffect(() => () => streamRef.current?.close(), []);

  /* ---------------------------------------------------------------------- */
  /* Asking                                                                 */
  /* ---------------------------------------------------------------------- */

  const ask = useCallback(
    async (question: string) => {
      const trimmed = question.trim();
      const tenant = tenantsRef.current.find((t) => t.orgId === activeOrgRef.current);
      if (!trimmed || !tenant) return;

      // Asking again while an answer is still arriving hangs up the old stream.
      // The abandoned message stores nothing and stays re-streamable.
      streamRef.current?.close();

      const turnId = `${tenant.orgId}:${requestSeq.current}:${Date.now()}`;
      setTurns((prev) => [
        ...prev,
        {
          id: turnId,
          orgId: tenant.orgId,
          question: trimmed,
          citations: null,
          answer: '',
          tokens: 0,
          frames: [],
          timings: {},
          status: 'sending',
        },
      ]);

      try {
        const conversationId = tenant.conversationId ?? (await createConversation(tenant));
        const { message_id: messageId } = await api.postMessage(tenant.token, conversationId, trimmed);
        // Minted here rather than earlier: the ticket is bound to this message
        // and its clock is already running.
        const { ticket, expires_in: lifetime } = await api.streamTicket(tenant.token, messageId);
        patchTurn(turnId, { messageId, status: 'streaming' });

        streamRef.current = openAnswerStream(streamUrl(conversationId, messageId, ticket), lifetime, {
          onFrame: (frame) => patchTurn(turnId, (turn) => ({ frames: [...turn.frames, frame] })),
          onStart: (data, at) =>
            patchTurn(turnId, (turn) => ({
              model: data.model,
              timings: { ...turn.timings, start: at },
            })),
          onCitations: (chunks, at) =>
            patchTurn(turnId, (turn) => ({
              citations: chunks,
              timings: { ...turn.timings, citations: at },
            })),
          onToken: (text, at) =>
            patchTurn(turnId, (turn) => ({
              answer: turn.answer + text,
              tokens: turn.tokens + 1,
              timings: { ...turn.timings, firstToken: turn.timings.firstToken ?? at, lastToken: at },
            })),
          onServerError: (problem) => patchTurn(turnId, { serverError: problem }),
          onEnd: (outcome) => {
            // Every branch below leaves a terminal status on the turn, which is
            // what clears the composer for this tenant.
            if (outcome.kind === 'complete') {
              patchTurn(turnId, (turn) => ({
                status: 'complete',
                finishReason: outcome.finishReason,
                timings: { ...turn.timings, end: outcome.at },
              }));
              return;
            }
            if (outcome.kind === 'aborted') {
              patchTurn(turnId, { status: 'cancelled' });
              return;
            }
            if (outcome.kind === 'timeout') {
              patchTurn(turnId, {
                status: 'failed',
                failure: {
                  summary:
                    outcome.reason === 'connect'
                      ? 'The stream never opened before the ticket expired.'
                      : 'The stream went quiet and was closed.',
                },
              });
              return;
            }
            // Transport. EventSource hides the status, so go and ask for it.
            patchTurn(turnId, {
              status: 'failed',
              failure: { summary: 'The stream connection failed. Asking the service why…' },
            });
            void diagnoseStreamFailure(tenant.token, conversationId, messageId).then((diagnosis) =>
              patchTurn(turnId, {
                failure: {
                  summary: explainDiagnosis(diagnosis),
                  code: diagnosis.code,
                  requestId: diagnosis.requestId,
                  detail: diagnosis.detail,
                },
              }),
            );
          },
        });
      } catch (error) {
        const described = describe(error, 'The question could not be sent.');
        patchTurn(turnId, {
          status: 'failed',
          failure: {
            summary: described.message,
            code: described.code,
            requestId: error instanceof ApiError ? error.requestId : undefined,
          },
        });
        // A 401 is terminal for this credential: the API rotates its signing
        // key on restart, so every outstanding token stops verifying at once.
        if (error instanceof ApiError && error.status === 401) setFatal(described);
      }

      async function createConversation(forTenant: Tenant): Promise<string> {
        const { id } = await api.createConversation(forTenant.token, 'Demo session');
        setTenants((prev) =>
          prev.map((t) => (t.orgId === forTenant.orgId ? { ...t, conversationId: id } : t)),
        );
        tenantsRef.current = tenantsRef.current.map((t) =>
          t.orgId === forTenant.orgId ? { ...t, conversationId: id } : t,
        );
        return id;
      }
    },
    [patchTurn],
  );

  const cancel = useCallback(() => streamRef.current?.close(), []);

  /* ---------------------------------------------------------------------- */
  /* Isolation                                                              */
  /* ---------------------------------------------------------------------- */

  /**
   * Runs once there are two tenants and something for the second one to fail
   * to reach. It is a real request with a real token, and the `request_id` the
   * panel prints is the one the service logged.
   */
  useEffect(() => {
    if (probe || probeRunning || tenants.length < 2) return;
    const target = tenants.find((t) => t.conversationId);
    const intruder = tenants.find((t) => t.orgId !== target?.orgId);
    if (!target?.conversationId || !intruder) return;

    setProbeRunning(true);
    void probeCrossTenant(intruder.token, target.conversationId)
      .then((result) => setProbe({ intruder: intruder.orgName, target: target.orgName, result }))
      .finally(() => setProbeRunning(false));
  }, [tenants, probe, probeRunning]);

  const isolation = useMemo<IsolationReport>(() => {
    const orgsSeen = new Set<string>();
    const askers = new Set<string>();
    let rows = 0;
    let foreignRows = 0;
    for (const turn of turns) {
      if (turn.citations === null) continue;
      askers.add(turn.orgId);
      for (const citation of turn.citations) {
        rows += 1;
        orgsSeen.add(citation.org_id);
        if (citation.org_id !== turn.orgId) foreignRows += 1;
      }
    }
    return {
      orgsSeen: [...orgsSeen],
      rows,
      tenantsAsked: askers.size,
      foreignRows,
      probe,
      probeRunning,
    };
  }, [turns, probe, probeRunning]);

  const activeTenant = tenants.find((t) => t.orgId === activeOrgId) ?? null;
  const activeTurns = useMemo(
    () => turns.filter((turn) => turn.orgId === activeOrgId),
    [turns, activeOrgId],
  );
  const currentTurn = activeTurns.length > 0 ? activeTurns[activeTurns.length - 1] : null;
  /** Another tenant in this session has asked something, and this one has not. */
  const elsewhereHasHistory = activeTurns.length === 0 && turns.length > 0;
  /**
   * Busy is a property of the selected tenant, not of the page: a stream still
   * running for the tenant you just switched away from must not put a Stop
   * button in front of the tenant you switched to.
   */
  const busy = activeTurns.some((turn) => turn.status === 'sending' || turn.status === 'streaming');

  return {
    tenants,
    activeTenant,
    activeTurns,
    currentTurn,
    elsewhereHasHistory,
    requests,
    isolation,
    busy,
    creatingTenant,
    fatal,
    ask,
    cancel,
    addTenant,
    switchTenant: setActiveOrgId,
  };
}

function describe(error: unknown, fallback: string): { code: string; message: string } {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return {
        code: error.code,
        message:
          'This tenant’s token is no longer accepted. The service mints its signing key in memory, so a restart invalidates every token that was issued before it. Create a tenant to carry on.',
      };
    }
    if (error.code === 'network_error') {
      return { code: error.code, message: 'The service did not answer. Check that the API is running.' };
    }
    if (error.status === 429 || error.status === 503) {
      return { code: error.code, message: refusal(error.status, error.retryAfter) };
    }
    return { code: error.code, message: error.problem.detail ?? fallback };
  }
  return { code: 'unknown_error', message: fallback };
}

/** "in 5 seconds", or "in a moment" when the service did not say. */
function waitFor(seconds?: number): string {
  if (seconds === undefined) return 'in a moment';
  if (seconds <= 1) return 'in a second';
  return `in ${seconds} seconds`;
}

/**
 * The two refusals, written for a visitor.
 *
 * Neither can render `problem.detail`: for both of them it is literally "Retry
 * after Retry-After seconds." — the number lives in the header, deliberately,
 * because prose is not a contract (`docs/api-contract.md` §6.1). Falling
 * through to `detail`, as this page used to, shows a visitor the words
 * "Retry-After" and no number. Someone opening a fifth tab in one tenant is
 * exactly the person who sees this.
 */
function refusal(status: number, retryAfter?: number): string {
  if (status === 429) {
    return `This address has created as many demo tenants as it is allowed for now — the limit is per address, so other tabs and other browsers on this connection share it. Try again ${waitFor(retryAfter)}.`;
  }
  return `The service declined to start this answer: it is at capacity, most often because four answers are already streaming in this tenant. Try again ${waitFor(retryAfter)}.`;
}

function explainDiagnosis(diagnosis: {
  status: number;
  code: string;
  detail?: string;
  retryAfter?: number;
}): string {
  switch (diagnosis.code) {
    case 'message_already_answered':
      return 'The service had already stored an answer for this message, so the replay was refused.';
    case 'stream_available':
      return 'The connection dropped, but the stream is still there — nothing was consumed.';
    case 'network_error':
      return 'The service could not be reached at all.';
    case 'capacity_unavailable':
    case 'rate_limited':
      return refusal(diagnosis.status, diagnosis.retryAfter);
    default:
      return diagnosis.detail ?? `The stream was refused with ${diagnosis.status} ${diagnosis.code}.`;
  }
}
