/**
 * Hold-to-record microphone capture (UC1).
 *
 * Permission is asked on the first press, never at launch: the app opens to
 * capture (D9), and a permission dialog on top of the launch screen is a
 * prompt for something the user has not yet asked to do. A refusal is not an
 * error state — the text field is still there, and `denied` is what the screen
 * uses to fall back to it.
 *
 * The recording is kept as a file. Nothing here transcribes: the file is the
 * artefact that must survive (UC7, UC42), and the words are derived from it
 * server-side. See D20 for why on-device recognition is not in this path.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  RecordingPresets,
  getRecordingPermissionsAsync,
  requestRecordingPermissionsAsync,
  setAudioModeAsync,
  useAudioRecorder,
} from 'expo-audio';
import { File } from 'expo-file-system';

/** Shorter than this and it was a tap, not a hold. */
export const MIN_RECORDING_MS = 400;

/** A capture is a thought, not a voicemail. Stops itself rather than running on. */
export const MAX_RECORDING_MS = 120_000;

const TICK_MS = 100;

export type RecorderState =
  | 'idle'
  | 'requesting'
  | 'recording'
  | 'stopping'
  | 'denied'
  | 'unavailable';

/** A finished recording, shaped for `FormData`. */
export type Recording = {
  uri: string;
  name: string;
  mimeType: string;
  durationMs: number;
  /** Checked before upload — a part whose file cannot be read fails inside
   *  `fetch` as an indistinguishable "network request failed". */
  sizeBytes: number;
};

/** Why a recording could not be handed on. Distinct from an upload failure. */
export class RecordingError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RecordingError';
  }
}

/**
 * Confirm the file the recorder named is actually on disk and has content.
 *
 * React Native surfaces an unreadable multipart file part as
 * `TypeError: Network request failed` — the same string a flat network gives —
 * so a missing file reads as "the server is unreachable" unless it is caught
 * here, before `fetch` ever sees it.
 *
 * The empty-URI case is real rather than defensive: expo-audio's Android
 * recorder returns `""` from `uri` when `prepareToRecordAsync` ran without
 * microphone permission, and it does that silently, with no throw.
 *
 * Exported so `classifyRecording` can be tested without a file system.
 *
 * Args:
 *   uri: What the recorder reported.
 *
 * Returns:
 *   The file size in bytes.
 *
 * Throws:
 *   RecordingError: If the URI is empty, or the file is missing or empty.
 */
export function inspectRecording(uri: string): number {
  if (!uri) {
    throw new RecordingError(
      'The recorder produced no file. Check microphone permission.',
    );
  }

  let exists: boolean;
  let size: number;
  try {
    const file = new File(uri);
    exists = file.exists;
    size = file.size ?? 0;
  } catch (e) {
    throw new RecordingError(
      `The recording file could not be read (${uri}): ${
        e instanceof Error ? e.message : String(e)
      }`,
    );
  }

  if (!exists) throw new RecordingError(`The recording file is missing: ${uri}`);
  if (size <= 0) throw new RecordingError(`The recording file is empty: ${uri}`);
  return size;
}

/**
 * What came of releasing the button.
 *
 * A discriminated result rather than `Recording | null`, because "you tapped
 * instead of holding" and "the file is not there" are different problems with
 * different fixes, and a null cannot tell them apart.
 */
export type StopResult =
  | { outcome: 'recording'; recording: Recording }
  | { outcome: 'too-short'; durationMs: number }
  | { outcome: 'unusable'; message: string };

export type RecorderApi = {
  state: RecorderState;
  durationMs: number;
  /** Set when recording failed for a reason worth showing. */
  error: string | null;
  /** True once the mic is unusable and the screen should commit to text. */
  micUnavailable: boolean;
  /** Begin recording. Resolves false if permission was refused or setup failed. */
  start: () => Promise<boolean>;
  /** Stop, and say what came of it. */
  stop: () => Promise<StopResult>;
  /** Stop and throw the file away. */
  cancel: () => Promise<void>;
  clearError: () => void;
};

/** `.m4a` on both platforms under HIGH_QUALITY; Android's 3gp preset is LOW. */
function mimeFor(uri: string): string {
  const extension = uri.split('?')[0].split('.').pop()?.toLowerCase();
  switch (extension) {
    case 'm4a':
    case 'mp4':
      return 'audio/m4a';
    case '3gp':
      return 'audio/3gpp';
    case 'aac':
      return 'audio/aac';
    case 'wav':
      return 'audio/wav';
    case 'webm':
      return 'audio/webm';
    default:
      return 'audio/m4a';
  }
}

/**
 * Decide what a released button produced.
 *
 * Pure, and separate from the hook, because this is the part with the rules in
 * it: which failures are the user's (a tap instead of a hold) and which are the
 * app's (no file on disk). The hook only wires it up.
 *
 * Duration is checked before the file. A sub-100ms press can leave a zero-byte
 * file behind quite legitimately, and "the recording is empty" is worse advice
 * than "hold the button" for what is simply a mis-tap.
 *
 * Args:
 *   uri: What the recorder reported; `''` when it produced nothing.
 *   elapsedMs: How long the button was held.
 *   inspect: File check, injectable for tests.
 *
 * Returns:
 *   What to tell the caller.
 */
