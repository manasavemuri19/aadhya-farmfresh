/**
 * Display-side money formatting.
 *
 * The client never computes a total — it renders what the server sent. These
 * helpers only turn paise into something readable.
 */

export function formatPaise(paise: number): string {
  const negative = paise < 0;
  const whole = Math.floor(Math.abs(paise) / 100);
  const grouped = whole.toLocaleString('en-IN');
  return `${negative ? '-' : ''}₹${grouped}`;
}

/** Full precision, for receipts where the paise actually matter. */
export function formatPaiseExact(paise: number): string {
  const whole = Math.floor(Math.abs(paise) / 100).toLocaleString('en-IN');
  const fraction = String(Math.abs(paise) % 100).padStart(2, '0');
  return `${paise < 0 ? '-' : ''}₹${whole}.${fraction}`;
}

export function rupees(paise: number): number {
  return paise / 100;
}
