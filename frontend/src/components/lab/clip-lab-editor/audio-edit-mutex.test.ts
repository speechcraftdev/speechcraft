import { describe, expect, test } from "bun:test";
import { AudioEditMutex } from "./audio-edit-mutex";

describe("AudioEditMutex", () => {
  test("allows only one destructive audio mutation at a time", () => {
    const mutex = new AudioEditMutex();
    expect(mutex.tryBegin()).toBe(true);
    expect(mutex.isInFlight).toBe(true);
    expect(mutex.tryBegin()).toBe(false);
    mutex.end();
    expect(mutex.isInFlight).toBe(false);
    expect(mutex.tryBegin()).toBe(true);
  });

  test("undo and redo share the same single-flight lock as delete and insert", async () => {
    const mutex = new AudioEditMutex();
    const run = async (fn: () => Promise<void>): Promise<boolean> => {
      if (!mutex.tryBegin()) return false;
      try {
        await fn();
        return true;
      } finally {
        mutex.end();
      }
    };

    let releaseDelete!: () => void;
    const deleteInFlight = new Promise<void>((resolve) => {
      releaseDelete = resolve;
    });
    const deleteStarted = run(() => deleteInFlight);
    expect(await run(async () => undefined)).toBe(false);
    expect(await run(async () => undefined)).toBe(false);
    releaseDelete();
    expect(await deleteStarted).toBe(true);
    expect(await run(async () => undefined)).toBe(true);
  });
});
