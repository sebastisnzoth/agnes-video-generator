import { afterEach, describe, expect, it, vi } from 'vitest'
import { getConfig, getArtifactFileUrl, resolveApiUrl, submitMusicVideo } from './index'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Agnes backend origin for GitHub Pages', () => {
  it('keeps same-origin API requests when the URL is unset', () => {
    vi.stubEnv('VITE_AGNES_BACKEND_URL', '')
    expect(resolveApiUrl('/api/config')).toBe('/api/config')
  })

  it('routes API calls to the configured HTTPS origin without duplicated slashes', async () => {
    vi.stubEnv('VITE_AGNES_BACKEND_URL', 'https://agnes-api.example.org///')
    const calls: string[] = []
    vi.stubGlobal('fetch', (url: string) => {
      calls.push(url)
      return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }))
    })
    await getConfig()
    await submitMusicVideo(new FormData())
    expect(calls).toEqual([
      'https://agnes-api.example.org/api/config',
      'https://agnes-api.example.org/api/tasks/music-video',
    ])
    expect(getArtifactFileUrl('abc', 'video clip')).toBe(
      'https://agnes-api.example.org/api/tasks/abc/artifacts/video%20clip/file',
    )
  })

  it('preserves paths outside /api/', () => {
    vi.stubEnv('VITE_AGNES_BACKEND_URL', 'https://agnes-api.example.org')
    expect(resolveApiUrl('https://another.example/video.mp4')).toBe(
      'https://another.example/video.mp4',
    )
  })
})
