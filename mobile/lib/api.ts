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

/** The four states an item can be in. Behaviour sets it, not the user. */
export type ItemState = 'active' | 'shelved' | 'done' | 'dropped';

// ----------------------------------------------------------------- devices

export type DeviceRegistration = {
  token: string;
  platform: 'android' | 'ios' | 'web';
  device_name?: string;
};

export type DeviceResponse = { registered: boolean; devices: number };

/**
 * Tell the server where to send this device's reminders (UC23).
 *
 * Called on every launch rather than once: Expo reissues the token when the
 * app is reinstalled or its data is cleared, and the server has no way to
 * notice a token has gone stale — a push to a dead one is accepted and then
 * simply never arrives.
 */
export function registerDevice(device: DeviceRegistration): Promise<DeviceResponse> {
  return request<DeviceResponse>('/devices', {
    method: 'POST',
    body: JSON.stringify(device),
  });
}

// ------------------------------------------------------------------ snooze

export type SnoozeResponse = {
  id: string;
  state: ItemState;
  due_at: string | null;
  snooze_count: number;
  /** False when the item was no longer active — it had already decayed. */
  changed: boolean;
};

/**
 * Not now (UC17).
 *
 * Omitting `minutes` takes the server's default, which is what the
 * notification button does — the duration is one number and it belongs in one
 * place. A snooze counts toward the decay threshold exactly as an ignore does
 * (UC18): both are the user saying not now.
 */
export function snoozeItem(itemId: string, minutes?: number): Promise<SnoozeResponse> {
  return request<SnoozeResponse>(`/items/${itemId}/snooze`, {
    method: 'POST',
    body: JSON.stringify(minutes === undefined ? {} : { minutes }),
  });
}

// -------------------------------------------------------------- reactivate

export type ReactivateResponse = {
  id: string;
  state: ItemState;
  previous: ItemState;
  due_at: string | null;
  changed: boolean;
};

/**
 * Take an item back off the shelf (UC20).
 *
 * The counterweight to decay being silent: the system puts things away on its
 * own, so there is one action that undoes it. The server gives the item a due
 * time on the way back, because an active item without one is a thing nothing
 * would ever surface again.
 */
export function reactivateItem(
  itemId: string,
  dueAt?: string,
): Promise<ReactivateResponse> {
  return request<ReactivateResponse>(`/items/${itemId}/reactivate`, {
    method: 'POST',
    body: JSON.stringify(dueAt === undefined ? {} : { due_at: dueAt }),
  });
}

// ------------------------------------------------------------- item detail

export type ItemDetail = {
  id: string;
  /** What is displayed and edited. */
  text: string;
  /** The transcript it came from. Never rewritten (D14). */
  raw_text: string;
  parsed_text: string | null;
  kind: 'task' | 'note' | 'person_note';
  state: ItemState;
  due_at: string | null;
  critical: boolean;
  parse_status: 'ok' | 'failed' | 'needs_review';
  source: 'voice' | 'text' | 'widget';
  has_audio: boolean;
  transcript_source: 'on_device' | 'cloud' | 'none';
  transcript_confidence: number | null;
  created_at: string;
  updated_at: string;
};

/** One item in full (UC37). */
export function item(itemId: string): Promise<ItemDetail> {
  return request<ItemDetail>(`/items/${itemId}`);
}

/**
 * Correct a mis-parsed item (UC38).
 *
 * Omitting `due_at` leaves the time alone; passing `null` clears it. The
 * distinction is real — `undefined` and `null` mean different things here —
 * so the field is only sent when the caller actually set it.
 */
