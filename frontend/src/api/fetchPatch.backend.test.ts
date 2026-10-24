import { describe, expect, it, vi } from 'vitest'
import { installFetchLangHeader } from './fetchPatch'

describe('Global fetch bridge for GitHub Pages', () => {
  it('routes direct /api fetch calls through the public HTTPS backend', async () => {
    vi.stubEnv('VITE_AGNES_BACKEND_URL', 'https://api.example.test/')
    const outbound = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }))
    const host = { fetch: outbound }
    vi.stubGlobal('window', host)
    installFetchLangHeader()
    await host.fetch('/api/tasks')
    expect(outbound).toHaveBeenCalledWith(
      'https://api.example.test/api/tasks',
      expect.objectContaining({ headers: expect.anything() }),
    )
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
  })
})
