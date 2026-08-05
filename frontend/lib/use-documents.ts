'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError, type CatalogueEntry, type IngestStatusMap } from '@/lib/api';
import type { Tenant } from '@/lib/use-lexloom';

/**
 * What this deployment can ingest, and what this tenant has done about it.
 *
 * The state is a hook rather than component state because two parts of the
 * document column read it and only one of them writes: the cards in the header
 * offer the ingest, and the empty state offers the questions that become worth
 * asking once a document has landed. Calling this hook twice would poll twice,
 * so `chat.tsx` calls it once and hands the result to both.
 */

const POLL_MS = 500;

export interface DocumentsState {
  catalogue: CatalogueEntry[];
  statuses: IngestStatusMap;
  /** The source whose `POST /ingest` is in flight, if any. */
  starting: string | null;
  startError: string | null;
  start: (sourceKey: string) => void;
  /**
   * Sources this tenant holds in full that carry questions worth offering, in
   * catalogue order. Not "sources with a complete stage" — a document with no
   * suggestions has nothing to add to the empty state.
   */
  suggesting: CatalogueEntry[];
}

/**
 * Plain fetch, deliberately not `api.ingestStatus` — the same reasoning as
 * `panel/service-status.tsx`'s own poll: every call through `lib/api.ts` is
 * reported to the panel's REQUESTS block, and a poll every 500 ms would fill a
 * visitor's request log with this watching itself. The one-shot `POST` that
 * starts an ingest is a real, rare, visitor-initiated action and does go
 * through `api.startIngest`.
 */
async function pollStatus(token: string): Promise<IngestStatusMap | null> {
  try {
    const response = await fetch('/api/v1/ingest/status', {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) return null;
    return (await response.json()) as IngestStatusMap;
  } catch {
    return null;
  }
}

function errorDetail(error: unknown): string {
  if (error instanceof ApiError) return error.problem.detail ?? error.message;
  return String(error);
}

export function useDocuments(tenant: Tenant | null): DocumentsState {
  const token = tenant?.token ?? null;
  const orgId = tenant?.orgId ?? null;

  const [catalogue, setCatalogue] = useState<CatalogueEntry[]>([]);
  const [statuses, setStatuses] = useState<IngestStatusMap>({});
  const [starting, setStarting] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);

  // The catalogue is a property of the deployment, not of the tenant, so it is
  // fetched once and never re-fetched on a tenant switch.
  useEffect(() => {
    let cancelled = false;
    api
      .catalogue()
      .then((entries) => {
        if (!cancelled) setCatalogue(entries);
      })
      .catch(() => {
        /* The section renders nothing rather than a guess. */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // One read per organisation, not a poll: a status this session did not itself
  // start — the same tenant reached again after a re-render — still has to show
  // what actually happened rather than default to "not ingested" until a click.
  useEffect(() => {
    setStatuses({});
    setStartError(null);
    if (!token) return;
    let cancelled = false;
    pollStatus(token).then((body) => {
      if (!cancelled && body) setStatuses(body);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId]);

  const inFlight = Object.values(statuses).some(
    (status) => status.stage !== 'idle' && status.stage !== 'complete' && status.stage !== 'error',
  );

  useEffect(() => {
    if (!token || !inFlight) return;
    const id = setInterval(() => {
      // A hidden tab polls nothing — the demo box is shared, and a tab left
      // open for a day is otherwise over a hundred thousand requests.
      if (document.visibilityState !== 'visible') return;
      pollStatus(token).then((body) => {
        if (body) setStatuses(body);
      });
    }, POLL_MS);
    return () => clearInterval(id);
  }, [token, inFlight]);

  const start = useCallback(
    (sourceKey: string) => {
      if (!token) return;
      setStarting(sourceKey);
      setStartError(null);
      api
        .startIngest(token, sourceKey)
        .then((status) => {
          setStatuses((current) => ({ ...current, [sourceKey]: status }));
        })
        .catch((error: unknown) => {
          setStartError(errorDetail(error));
        })
        .finally(() => {
          setStarting(null);
        });
    },
    [token],
  );

  const suggesting = useMemo(
    () =>
      catalogue.filter(
        (entry) => entry.suggestions.length > 0 && statuses[entry.key]?.stage === 'complete',
      ),
    [catalogue, statuses],
  );

  return { catalogue, statuses, starting, startError, start, suggesting };
}
