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

/** An audio capture uploads a file, then waits on transcription and the parse. */
const UPLOAD_TIMEOUT_MS = 90_000;

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

type RequestOptions = {
  /** Milliseconds before the request is aborted. */
  timeoutMs?: number;
  /**
   * Let the runtime set `Content-Type`. Required for `FormData`: the multipart
   * boundary is generated during serialisation, so a hand-set header names a
   * boundary that is not in the body and the server parses zero parts.
   */
  multipart?: boolean;
};

async function request<T>(
  path: string,
  init: RequestInit = {},
  options: RequestOptions = {},
): Promise<T> {
  const token = await accessToken();
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(),
    options.timeoutMs ?? TIMEOUT_MS,
  );

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        ...init.headers,
        Authorization: `Bearer ${token}`,
        ...(options.multipart ? {} : { 'Content-Type': 'application/json' }),
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

/** One item a capture produced. A note holding several things yields several (UC4). */
export type CapturedItem = {
  id: string;
  state: 'active' | 'shelved' | 'done' | 'dropped';
  kind: 'task' | 'note' | 'person_note';
  text: string | null;
  due_at: string | null;
  critical: boolean;
};

export type CaptureResponse = {
  id: string;
  status: string;
  parse_status: 'ok' | 'failed' | 'needs_review';
  state: 'active' | 'shelved' | 'done' | 'dropped';
  kind: 'task' | 'note' | 'person_note';
  due_at: string | null;
  critical: boolean;
  text: string | null;
  /** True when the note was split into more than one item (UC4). */
  split?: boolean;
  /** Every item written. The flat fields above describe the first of them. */
  items?: CapturedItem[];
};

export type AudioCaptureResponse = CaptureResponse & {
  audio_path: string | null;
  transcript: string | null;
  /** Which path produced the transcript. `none` means nothing did (UC42). */
  transcript_source: 'on_device' | 'cloud' | 'none';
  transcript_confidence: number | null;
};

/** Capture typed text (UC5). The state comes back decided by the parse (UC12). */
export function capture(text: string): Promise<CaptureResponse> {
  return request<CaptureResponse>('/capture', {
    method: 'POST',
    body: JSON.stringify({ text, source: 'text' }),
  });
}

/**
 * Upload a recording (UC1, UC7, UC8).
 *
 * The file is sent by URI rather than read into memory: React Native's
 * `FormData` streams it from disk, and a base64 round-trip of a minute of
 * audio is a needless spike on a phone.
 *
 * `transcript` is for an on-device recognition result. Sending one skips the
 * cloud transcriber entirely; sending none means the server transcribes.
 */
export function captureAudio(
  recording: { uri: string; name: string; mimeType: string },
  options: { transcript?: string; confidence?: number } = {},
): Promise<AudioCaptureResponse> {
  const form = new FormData();
  form.append('audio', {
    uri: recording.uri,
    name: recording.name,
    type: recording.mimeType,
    // RN's FormData takes this shape; the DOM's typings do not describe it.
  } as unknown as Blob);
  form.append('source', 'voice');
  if (options.transcript) form.append('transcript', options.transcript);
  if (options.confidence !== undefined) {
    form.append('transcript_confidence', String(options.confidence));
  }

  return request<AudioCaptureResponse>(
    '/capture/audio',
    { method: 'POST', body: form },
    { multipart: true, timeoutMs: UPLOAD_TIMEOUT_MS },
  );
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
  /** Whether the item has a recording to play (UC7). */
  has_audio: boolean;
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

// ------------------------------------------------------------------- audio

export type AudioUrlResponse = { id: string; url: string; expires_in: number };

/**
 * A playable URL for an item's original recording (UC7).
 *
 * Signed per request and short-lived, so it is fetched at play time rather
 * than held on the row.
 */
export function audioUrl(itemId: string): Promise<AudioUrlResponse> {
  return request<AudioUrlResponse>(`/items/${itemId}/audio`);
}
