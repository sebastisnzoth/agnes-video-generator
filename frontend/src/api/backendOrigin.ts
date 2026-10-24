/** Resolve API requests when the frontend is served from GitHub Pages. */
export function resolveApiUrl(path: string): string {
  const configured = (import.meta.env.VITE_AGNES_BACKEND_URL || '').trim()
  if (!/^\/api(?:\/|\?|$)/.test(path) || !configured) return path
  try {
    const backend = new URL(configured)
    // Never forward browser requests to HTTP or credential-bearing/misconfigured URLs.
    if (backend.protocol !== 'https:' || backend.username || backend.password
      || backend.pathname !== '/' || backend.search || backend.hash) return path
    return backend.origin + path
  } catch {
    return path
  }
}