export function editItem(
  itemId: string,
  changes: { text?: string; due_at?: string | null },
): Promise<ItemDetail> {
  const body: Record<string, unknown> = {};
  if (changes.text !== undefined) body.text = changes.text;
  if ('due_at' in changes) body.due_at = changes.due_at;

  return request<ItemDetail>(`/items/${itemId}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export type StateResponse = {
  id: string;
  state: ItemState;
  previous: ItemState;
  changed: boolean;
};

/** Move an item between states by hand (UC21). */
export function setItemState(itemId: string, state: ItemState): Promise<StateResponse> {
  return request<StateResponse>(`/items/${itemId}/state`, {
    method: 'POST',
    body: JSON.stringify({ state }),
  });
}

export type DeleteResponse = {
  id: string;
  deleted: boolean;
  /** Whether a recording went with it (UC39). */
  audio_deleted: boolean;
};

/** Delete an item and its recording permanently (UC39). */
export function deleteItem(itemId: string): Promise<DeleteResponse> {
  return request<DeleteResponse>(`/items/${itemId}`, { method: 'DELETE' });
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

// ------------------------------------------------------------------- shelf

/**
 * One row of the Shelf (UC33).
 *
 * `project_id` and `project_name` come down per item rather than the server
 * returning sections, because the list is paginated and a project's items can
 * straddle a page boundary. Grouping is the client's job for exactly that
 * reason — see `groupByProject` in the Shelf screen.
 */
export type ShelfItem = {
  id: string;
  text: string;
  raw_text: string;
  kind: 'task' | 'note' | 'person_note';
  state: ItemState;
  due_at: string | null;
  critical: boolean;
  parse_status: 'ok' | 'failed' | 'needs_review';
  has_audio: boolean;
  project_id: string | null;
  project_name: string | null;
  /** When it was captured. This is what the list is ordered by (D38). */
  created_at: string;
  /** When the system last moved it. Shown on decayed rows, not sorted on. */
  state_changed_at: string;
};

export type ShelfPage = {
  items: ShelfItem[];
  /** Opaque. Hand it straight back as `cursor`; never take it apart. */
  next_cursor: string | null;
  has_more: boolean;
  /** The states the server actually applied, which may be the default. */
  states: ItemState[];
};

export type ShelfQuery = {
  /** Search text (UC34). Two characters minimum, or the server refuses. */
  q?: string;
  /** States to include. Empty takes the server's default. */
  states?: ItemState[];
  /** A project id, or `'none'` for items with no project (UC36). */
  project?: string;
  /** Earliest capture time, inclusive — an ISO string. */
  from?: string;
  /** Latest capture time, exclusive — an ISO string. */
  to?: string;
  cursor?: string;
  limit?: number;
};

/**
 * Browse, search and filter every item (UC33, UC34, UC36).
 *
 * With no arguments this is the Shelf: everything that is not `active`.
 * Passing `q` widens it to all four states server-side, because a search is a
 * question about what you said and not about where the item currently sits.
 */
export function browseItems(query: ShelfQuery = {}): Promise<ShelfPage> {
  const params = new URLSearchParams();
  if (query.q) params.append('q', query.q);
  // Repeated rather than comma-joined: the server reads `state` as a list.
  for (const state of query.states ?? []) params.append('state', state);
  if (query.project) params.append('project', query.project);
  if (query.from) params.append('from', query.from);
  if (query.to) params.append('to', query.to);
  if (query.cursor) params.append('cursor', query.cursor);
  if (query.limit !== undefined) params.append('limit', String(query.limit));

  const qs = params.toString();
  return request<ShelfPage>(`/items${qs ? `?${qs}` : ''}`);
}

export type ProjectSummary = {
  id: string;
  name: string;
  slug: string;
  items: number;
};

/**
 * The projects the filter chips are drawn from (UC36).
 *
 * Normally empty. UC11 was dropped, so nothing infers a project and one only
 * exists if it was created by hand — the chip row not rendering is that
 * decision showing through, not a missing feature.
 */
export function projects(): Promise<{ projects: ProjectSummary[] }> {
  return request<{ projects: ProjectSummary[] }>('/projects');
}

// ------------------------------------------------------------------ people

/**
 * One person (UC46, UC47).
 *
 * `aliases` is the other names the same person goes by. It is what keeps a
 * bare "Priya" landing on the row a later capture renamed to "Priya Sharma",
 * and it is searched alongside the name — the alias is usually the name you
 * actually remember.
 */
export type Person = {
  id: string;
  name: string;
  type: 'person' | 'org' | 'place';
  aliases: string[];
  mentions: number;
  last_mentioned: string | null;
};

/** One thing that was said about somebody (UC46). */
export type PersonItem = {
  id: string;
  text: string;
  raw_text: string;
  kind: 'task' | 'note' | 'person_note';
  state: ItemState;
  due_at: string | null;
  critical: boolean;
  parse_status: 'ok' | 'failed' | 'needs_review';
  has_audio: boolean;
  created_at: string;
};

export type PersonPage = {
  person: Person;
  items: PersonItem[];
  next_cursor: string | null;
  has_more: boolean;
};

/**
 * Browse and search the people who have been mentioned (UC47).
 *
 * Not paginated, unlike the Shelf: this list is bounded by how many people are
 * in a life rather than by how much gets captured.
 */
export function people(query?: string): Promise<{ people: Person[] }> {
  const qs = query ? `?q=${encodeURIComponent(query)}` : '';
  return request<{ people: Person[] }>(`/people${qs}`);
}

/**
 * Everything ever said about one person (UC46).
 *
 * Newest first, and every state — a page that hid what you had already dealt
 * with would answer a narrower question than the one it is open for.
 */
export function person(entityId: string, cursor?: string): Promise<PersonPage> {
  const qs = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
  return request<PersonPage>(`/people/${entityId}${qs}`);
}

// ------------------------------------------------- correcting a person

export type MergeResponse = {
  person: Person;
  absorbed_id: string;
  absorbed_name: string;
  /** Notes that changed hands. Lower than the absorbed count when a note named both. */
  moved: number;
};

/**
 * Fold one person into another (UC48).
 *
 * The person whose page you are on survives; `absorbId` is folded in and
 * removed. The direction is fixed rather than a parameter — "the page you are
 * on is the one that stays" is a rule you can hold in your head, and a merge
 * you have to reason about is one you will get backwards.
 *
 * Destructive: a row disappears. The notes do not.
 */
export function mergePerson(
  entityId: string,
  absorbId: string,
): Promise<MergeResponse> {
  return request<MergeResponse>(`/people/${entityId}/merge`, {
    method: 'POST',
    body: JSON.stringify({ absorb: absorbId }),
  });
}

export type SplitResponse = {
  target: Person;
  /** Null when every note moved and the source row went with them. */
  source: Person | null;
  source_removed: boolean;
  target_created: boolean;
  moved: number;
  /** Aliases that stopped being the source's because they name the target (D45). */
  aliases_moved: string[];
};

/**
 * Move some of a person's notes to somebody else (UC49).
 *
 * Exactly one of `intoId` and `intoName`. Nothing is deleted and no note is
 * lost — the mentions simply belong to a different name — so this needs no
 * confirmation, unlike a merge.
 */
export function splitPerson(
  entityId: string,
  itemIds: string[],
  into: { id: string } | { name: string },
): Promise<SplitResponse> {
  return request<SplitResponse>(`/people/${entityId}/split`, {
    method: 'POST',
    body: JSON.stringify({
      item_ids: itemIds,
      ...('id' in into ? { into_id: into.id } : { into_name: into.name }),
    }),
  });
}