export function classifyRecording(
  uri: string,
  elapsedMs: number,
  inspect: (uri: string) => number = inspectRecording,
): StopResult {
  if (elapsedMs < MIN_RECORDING_MS) {
    return { outcome: 'too-short', durationMs: elapsedMs };
  }

  let sizeBytes: number;
  try {
    sizeBytes = inspect(uri);
  } catch (e) {
    return {
      outcome: 'unusable',
      message: e instanceof Error ? e.message : 'The recording could not be used.',
    };
  }

  return {
    outcome: 'recording',
    recording: {
      uri,
      name: uri.split('/').pop() || 'capture.m4a',
      mimeType: mimeFor(uri),
      durationMs: elapsedMs,
      sizeBytes,
    },
  };
}

export function useRecorder(): RecorderApi {
  const recorder = useAudioRecorder(RecordingPresets.HIGH_QUALITY);
  const [state, setState] = useState<RecorderState>('idle');
  const [durationMs, setDurationMs] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const startedAt = useRef<number | null>(null);
  const ticker = useRef<ReturnType<typeof setInterval> | null>(null);
  // `stop` is called from a gesture handler and from the max-length timer, and
  // both can land at once. The ref is what makes the second one a no-op.
  const stopping = useRef(false);

  const clearTicker = useCallback(() => {
    if (ticker.current) {
      clearInterval(ticker.current);
      ticker.current = null;
    }
  }, []);

  useEffect(() => clearTicker, [clearTicker]);

  const start = useCallback(async (): Promise<boolean> => {
    if (state === 'recording' || state === 'requesting') return false;
    setError(null);

    // Asked here, on the first press, and not before.
    let permission = await getRecordingPermissionsAsync();
    if (!permission.granted && permission.canAskAgain) {
      setState('requesting');
      permission = await requestRecordingPermissionsAsync();
    }
    if (!permission.granted) {
      setState('denied');
      return false;
    }

    try {
      await setAudioModeAsync({ allowsRecording: true, playsInSilentMode: true });
      await recorder.prepareToRecordAsync();
      recorder.record();
    } catch (e) {
      setState('unavailable');
      setError(
        e instanceof Error && e.message
          ? `The microphone is unavailable: ${e.message}`
          : 'The microphone is unavailable.',
      );
      return false;
    }

    stopping.current = false;
    startedAt.current = Date.now();
    setDurationMs(0);
    setState('recording');

    clearTicker();
    ticker.current = setInterval(() => {
      const elapsed = startedAt.current ? Date.now() - startedAt.current : 0;
      setDurationMs(elapsed);
    }, TICK_MS);

    return true;
  }, [recorder, state, clearTicker]);

  /** Stop the hardware and say what is on disk. */
  const finish = useCallback(async (): Promise<StopResult> => {
    if (stopping.current) {
      return { outcome: 'too-short', durationMs: 0 };
    }
    stopping.current = true;

    clearTicker();
    const elapsed = startedAt.current ? Date.now() - startedAt.current : 0;
    startedAt.current = null;
    setState('stopping');

    try {
      await recorder.stop();
    } catch (e) {
      setState('idle');
      console.error('[shelf/recorder] stop failed:', e);
      return {
        outcome: 'unusable',
        message:
          e instanceof Error && e.message
            ? `The recording could not be finished: ${e.message}`
            : 'The recording could not be finished.',
      };
    } finally {
      // Leaving the session in recording mode makes later playback quiet on
      // Android, which reads as "the audio was not saved" (UC7).
      await setAudioModeAsync({ allowsRecording: false }).catch(() => undefined);
    }

    const uri = recorder.uri ?? '';
    setState('idle');
    setDurationMs(0);

    const result = classifyRecording(uri, elapsed);
    if (result.outcome === 'unusable') {
      // A local problem, named as one. Deliberately not phrased as a network
      // failure — this is precisely the case that used to reach the screen as
      // "that recording did not reach the server".
      console.error('[shelf/recorder] recording unusable:', result.message);
    }
    return result;
  }, [recorder, clearTicker]);

  const stop = useCallback((): Promise<StopResult> => finish(), [finish]);

  const cancel = useCallback(async (): Promise<void> => {
    await finish();
  }, [finish]);

  // Nothing else stops a recording that has been held past the ceiling, and a
  // recorder left running is a battery drain the user cannot see.
  useEffect(() => {
    if (state !== 'recording' || durationMs < MAX_RECORDING_MS) return;
    void finish();
  }, [state, durationMs, finish]);

  return {
    state,
    durationMs,
    error,
    micUnavailable: state === 'denied' || state === 'unavailable',
    start,
    stop,
    cancel,
    clearError: useCallback(() => setError(null), []),
  };
}
