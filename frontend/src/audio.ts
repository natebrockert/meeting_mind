// Shared audio singleton for transcript playback.
//
// Why a singleton: the dashboard plays audio from two different places —
// the full-meeting scrubber inside the TranscriptView, AND per-segment
// "tap to hear this row" clips inside each TranscriptRow. If both played
// at once you'd get overlapping audio. We track whichever element is
// currently playing in `activeAudio` and pause it before another one
// starts. Both call sites consult and mutate this slot.
//
// Extracted from main.tsx in v0.1.5 so TranscriptRow can live in its
// own file without losing the cross-component pause coordination.

// Module-private singleton — only mutated through the exported helpers
// below so call sites can't accidentally bypass the pause-before-claim
// coordination.
let activeAudio: HTMLAudioElement | null = null;

export function getActiveAudio(): HTMLAudioElement | null {
  return activeAudio;
}

export function setActiveAudio(audio: HTMLAudioElement | null): void {
  activeAudio = audio;
}

export function pauseActiveAudio(): void {
  activeAudio?.pause();
}

/** Play an exact `[startMs, endMs]` span of a meeting's audio.
 *
 * Pauses any currently-playing meeting audio first. Returns the new
 * `HTMLAudioElement` so the caller can pause / seek it; that element
 * also becomes the new `activeAudio`.
 */
export function playExactAudioSpan(
  meetingId: number,
  startMs: number,
  endMs: number,
  onStop: () => void,
  onReady: () => void,
  onError: () => void,
): HTMLAudioElement {
  pauseActiveAudio();
  const audio = new Audio(`/api/meetings/${meetingId}/audio`);
  setActiveAudio(audio);
  const startSeconds = Math.max(0, startMs / 1000);
  const endSeconds = Math.max(startSeconds + 0.1, endMs / 1000);
  const stopAtEnd = () => {
    if (audio.currentTime >= endSeconds) {
      audio.pause();
      audio.removeEventListener("timeupdate", stopAtEnd);
      onStop();
    }
  };
  const stopOnPause = () => {
    if (audio.currentTime < endSeconds) onStop();
  };
  audio.addEventListener(
    "loadedmetadata",
    () => {
      audio.currentTime = startSeconds;
      onReady();
      void audio.play().catch(onError);
    },
    { once: true },
  );
  audio.addEventListener("error", onError, { once: true });
  audio.addEventListener("timeupdate", stopAtEnd);
  audio.addEventListener("pause", stopOnPause);
  return audio;
}
