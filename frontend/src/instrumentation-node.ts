/** Keep the launcher-owned nonce visible after Next.js sets its title. */
export function setLauncherProcessTitle() {
  const nonce = process.env.SPEECHCRAFT_OWNED_PROCESS_NONCE;
  if (nonce && /^[a-f0-9]{48}$/.test(nonce)) {
    process.title = `SPEECHCRAFT-OWNED-${nonce}`;
  }
}
