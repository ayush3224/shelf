/**
 * The Shelf API client.
 *
 * Every request carries the Supabase access token as a bearer (D11). The token
 * is read fresh per request rather than captured once, because `getSession`
 * refreshes it when it has expired and a cached copy would go stale mid-session.
 */
import { API_BASE_URL } from './config';
import { supabase } from './supabase';

/** Long enough for a capture that waits on the Haiku parse, short enough to fail honestly. */
const TIMEOUT_MS = 30_000;

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }

  /** The session is gone or was rejected; the caller should sign out. */
  get isAuthError(): boolean {
    return this.status === 401;
  }
}

async function accessToken(): Promise<string> {
  const { data, error } = await supabase.auth.getSession();
  if (error) throw new ApiError(401, 'Could not read your session.');
  if (!data.session) throw new ApiError(401, 'Signed out.');
  return data.session.access_token;
}

async function detail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (
      typeof body === 'object' &&
      body !== null &&
      typeof (body as { detail?: unknown }).detail === 'string'
    ) {
      return (body as { detail: string }).detail;
    }
  } catch {
    // Non-JSON error body — the status is all we have.
  }
  return `Request failed (${response.status}).`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await accessToken();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        ...init.headers,
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
    });
  } catch {
    // Offline, DNS, TLS, or the 30s timeout. The queue that would survive this
    // is UC6, phase 3; for now the caller keeps the text and says so.
    throw new ApiError(
      0,
      controller.signal.aborted ? 'The server took too long.' : 'No connection.',
    );
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) throw new ApiError(response.status, await detail(response));
  return (await response.json()) as T;
}

// ----------------------------------------------------------------- capture

export type CaptureResponse = {
  id: string;
  status: string;
  parse_status: 'ok' | 'failed' | 'needs_review';
  state: 'active' | 'shelved' | 'done' | 'dropped';
  kind: 'task' | 'note' | 'person_note';
  due_at: string | null;
  critical: boolean;
  text: string | null;
};

/** Capture typed text (UC5). The state comes back decided by the parse (UC12). */
export function capture(text: string): Promise<CaptureResponse> {
  return request<CaptureResponse>('/capture', {
    method: 'POST',
    body: JSON.stringify({ text, source: 'text' }),
  });
}

// ------------------------------------------------------------------- today

export type TodayItem = {
  id: string;
  text: string;
  raw_text: string;
  kind: 'task' | 'note' | 'person_note';
  state: 'active' | 'shelved' | 'done' | 'dropped';
  due_at: string;
  critical: boolean;
  parse_status: 'ok' | 'failed' | 'needs_review';
  overdue: boolean;
};

export type TodayResponse = {
  as_of: string;
  items: TodayItem[];
};

/** Active items due or overdue (UC32). Bounded by the server, not filtered here. */
export function today(): Promise<TodayResponse> {
  return request<TodayResponse>('/items/today');
}

// -------------------------------------------------------------- mark done

export type DoneResponse = { id: string; state: string; changed: boolean };

/** The one state the user sets by hand (UC16). Idempotent server-side. */
export function markDone(itemId: string): Promise<DoneResponse> {
  return request<DoneResponse>(`/items/${itemId}/done`, { method: 'POST' });
}
