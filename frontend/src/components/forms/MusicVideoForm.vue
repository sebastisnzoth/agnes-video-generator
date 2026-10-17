<script setup lang="ts">
import { ref, reactive } from 'vue'
import { t, tf } from '@/i18n'
import { useTaskSubmit } from '@/composables/useTaskSubmit'
import { appState } from '@/store'
import WatermarkToggle from '@/components/shared/WatermarkToggle.vue'
import PresetPicker from '@/components/presets/PresetPicker.vue'

// v7.1 音乐视频：上传歌曲 → 每 10 秒一段 AI 画面 → 以原曲为唯一音轨合成。
// 无 TTS 配音、无手动暂停点；歌词由后端 faster-whisper 自动识别（失败则无字幕，任务照常完成）。
// v7.2 歌手/演员：用户照片（参考图）或 AI 按描述生成；同时由歌词+画面组装分镜故事板。
const { submitting, runSubmit } = useTaskSubmit()

const form = reactive({
  name: '',
  style: '',
  resolution: '1280x720',
  lyrics: true,
  subtitle: true,
  // v7.2 歌手模式：none | photo | ai
  singerMode: 'none',
  singerPrompt: '',
})

const songFile = ref<File | null>(null)
const singerFile = ref<File | null>(null)

function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  songFile.value = input.files?.[0] ?? null
}

function onSingerFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  singerFile.value = input.files?.[0] ?? null
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
      // 提交前体积校验：serverless（Vercel）部署对请求体有平台级硬上限，超限请求
      // 根本到不了后端（返回无法解析的 HTML 413）。这里用后端下发的实际上限提前拦截，
      // 给用户可读提示，而不是让他们撞上一个「未知错误」。
      const maxBytes = appState.maxSongBytes || 50 * 1024 * 1024
      if (songFile.value.size > maxBytes) {
        throw new Error(tf('mvSongTooLarge', { max: Math.floor(maxBytes / (1024 * 1024)) }))
      }
      // v7.2 歌手模式：photo 必须已选照片；ai 必须已填描述（none 跳过）
      if (form.singerMode === 'photo' && !singerFile.value) throw new Error(t('mvNeedPhoto'))
      if (form.singerMode === 'ai' && !form.singerPrompt.trim()) throw new Error(t('mvNeedPrompt'))

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
      // v7.2 歌手/演员
      fd.append('singer_mode', form.singerMode)
      fd.append('singer_prompt', form.singerPrompt.trim())
      if (form.singerMode === 'photo' && singerFile.value) {
        fd.append('singer_photo', singerFile.value)
      }

      ev = {
        resolution: form.resolution,
        lyrics: form.lyrics ? 'on' : 'off',
        subtitle: form.lyrics && form.subtitle ? 'on' : 'off',
        singer: form.singerMode,
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

    <!-- v7.2 歌手/演员：照片参考图或 AI 生成（影响每段视频的人物一致性） -->
    <div class="glass-card rounded-2xl p-6 mb-4">
      <h2 class="text-lg font-semibold text-accent mb-4">{{ t('mvSingerSection') }}</h2>

      <div class="flex flex-wrap items-center gap-4 mb-3">
        <label class="flex items-center gap-2 text-sm text-ink-2 cursor-pointer">
          <input v-model="form.singerMode" type="radio" value="none" class="bg-paper-2 border-rule" />
          <span>{{ t('mvSingerNone') }}</span>
        </label>
        <label class="flex items-center gap-2 text-sm text-ink-2 cursor-pointer">
          <input v-model="form.singerMode" type="radio" value="photo" class="bg-paper-2 border-rule" />
          <span>{{ t('mvSingerPhoto') }}</span>
        </label>
        <label class="flex items-center gap-2 text-sm text-ink-2 cursor-pointer">
          <input v-model="form.singerMode" type="radio" value="ai" class="bg-paper-2 border-rule" />
          <span>{{ t('mvSingerAi') }}</span>
        </label>
      </div>

      <div v-if="form.singerMode === 'photo'" class="flex flex-wrap items-center gap-3 mb-3">
        <label class="cursor-pointer px-4 py-2.5 rounded-lg text-sm font-medium bg-accent/15 text-accent hover:bg-accent/25 transition">
          {{ t('mvChoosePhoto') }}
          <input type="file" accept="image/*" class="sr-only" @change="onSingerFileChange" />
        </label>
        <span class="text-sm text-ink-2 truncate max-w-full">{{ singerFile ? singerFile.name : t('mvNoPhoto') }}</span>
      </div>

      <div v-if="form.singerMode !== 'none'" class="mb-3">
        <label class="block text-sm text-muted mb-1.5">
          {{ t('mvSingerPromptLabel') }}
          <span v-if="form.singerMode === 'ai'" class="text-red-400">*</span>
        </label>
        <textarea
          v-model="form.singerPrompt"
          rows="2"
          maxlength="500"
          :placeholder="t('mvSingerPromptPh')"
          class="w-full glass-input rounded-lg px-4 py-2.5 text-sm text-ink placeholder-muted resize-y"
        ></textarea>
      </div>

      <p class="text-xs text-muted">{{ t('mvSingerHint') }}</p>
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
