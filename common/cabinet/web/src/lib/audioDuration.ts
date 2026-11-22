// A recording's length in the thread's player: Chrome's MediaRecorder writes
// webm without a duration in its header, so the player knows no length (`duration` is Infinity) and its time and bar
// are wrong. Seeking far past the end makes the browser read the file through and learn the length; then the player
// goes back to the start. A file that knows its length is left alone.

type Player = Pick<HTMLMediaElement, "duration" | "currentTime" | "addEventListener" | "removeEventListener">;

/** A Svelte action on an `<audio>`: once its metadata come without a length, find the length and go back to 0. */
export function fullDuration(audio: Player) {
  const back = () => {
    if (!Number.isFinite(audio.duration)) return; // not read through yet: the next update
    audio.removeEventListener("timeupdate", back);
    audio.removeEventListener("durationchange", back);
    audio.currentTime = 0;
  };
  const loaded = () => {
    if (Number.isFinite(audio.duration)) return;
    audio.addEventListener("timeupdate", back);
    audio.addEventListener("durationchange", back);
    audio.currentTime = 1e101;
  };
  audio.addEventListener("loadedmetadata", loaded);
  return {
    destroy() {
      audio.removeEventListener("loadedmetadata", loaded);
      audio.removeEventListener("timeupdate", back);
      audio.removeEventListener("durationchange", back);
    },
  };
}
