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
