/** Keep the launcher-owned Darwin nonce visible after Next.js sets its title. */
export async function register() {
  if (process.env.NEXT_RUNTIME !== 'nodejs') return;
  const nonce = process.env.SPEECHCRAFT_OWNED_PROCESS_NONCE;
  if (nonce && /^[a-f0-9]{48}$/.test(nonce)) {
    process.title = `SPEECHCRAFT-OWNED-${nonce}`;
  }
}
