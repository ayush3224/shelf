/**
 * What a released button produces (UC1, UC42).
 *
 * The reason this matters: React Native surfaces an unreadable multipart file
 * part as `TypeError: Network request failed` — the same string a flat network
 * gives. Unless the file is checked before `fetch` sees it, a recording that
 * never reached disk is indistinguishable from an unreachable server, and the
 * investigation goes to the wrong machine.
 */
// `classifyRecording` takes its file check as an argument, so neither native
// module is exercised here — but importing the module still loads them.
jest.mock('expo-audio', () => ({
  RecordingPresets: { HIGH_QUALITY: {} },
  getRecordingPermissionsAsync: jest.fn(),
  requestRecordingPermissionsAsync: jest.fn(),
  setAudioModeAsync: jest.fn(),
  useAudioRecorder: jest.fn(),
}));
jest.mock('expo-file-system', () => ({
  File: class {
    exists = false;
    size = 0;
  },
}));

import {
  MIN_RECORDING_MS,
  RecordingError,
  classifyRecording,
} from '../lib/recorder';

const URI = 'file:///data/user/0/com.shelf.app/cache/Audio/recording-abc.m4a';
const HELD_MS = 3_000;

/** A file check that reports `size` bytes for any URI. */
const found = (size: number) => () => size;

/** A file check that fails the way `inspectRecording` does. */
const missing = (message: string) => () => {
  throw new RecordingError(message);
};

describe('a good recording', () => {
  it('comes back with the file, its size and its type', () => {
    const result = classifyRecording(URI, HELD_MS, found(83_707));

    expect(result.outcome).toBe('recording');
    if (result.outcome !== 'recording') return;
    expect(result.recording).toMatchObject({
      uri: URI,
      name: 'recording-abc.m4a',
      mimeType: 'audio/m4a',
      durationMs: HELD_MS,
      sizeBytes: 83_707,
    });
  });

  it.each([
    ['recording.m4a', 'audio/m4a'],
    ['recording.3gp', 'audio/3gpp'],
    ['recording.wav', 'audio/wav'],
    ['recording.webm', 'audio/webm'],
  ])('maps %s to %s', (name, mime) => {
    const result = classifyRecording(`file:///cache/${name}`, HELD_MS, found(1));
    if (result.outcome !== 'recording') throw new Error('expected a recording');
    expect(result.recording.mimeType).toBe(mime);
  });
});

describe('a tap rather than a hold', () => {
  it('is reported as too-short', () => {
    const result = classifyRecording(URI, MIN_RECORDING_MS - 1, found(1_000));
    expect(result.outcome).toBe('too-short');
  });

  it('is checked before the file, so a 0-byte mis-tap still says "hold"', () => {
    // A sub-100ms press can legitimately leave a zero-byte file behind, and
    // "the recording is empty" is worse advice than "hold the button".
    const result = classifyRecording(URI, 50, missing('The recording file is empty'));
    expect(result.outcome).toBe('too-short');
  });
});

describe('a recording that never reached disk', () => {
  it.each([
    ['the file is missing', 'The recording file is missing: ' + URI],
    ['the file is empty', 'The recording file is empty: ' + URI],
    ['the URI will not resolve', 'The recording file could not be read'],
    ['permission was absent', 'The recorder produced no file. Check microphone permission.'],
  ])('is reported as unusable when %s', (_label, message) => {
    const result = classifyRecording(URI, HELD_MS, missing(message));

    expect(result.outcome).toBe('unusable');
    if (result.outcome !== 'unusable') return;
    expect(result.message).toBe(message);
  });

  it('never phrases a local failure as a network one', () => {
    const result = classifyRecording(URI, HELD_MS, missing('The recording file is missing'));
    if (result.outcome !== 'unusable') throw new Error('expected unusable');
    // The whole point of the fix.
    expect(result.message).not.toMatch(/server|connection|network|unreachable/i);
  });

  it('survives a non-Error throw from the file check', () => {
    const result = classifyRecording(URI, HELD_MS, () => {
      throw 'something odd';
    });
    expect(result.outcome).toBe('unusable');
  });
});

describe('the empty URI expo-audio returns', () => {
  it('is what a real preflight has to catch', () => {
    // expo-audio's Android recorder returns "" from `uri` when
    // prepareToRecordAsync ran without microphone permission — silently, with
    // no throw. So "" is a real value, not a defensive one.
    const realCheck = (uri: string) => {
      if (!uri) throw new RecordingError('The recorder produced no file.');
      return 1;
    };
    const result = classifyRecording('', HELD_MS, realCheck);
    expect(result.outcome).toBe('unusable');
  });
});
