#!/usr/bin/env node
'use strict';

(async () => {
  const { runLauncher } = await import('./launcher.js');
  let app;
  let stopping = false;
  const startupAbort = new AbortController();
  const stop = async (code = 0) => {
    if (stopping) return;
    stopping = true;
    try { await app?.shutdown(); }
    catch (error) {
      console.error(`SpeechCraft shutdown could not confirm process cleanup: ${error.message}`);
      process.exitCode = 1;
      return;
    }
    process.exitCode = code;
  };
  const handleSignal = () => {
    if (!app) { startupAbort.abort(); return; }
    void stop(0);
  };
  process.once('SIGINT', handleSignal);
  process.once('SIGTERM', handleSignal);
  try {
    app = await runLauncher({ signal: startupAbort.signal });
    if (startupAbort.signal.aborted) { await stop(0); return; }
    if (app.help || app.command || app.ownedChildren === 0) return;
    const unexpected = await app.unexpected;
    if (!stopping) {
      // `speechcraft stop` asks the controller to close through its
      // authenticated lifecycle endpoint. A clean controller exit is that
      // explicit shutdown completing; treating it as a crash would make the
      // long-running launcher race its own stop command while it tears down
      // the frontend.
      if (unexpected.child.label === 'setup controller'
          && unexpected.result.code === 0 && unexpected.result.signal === null) {
        await stop(0);
        return;
      }
      console.error(`SpeechCraft ${unexpected.child.label} exited unexpectedly (${unexpected.result.signal || unexpected.result.code}).`);
      await stop(1);
    }
  } catch (error) {
    if (!startupAbort.signal.aborted) {
      console.error(`speechcraft: ${error.message}`);
      process.exitCode = 1;
    }
  }
})();
