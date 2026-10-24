/** Resolve API requests when the frontend is served from GitHub Pages. */
export function resolveApiUrl(path: string): string {
  const origin = (import.meta.env.VITE_AGNES_BACKEND_URL || '').trim().replace(/\/+$/, '')
  // Reject HTTP, embedded credentials, paths and query parameters in the public API origin.
  // If misconfigured, same-origin API calls fail visibly instead of leaking requests.
  if (!/^https:\/\/[^/]+$/i.test(origin)) return path
  return /^\/api(?:\/|\?|$)/.test(path) ? origin + path : path
}
