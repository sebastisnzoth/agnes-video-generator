/**
 * Public API origin used when the Vue app is hosted on GitHub Pages.
 * Relative requests continue to work unchanged on Vercel and local dev.
 */
export function resolveApiUrl(path: string): string {
  const origin = (import.meta.env.VITE_AGNES_BACKEND_URL || '').trim().replace(/\/+$/, '')
  return origin && path.startsWith('/api/') ? origin + path : path
}
