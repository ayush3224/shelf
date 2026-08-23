/**
 * The Shelf API client.
 *
 * Every request carries the Supabase access token as a bearer (D11). The token
 * is read fresh per request rather than captured once, because `getSession`
 * refreshes it when it has expired and a cached copy would go stale mid-session.
 */
import { File } from 'expo-file-system';

import { API_BASE_URL } from './config';
import { supabase } from './supabase';

/** Long enough for a capture that waits on the Haiku parse, short enough to fail honestly. */
const TIMEOUT_MS = 30_000;

/** An audio capture uploads a file, then waits on transcription and the parse. */
const UPLOAD_TIMEOUT_MS = 90_000;

/**
 * Where a request died.
 *
 * The distinction that matters is `transport` versus `client`: one means the
 * request left the device and the network refused it, the other means it never
 * got that far. Collapsing them — which this client used to do — makes a
 * malformed body indistinguishable from a flat tyre, and sends you looking at
 * the server for a bug that is on the phone.
 */
export type ApiFailureKind =
  | 'http' // the server answered, with a status we did not want
  | 'timeout' // we stopped waiting
  | 'transport' // dispatched, and the network failed it
  | 'client'; // never dispatched — the request itself was the problem

export class ApiError extends Error {
  readonly status: number;
  readonly kind: ApiFailureKind;
  /** The original throw, kept so it can be logged instead of guessed at. */
  readonly cause: unknown;
  /** One line naming the real failure. Shown under the friendly message. */
  readonly diagnostic: string;

  constructor(
    status: number,
    message: string,
    kind: ApiFailureKind = 'http',
    cause?: unknown,
    diagnostic?: string,
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.kind = kind;
    this.cause = cause;
    this.diagnostic = diagnostic ?? describe(cause) ?? message;
  }

  /** The session is gone or was rejected; the caller should sign out. */
  get isAuthError(): boolean {
    return this.status === 401;
  }

  /** True when the request never left the device. */
  get isLocalFailure(): boolean {
    return this.kind === 'client';
  }
}

/** A one-line description of a thrown value, for logs and for the screen. */
function describe(error: unknown): string | undefined {
  if (error === undefined || error === null) return undefined;
  if (error instanceof Error) {
    const name = error.name || 'Error';
    const nested = (error as { cause?: unknown }).cause;
    const suffix = nested instanceof Error ? ` (caused by ${nested.name}: ${nested.message})` : '';
    return `${name}: ${error.message}${suffix}`;
  }
  return String(error);
}

/**
 * React Native reports a genuine transport failure as
 * `TypeError: Network request failed`.
 *
 * It reports an unreadable file part in a multipart body the same way, because
 * the native networking module raises the failure through the same channel. So
 * this narrows the field; it does not settle it, which is why the recorder
 * checks the file *before* handing it to `fetch` rather than trying to work
 * out afterwards which of the two happened.
 */
function looksLikeTransportFailure(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? '');
  return /network request failed|unable to resolve host|connection (reset|refused|closed)|timed? ?out|tls|ssl/i.test(
    message,
  );
}

/** Log with a stable prefix so it is greppable in `adb logcat` / Expo logs. */
function logFailure(path: string, error: unknown, kind: ApiFailureKind): void {
  // console.error rather than a swallowed string: the whole reason this bug
  // was invisible is that the original throw was discarded.
  console.error(`[shelf/api] ${kind} failure on ${path}:`, describe(error), error);
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
  } catch (e) {
    if (controller.signal.aborted) {
      logFailure(path, e, 'timeout');
      throw new ApiError(0, 'The server took too long.', 'timeout', e);
    }
    if (looksLikeTransportFailure(e)) {
      // Offline, DNS, TLS. The queue that would survive this is UC6, phase 3;
      // for now the caller keeps the capture and says so.
      logFailure(path, e, 'transport');
      throw new ApiError(0, 'No connection.', 'transport', e);
    }
    // Never dispatched: a body we could not build, a URI that would not
    // resolve, something thrown inside fetch before the socket. Reporting this
    // as "no connection" sends the reader to the wrong machine.
    logFailure(path, e, 'client');
    throw new ApiError(0, 'The app could not send that request.', 'client', e);
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    throw new ApiError(response.status, await detail(response), 'http');
  }

  try {
    return (await response.json()) as T;
  } catch (e) {
    // A 200 whose body will not parse is the server's problem, not the
    // network's, and saying "no connection" about it is a lie.
    logFailure(path, e, 'http');
    throw new ApiError(
      response.status,
      'The server sent something unreadable.',
      'http',
      e,
    );
  }
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
 * The file part is an `expo-file-system` `File`, **not** React Native's
 * `{uri, name, type}` object. That distinction is the whole reason voice
 * capture was failing: Expo installs its own WinterCG `fetch` over the global
 * (`expo/src/winter/runtime.native.ts`), and its multipart encoder accepts a
 * part only if it is a string, a `Blob`, or an object exposing `bytes()`.
 * A bare `{uri, ...}` is none of those, so it threw
 * `Unsupported FormDataPart implementation` before anything was dispatched —
 * which is why nothing ever reached the server.
 *
 * Expo's own source says so outright: "`uri` is not supported for React
 * Native's FormData". The RN shape is only correct for RN's XHR-based fetch,
 * which this app does not use.
 *
 * `File` satisfies the encoder directly: `bytes()` supplies the body, and
 * `name` and `type` become the part's `filename` and `content-type`. It also
 * still streams from disk rather than being read into JS.
 *
 * `transcript` is for an on-device recognition result. Sending one skips the
 * cloud transcriber entirely; sending none means the server transcribes.
 */
export async function captureAudio(
  recording: { uri: string; name: string; mimeType: string },
  options: { transcript?: string; confidence?: number } = {},
): Promise<AudioCaptureResponse> {
  let file: File;
  try {
    file = new File(recording.uri);
  } catch (e) {
    // A URI that will not open is ours, not the network's.
    logFailure('/capture/audio', e, 'client');
    throw new ApiError(
      0,
      'The app could not open that recording.',
      'client',
      e,
    );
  }

  const form = new FormData();
  // Typed as Blob because `File` implements the interface without extending it.
  form.append('audio', file as unknown as Blob);
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
