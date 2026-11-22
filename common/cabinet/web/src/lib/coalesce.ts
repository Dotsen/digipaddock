// One refresh for a burst of live events: it runs `wait` ms after the last event, but never
// later than `maxWait` ms after the first one still waiting — a steady stream (an agent writing every 200 ms)
// refreshes the screen at least once a second instead of putting it off for ever.

export interface Coalesced {
  (): void;
  /** Drop the refresh that is waiting. */
  cancel(): void;
}

export function coalesce(fn: () => void, wait = 300, maxWait = 1000): Coalesced {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let first = 0; // Date.now() of the first event still waiting; 0: none
  const fire = () => {
    timer = undefined;
    first = 0;
    fn();
  };
  const call = (() => {
    const now = Date.now();
    if (!first) first = now;
    clearTimeout(timer);
    timer = setTimeout(fire, Math.max(0, Math.min(wait, first + maxWait - now)));
  }) as Coalesced;
  call.cancel = () => {
    clearTimeout(timer);
    timer = undefined;
    first = 0;
  };
  return call;
}
