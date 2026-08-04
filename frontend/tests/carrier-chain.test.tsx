import { act } from 'react';
import { cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GeneratorSelector } from '@/components/panel/generator-selector';
import { SearchMode as SearchModeSection } from '@/components/panel/search-mode';
import type { PanelSectionProps } from '@/components/panel/types';
import { readGenerator } from '@/lib/generator-choice';
import { readSearchMode } from '@/lib/search-mode';
import { useLexloom } from '@/lib/use-lexloom';

/**
 * The chain that must not break, in both directions (see task-43-brief.md):
 *
 *   user clicks a mode button
 *     -> components/panel/search-mode.tsx        onSelect -> writeSearchMode(next)
 *     -> lib/search-mode.ts                      writes ?mode=... via history.replaceState
 *     -> lib/use-lexloom.ts                       ask() reads it back via readSearchMode()
 *     -> lib/api.ts                               postMessage(..., { mode, generator })
 *     -> POST body carries the mode
 *
 * Every assertion below is on the observable end of its link, not on
 * component state and not on a mock of a `write*()` function: mocking
 * `writeSearchMode` and asserting it was called would still pass with the
 * URL write itself deleted, as long as the mock stayed imported.
 */

function panelProps(overrides: Partial<PanelSectionProps> = {}): PanelSectionProps {
  return {
    tenant: null,
    turn: null,
    requests: [],
    isolation: {
      orgsSeen: [],
      rows: 0,
      tenantsAsked: 0,
      foreignRows: 0,
      probe: null,
      probeRunning: false,
    },
    tenantCount: 0,
    ...overrides,
  };
}

function resetUrl(): void {
  window.history.replaceState(null, '', '/');
}

/** A minimal `Response`-shaped object — real enough for `lib/api.ts`'s `request()`. */
function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null } as unknown as Headers,
    json: async () => body,
  } as Response;
}

describe('SearchMode section: click writes the URL, not just component state', () => {
  beforeEach(resetUrl);

  it('writes ?mode= on click and readSearchMode() reads it back', () => {
    render(<SearchModeSection {...panelProps()} />);

    const vectorButton = screen.getByRole('radio', { name: /vector/i });
    fireEvent.click(vectorButton);

    expect(new URLSearchParams(window.location.search).get('mode')).toBe('vector');
    expect(readSearchMode()).toBe('vector');
  });
});

describe('GeneratorSelector section: click writes the URL, not just component state', () => {
  beforeEach(() => {
    resetUrl();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          generators: [
            { value: 'deterministic', label: 'Deterministic' },
            { value: 'gemini-3.5-flash-lite', label: 'Flash-Lite' },
            { value: 'gemini-3.6-flash', label: 'Flash' },
          ],
        }),
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('writes ?generator= on click and readGenerator() reads it back', async () => {
    render(<GeneratorSelector {...panelProps()} />);

    // The selector starts with only "deterministic" enabled and flips the
    // rest on once GET /api/v1/generators answers — the click has to wait
    // for that, the same way a real visitor's would. Matched on `textContent`
    // rather than the computed accessible name: the label and note live in
    // two adjacent, space-less `<span>`s, so "Flash" and "Flash-Lite" both
    // start with "Flash" in the flattened accessible name and a name-based
    // regex cannot tell them apart.
    const flashButton = await waitFor(() => {
      const radios = screen.getAllByRole('radio');
      const match = radios.find(
        (radio) => radio.textContent?.startsWith('Flash') && !radio.textContent?.startsWith('Flash-Lite'),
      );
      if (!match || match.getAttribute('aria-disabled') === 'true') {
        throw new Error('the non-default "Flash" generator is not yet enabled');
      }
      return match;
    });
    fireEvent.click(flashButton);

    expect(new URLSearchParams(window.location.search).get('generator')).toBe('gemini-3.6-flash');
    expect(readGenerator()).toBe('gemini-3.6-flash');
  });
});

describe('ask() sends the mode and generator that are in the URL', () => {
  let capturedBody: Record<string, unknown> | undefined;

  beforeEach(() => {
    resetUrl();
    capturedBody = undefined;
    // `new EventSource(...)` is not implemented by jsdom. `ask()` calls it
    // without awaiting, inside the same try/catch that awaits
    // `api.postMessage` above it, so the resulting throw is caught internally
    // and turns the turn's status to "failed" — after the request body this
    // test cares about has already been sent and captured below. Stubbing it
    // explicitly (rather than relying on it being merely absent) keeps that
    // behaviour deterministic regardless of what a given jsdom version does
    // or does not implement.
    vi.stubGlobal('EventSource', undefined);
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = init?.method ?? 'GET';

        if (url.endsWith('/api/v1/auth/demo-login')) {
          return jsonResponse({
            access_token: 'test-token',
            org_id: 'org-1',
            org_name: 'Acme Legal',
            expires_in: 3600,
          });
        }
        if (method === 'POST' && url.endsWith('/api/v1/conversations')) {
          return jsonResponse({ id: 'conv-1' });
        }
        if (method === 'POST' && /\/conversations\/[^/]+\/messages$/.test(url)) {
          capturedBody = JSON.parse(init?.body as string) as Record<string, unknown>;
          return jsonResponse({ message_id: 'msg-1' });
        }
        if (url.endsWith('/api/v1/auth/stream-tickets')) {
          return jsonResponse({ ticket: 'ticket-1', expires_in: 30 });
        }
        throw new Error(`unexpected fetch in this test: ${method} ${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('carries readSearchMode() and readGenerator() into the POST body', async () => {
    const { result } = renderHook(() => useLexloom());

    // The hook logs in a demo tenant on mount (see the `bootstrapped` guard
    // in use-lexloom.ts); ask() needs that tenant before it can do anything.
    await waitFor(() => expect(result.current.activeTenant).not.toBeNull());

    // Set the carriers the same way a click on the two selectors would,
    // without going through the components — this test's job is the other
    // end of the chain, already covered above.
    window.history.replaceState(null, '', '/?mode=vector&generator=gemini-3.6-flash');

    await act(async () => {
      await result.current.ask('does the URL survive to the request body?');
    });

    expect(capturedBody).toBeDefined();
    expect(capturedBody?.mode).toBe('vector');
    expect(capturedBody?.generator).toBe('gemini-3.6-flash');
  });
});

afterEach(() => {
  cleanup();
});
