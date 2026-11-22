import { describe, expect, it } from "vitest";
import { fresher, inReadingArea, isReply, mergeChats, readUpTo, Reading, SHOWN_MS, titleFor } from "./unread";

const state = (read: number, unread: number, last: number, first: number | null = null) => ({ read, unread, first, last });

describe("chat states (KD-9043)", () => {
  it("a late, older answer never moves the cursor back", () => {
    expect(fresher(state(5, 0, 5), state(3, 2, 5))).toEqual(state(5, 0, 5));
    expect(fresher(state(5, 0, 5), state(5, 1, 7, 6))).toEqual(state(5, 1, 7, 6));
    expect(fresher(state(5, 1, 7, 6), state(5, 0, 5))).toEqual(state(5, 1, 7, 6));
    expect(fresher(undefined, state(1, 1, 2, 2))).toEqual(state(1, 1, 2, 2));
  });

  it("merges a snapshot, counts and titles", () => {
    const merged = mergeChats({ boss: state(9, 0, 9) }, { boss: state(4, 3, 9, 5), dev: state(0, 2, 3, 1) });
    expect(merged).toEqual({ boss: state(9, 0, 9), dev: state(0, 2, 3, 1) });
    expect(titleFor("Кабинет", 0)).toBe("Кабинет");
    expect(titleFor("Кабинет", 3)).toBe("Кабинет (3)");
    expect(titleFor("Кабинет", 150)).toBe("Кабинет (99+)");
  });

  it("only the agent's texts are replies", () => {
    expect(isReply({ author: "agent:boss", kind: "text" }, "boss")).toBe(true);
    expect(isReply({ author: "agent:boss", kind: "tool_call" }, "boss")).toBe(false);
    expect(isReply({ author: "user:owner", kind: "text" }, "boss")).toBe(false);
  });

  it("the cursor stops before a reply that was not shown", () => {
    expect(readUpTo(2, [3, 5, 8], new Set([3, 4, 5]))).toBe(5);
    expect(readUpTo(2, [3, 5, 8], new Set([3, 8]))).toBe(4); // 5 was skipped: it stays new
    expect(readUpTo(2, [3, 5], new Set([3, 5, 6]))).toBe(6);
    expect(readUpTo(7, [3, 5], new Set([3]))).toBe(7);
    expect(readUpTo(2, [3], new Set())).toBe(2);
  });

  it("a message counts as shown after SHOWN_MS in an active window only", () => {
    let now = 1000;
    const reading = new Reading(() => now);
    reading.enter(4);
    now += SHOWN_MS - 1;
    expect([...reading.tick(true)]).toEqual([]);
    now += 1;
    expect([...reading.tick(true)]).toEqual([4]);
    reading.enter(5);
    now += SHOWN_MS;
    reading.tick(false); // the tab went to the background
    now += 10_000;
    expect(reading.tick(true).has(5)).toBe(false); // the clock starts again
    now += SHOWN_MS;
    expect(reading.tick(true).has(5)).toBe(true);
    reading.enter(6);
    reading.leave(6);
    now += SHOWN_MS;
    expect(reading.tick(true).has(6)).toBe(false);
  });

  it("a tall message counts when half of the reading area is it", () => {
    const entry = (seen: number, own: number, area: number) => ({
      intersectionRect: { height: seen }, boundingClientRect: { height: own }, rootBounds: { height: area } });
    expect(inReadingArea(entry(50, 100, 600))).toBe(true);
    expect(inReadingArea(entry(49, 100, 600))).toBe(false);
    expect(inReadingArea(entry(300, 2000, 600))).toBe(true);
    expect(inReadingArea(entry(0, 0, 600))).toBe(false);
  });
});
