/**
 * Playing back an item's original recording (UC7).
 *
 * One player for the whole screen rather than one per row: only one recording
 * can usefully play at a time, and a list of forty items should not hold forty
 * native players open.
 *
 * The URL is fetched at play time, not with the list. Signed URLs are
 * short-lived on purpose (the bucket holds the user's voice), so one minted
 * when the list loaded would be stale by the time anyone pressed play.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useAudioPlayer, useAudioPlayerStatus } from 'expo-audio';

import { ApiError, audioUrl } from './api';

export type PlaybackApi = {
  /** Item currently playing or loading, or null. */
  activeId: string | null;
  /** True while the signed URL is being fetched. */
  loading: boolean;
  error: string | null;
  /** Start this item, or stop it if it is already the active one. */
  toggle: (itemId: string) => Promise<void>;
  stop: () => void;
};

export function usePlayback(): PlaybackApi {
  const player = useAudioPlayer();
  const status = useAudioPlayerStatus(player);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guards against a slow URL fetch resolving after the user moved on and
  // starting playback of something they are no longer looking at.
  const wanted = useRef<string | null>(null);

  useEffect(() => {
    if (status.didJustFinish) {
      wanted.current = null;
      setActiveId(null);
    }
  }, [status.didJustFinish]);

  const stop = useCallback(() => {
    wanted.current = null;
    setActiveId(null);
    try {
      player.pause();
    } catch {
      // Already gone. Nothing to stop is the state we wanted anyway.
    }
  }, [player]);

  const toggle = useCallback(
    async (itemId: string) => {
      if (activeId === itemId) {
        stop();
        return;
      }

      wanted.current = itemId;
      setActiveId(itemId);
      setLoading(true);
      setError(null);
      try {
        const { url } = await audioUrl(itemId);
        if (wanted.current !== itemId) return;
        player.replace({ uri: url });
        player.play();
      } catch (e) {
        if (wanted.current === itemId) {
          setActiveId(null);
          setError(
            e instanceof ApiError && e.status === 404
              ? 'That item has no recording.'
              : 'Could not play the recording.',
          );
        }
      } finally {
        if (wanted.current === itemId) setLoading(false);
      }
    },
    [activeId, player, stop],
  );

  return { activeId, loading, error, toggle, stop };
}
