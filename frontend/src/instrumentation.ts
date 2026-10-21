/** Keep the launcher-owned nonce visible after Next.js sets its title. */
export async function register() {
  if (process.env.NEXT_RUNTIME !== 'nodejs') return;
  const { setLauncherProcessTitle } = await import('./instrumentation-node');
  setLauncherProcessTitle();
}
