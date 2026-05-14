import { describe, it, expect, vi, beforeEach } from 'vitest';
import { approvePlan, createTask, getGateHistory } from '@/lib/tasks';

// ADR-015 §2 / §6 / §7 Phase 12 — verify the lib functions hit the
// right URLs with the right bodies. The api() wrapper uses fetch under
// the hood; we stub global.fetch and assert the request shape.

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('createTask', () => {
  it('omits mode_override when not specified (inherit from repo)', async () => {
    const fetchMock = vi
      .spyOn(global, 'fetch')
      .mockResolvedValue(jsonResponse({ id: 1 }));
    await createTask({ title: 'hi' });
    const [, init] = fetchMock.mock.calls[0]!;
    const body = JSON.parse((init as RequestInit).body as string);
    expect(body).toEqual({ title: 'hi' });
  });

  it('includes mode_override when caller flips the toggle', async () => {
    const fetchMock = vi
      .spyOn(global, 'fetch')
      .mockResolvedValue(jsonResponse({ id: 1 }));
    await createTask({ title: 'hi', modeOverride: 'freeform' });
    const [, init] = fetchMock.mock.calls[0]!;
    const body = JSON.parse((init as RequestInit).body as string);
    expect(body.mode_override).toBe('freeform');
  });

  it('treats null mode_override as inherit', async () => {
    const fetchMock = vi
      .spyOn(global, 'fetch')
      .mockResolvedValue(jsonResponse({ id: 1 }));
    await createTask({ title: 'hi', modeOverride: null });
    const [, init] = fetchMock.mock.calls[0]!;
    const body = JSON.parse((init as RequestInit).body as string);
    expect('mode_override' in body).toBe(false);
  });
});

describe('approvePlan', () => {
  it('POSTs verdict + comments to /api/tasks/:id/approve-plan', async () => {
    const fetchMock = vi
      .spyOn(global, 'fetch')
      .mockResolvedValue(jsonResponse({ id: 42 }));
    await approvePlan(42, 'approved', 'LGTM');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/tasks/42/approve-plan');
    expect((init as RequestInit).method).toBe('POST');
    const body = JSON.parse((init as RequestInit).body as string);
    expect(body).toEqual({ verdict: 'approved', comments: 'LGTM' });
  });

  it('defaults comments to empty string', async () => {
    const fetchMock = vi
      .spyOn(global, 'fetch')
      .mockResolvedValue(jsonResponse({ id: 42 }));
    await approvePlan(42, 'rejected');
    const [, init] = fetchMock.mock.calls[0]!;
    const body = JSON.parse((init as RequestInit).body as string);
    expect(body.comments).toBe('');
  });
});

describe('getGateHistory', () => {
  it('GETs /api/tasks/:id/gate-history', async () => {
    const fetchMock = vi
      .spyOn(global, 'fetch')
      .mockResolvedValue(jsonResponse([]));
    await getGateHistory(7);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/tasks/7/gate-history');
    expect((init as RequestInit).method).toBeUndefined();
  });
});
