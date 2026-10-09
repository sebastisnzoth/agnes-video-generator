<script setup lang="ts">
import { ref, reactive } from 'vue'
import { t } from '@/i18n'
import { useTaskSubmit } from '@/composables/useTaskSubmit'
import WatermarkToggle from '@/components/shared/WatermarkToggle.vue'
import PresetPicker from '@/components/presets/PresetPicker.vue'

// v7.1 音乐视频：上传歌曲 → 每 10 秒一段 AI 画面 → 以原曲为唯一音轨合成。
// 无 TTS 配音、无手动暂停点；歌词由后端 faster-whisper 自动识别（失败则无字幕，任务照常完成）。
const { submitting, runSubmit } = useTaskSubmit()

const form = reactive({
  name: '',
  style: '',
  resolution: '1280x720',
  lyrics: true,
  subtitle: true,
})

const songFile = ref<File | null>(null)

function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  songFile.value = input.files?.[0] ?? null
}

function parseResolution(val: string) {
  const [w, h] = val.split('x').map(Number)
  return { width: w, height: h }
}

async function submitMusicVideo() {
  let ev: Record<string, any> = {}
  await runSubmit({
    taskType: 'music_video',
    buildForm: () => {
      if (!songFile.value) throw new Error(t('mvNeedSong'))

      const fd = new FormData()
      fd.append('song', songFile.value)
      fd.append('creative_name', form.name.trim())
      fd.append('style', form.style.trim())
      const res = parseResolution(form.resolution)
      fd.append('video_width', String(res.width))
      fd.append('video_height', String(res.height))
      // 字幕即歌词字幕：关闭歌词识别时没有可烧录的内容，强制为 false
      fd.append('lyrics_enabled', String(form.lyrics))
      fd.append('subtitle_enabled', String(form.lyrics && form.subtitle))

      ev = {
        resolution: form.resolution,
        lyrics: form.lyrics ? 'on' : 'off',
        subtitle: form.lyrics && form.subtitle ? 'on' : 'off',
      }
      return fd
    },
    extraEvent: ev,
  })
}
</script>

<template>
  <div>
    <div class="glass-card rounded-2xl p-6 mb-4">
      <h2 class="text-lg font-semibold text-accent mb-4">{{ t('mvSettings') }}</h2>

      <div class="mb-4">
        <label class="block text-sm text-muted mb-1.5">{{ t('taskName') }}</label>
        <input v-model="form.name" :placeholder="t('taskNamePlaceholder')" class="w-full glass-input rounded-lg px-4 py-2.5 text-sm text-ink placeholder-muted" />
      </div>

      <div class="mb-4">
        <label class="block text-sm text-muted mb-1.5">{{ t('mvSongLabel') }} <span class="text-red-400">*</span></label>
        <div class="flex flex-wrap items-center gap-3">
          <label class="cursor-pointer px-4 py-2.5 rounded-lg text-sm font-medium bg-accent/15 text-accent hover:bg-accent/25 transition">
            {{ t('mvChooseSong') }}
            <input
              type="file"
              accept=".mp3,.wav,.m4a,.aac,.ogg,.flac,.opus,audio/*"
              class="sr-only"
              @change="onFileChange"
            />
          </label>
          <span class="text-sm text-ink-2 truncate max-w-full">{{ songFile ? songFile.name : t('mvNoSong') }}</span>
        </div>
        <p class="text-xs text-muted mt-1.5">{{ t('mvSongHint') }}</p>
      </div>

      <div class="mb-4">
        <label class="block text-sm text-muted mb-1.5">{{ t('visualStyle') }}</label>
        <div class="flex items-center gap-2">
          <input v-model="form.style" class="flex-1 glass-input rounded-lg px-4 py-2.5 text-sm text-ink placeholder-muted" />
          <PresetPicker v-model="form.style" />
        </div>
      </div>

      <p class="text-xs text-muted">{{ t('mvClipHint') }}</p>
    </div>

    <!-- Lyrics & subtitles（无 TTS 配音，故不使用 SubtitleConfig） -->
    <div class="glass-card rounded-2xl p-6 mb-4">
      <h2 class="text-lg font-semibold text-accent mb-4">{{ t('subtitleConfig') }}</h2>
      <label class="flex items-center gap-2 text-sm text-ink-2 cursor-pointer mb-1">
        <input v-model="form.lyrics" type="checkbox" class="rounded bg-paper-2 border-rule" />
        <span>{{ t('mvLyricsToggle') }}</span>
      </label>
      <p class="text-xs text-muted mb-3 ml-6">{{ t('mvLyricsHint') }}</p>
      <label
        class="flex items-center gap-2 text-sm text-ink-2 cursor-pointer"
        :class="{ 'opacity-50 cursor-not-allowed': !form.lyrics }"
      >
        <input v-model="form.subtitle" :disabled="!form.lyrics" type="checkbox" class="rounded bg-paper-2 border-rule" />
        <span>{{ t('mvBurnLyrics') }}</span>
      </label>
    </div>

    <!-- Advanced Config -->
    <div class="glass-card rounded-2xl p-6 mb-4">
      <h2 class="text-lg font-semibold text-accent mb-4">{{ t('advancedSettings') }}</h2>
      <div class="grid grid-cols-2 md:grid-cols-3 gap-4 mb-4">
        <div>
          <label class="block text-sm text-muted mb-1.5">{{ t('resolution') }}</label>
          <select v-model="form.resolution" class="w-full glass-input rounded-lg px-3 py-2.5 text-sm text-ink">
            <option value="1280x720">{{ t('resLandscape') }}</option>
            <option value="768x1152">{{ t('resPortrait') }}</option>
          </select>
        </div>
      </div>
    </div>

    <WatermarkToggle />

    <button
      class="w-full py-3.5 bg-accent text-accent-ink hover:bg-accent/90 rounded-xl text-base font-semibold transition disabled:opacity-50 disabled:cursor-not-allowed glow-btn"
      :disabled="submitting"
      @click="submitMusicVideo"
    >
      {{ submitting ? t('submitting') : t('startGenerate') }}
    </button>
  </div>
</template>
