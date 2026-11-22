/** A size in bytes as people read it: 512 B, 3 KB, 1.2 MB. */
export const bytes = (n: number): string =>
  n < 1024 ? `${n} B` : n < 1 << 20 ? `${Math.round(n / 1024)} KB` : `${(n / (1 << 20)).toFixed(1)} MB`;
