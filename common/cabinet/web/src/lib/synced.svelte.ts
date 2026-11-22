// A screen's data that must converge: its load runs again on every resync of the app, a run
// asked for while one is going makes exactly one more, and a failed load (the network, a 5xx, a 429) marks
// the data stale and is retried with a backoff until it succeeds: an event is never taken as applied when
// its fetch failed. What the server refused (another 4xx) is not retried; `error` keeps it for the screen.
import { ApiError } from "./api";
import { onResync } from "./live.svelte";
import { backoff } from "./ws";

export interface Synced {
  /** The last load failed and a retry is planned. */
  readonly stale: boolean;
  /** Date.now() of the last successful load; 0: none yet. */
  readonly updated: number;
  /** The last load's error; null after a success. */
  readonly error: unknown;
  run(): Promise<void>;
  dispose(): void;
}

class Staleness {
  count = $state(0);
  oldest = $state(0); // the oldest successful load among the stale ones (0: one never loaded)
}

/** How many screens show data that may be out of date: the app's strip reads it. */
export const staleness = new Staleness();
const all = new Set<SyncedData>();

function recount() {
  const stale = [...all].filter((s) => s.stale);
  staleness.count = stale.length;
  staleness.oldest = stale.length ? Math.min(...stale.map((s) => s.updated)) : 0;
}

/** The strip's "retry": every stale load now. */
export function retryStale() {
  [...all].filter((s) => s.stale).forEach((s) => void s.run());
}

const retryable = (exc: unknown) =>
  !(exc instanceof ApiError) || exc.status === 0 || exc.status === 429 || exc.status >= 500;

class SyncedData implements Synced {
  stale = $state(false);
  updated = $state(0);
  error = $state<unknown>(null);
  private running: Promise<void> | null = null;
  private looping = false;
  private again = false;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private disposed = false;
  private readonly stop: () => void;

  constructor(
    private readonly load: () => Promise<void>,
    private readonly delay: (attempt: number) => number,
    private readonly onError?: (exc: unknown) => void,
  ) {
    this.stop = onResync(() => void this.run());
    all.add(this);
  }

  run(): Promise<void> {
    if (this.disposed) return Promise.resolve(); // a screen that has closed reads nothing more
    if (this.running) {
      this.again = true;
      return this.running;
    }
    const loop = this.loop();
    if (this.looping) this.running = loop; // a load that threw at once has ended already
    return loop;
  }

  // `running` is cleared in the same step that decides there is no more to do: a run asked for a moment
  // later starts a new loop, never one that has just ended (it would be lost)
  private async loop() {
    this.looping = true;
    try {
      do {
        this.again = false;
        clearTimeout(this.timer);
        try {
          await this.load();
          this.stale = false;
          this.error = null;
          this.updated = Date.now();
          this.attempt = 0;
        } catch (exc) {
          if (this.disposed) return; // the screen closed meanwhile: its failure is nobody's now
          this.error = exc;
          this.stale = retryable(exc);
          if (!this.stale) this.onError?.(exc);
          else if (!this.disposed) this.timer = setTimeout(() => void this.run(), this.delay(this.attempt++));
          return;
        }
      } while (this.again && !this.disposed);
    } finally {
      this.looping = false;
      this.running = null;
      recount();
    }
  }

  dispose() {
    this.disposed = true;
    clearTimeout(this.timer);
    this.stop();
    all.delete(this);
    recount();
  }
}

export interface SyncedOptions {
  retryDelay?: (attempt: number) => number;
  /** A failure that is not retried (the server refused): the screen tells the person. */
  onError?: (exc: unknown) => void;
}

export function synced(load: () => Promise<void>, options: SyncedOptions = {}): Synced {
  return new SyncedData(load, options.retryDelay ?? backoff, options.onError);
}
