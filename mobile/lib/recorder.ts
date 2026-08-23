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
};

export type RecorderApi = {
  state: RecorderState;
  durationMs: number;
  /** Set when recording failed for a reason worth showing. */
  error: string | null;
  /** True once the mic is unusable and the screen should commit to text. */
  micUnavailable: boolean;
  /** Begin recording. Resolves false if permission was refused or setup failed. */
  start: () => Promise<boolean>;
  /** Stop and hand back the file, or null if it was too short to be a capture. */
  stop: () => Promise<Recording | null>;
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

  /** Stop the hardware and return the file, or null if there is nothing usable. */
  const finish = useCallback(async (): Promise<Recording | null> => {
    if (stopping.current) return null;
    stopping.current = true;

    clearTicker();
    const elapsed = startedAt.current ? Date.now() - startedAt.current : 0;
    startedAt.current = null;
    setState('stopping');

    try {
      await recorder.stop();
    } catch (e) {
      setState('idle');
      setError(
        e instanceof Error && e.message
          ? `The recording could not be finished: ${e.message}`
          : 'The recording could not be finished.',
      );
      return null;
    } finally {
      // Leaving the session in recording mode makes later playback quiet on
      // Android, which reads as "the audio was not saved" (UC7).
      await setAudioModeAsync({ allowsRecording: false }).catch(() => undefined);
    }

    const uri = recorder.uri;
    setState('idle');
    setDurationMs(0);
    if (!uri) return null;

    return {
      uri,
      name: uri.split('/').pop() || 'capture.m4a',
      mimeType: mimeFor(uri),
      durationMs: elapsed,
    };
  }, [recorder, clearTicker]);

  const stop = useCallback(async (): Promise<Recording | null> => {
    const recording = await finish();
    if (!recording) return null;
    if (recording.durationMs < MIN_RECORDING_MS) return null;
    return recording;
  }, [finish]);

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
