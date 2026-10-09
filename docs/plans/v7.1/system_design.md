# 增量系统设计：音乐视频（task type `music_video`）

- **版本线**：v7.1
- **配套 PRD**：`docs/plans/v7.1/music_video_PRD.md`（需求、验收与选型依据以 PRD 为准）
- **状态**：🟡 设计完成，待实现
- **性质**：增量设计。仅描述新增/改动部分；未提及的模块沿用既有实现。

---

## 一、架构位置

音乐视频作为第 7 种任务类型接入现有框架，复用：

- `MultiScenePipeline` 模板方法（`build_scenes → build_reference_images → generate_videos → audio → subtitle → composite`）；
- 任务状态持久化（`TaskManager`）、多态反序列化（`parse_task_state`）、并发权重（`WeightedSemaphore`）；
- Agnes 视频客户端与限速器（视频提交独立桶）；
- 水印后处理、字幕样式模型（`SubtitleStyle`）、SRT 工具（`srt` 库）；
- 产物注册表（`core/artifacts.py`）与依赖图（`core/dependency_graph.py`）。

新增的只有三类能力：**歌曲输入**（上传 + 转码 + 时长探测）、**歌词识别**（faster-whisper 封装）、**定长时间轴拼接**（逐段 trim 到精确跨度）。

```
浏览器 MusicVideoForm.vue
   │ POST /api/tasks/music-video (multipart: song + 参数)
   ▼
task_creation_routes.create_music_video_task
   │ 校验 → 落盘 uploads/ → 探测时长 → 建 MusicVideoTask
   ▼
deps.create_pipeline_for_type(MUSIC_VIDEO) → MusicVideoPipeline
   │ run() [MultiScenePipeline 模板]
   ├─ build_scenes      : 转码歌曲 → 探测时长 → 歌词识别(可选) → 分段 → LLM 分镜
   ├─ video_generation  : Agnes 逐段提交 + 并发等待（既有 _generate_videos）
   ├─ audio             : 转码为 song.mp3（覆写：不调用 TTS）
   ├─ subtitle          : lyrics.srt → full_subtitle.srt（覆写）
   └─ concatenation     : build_timed_video_track → concat_videos_with_audio_overlay(audio_volume=1.0)
```

---

## 二、模块清单

### 2.1 新增

| 路径 | 职责 |
|------|------|
| `core/audio/lyrics.py` | 歌词识别与纯函数工具：`WordTiming` / `LyricLine` 数据类；`FasterWhisperTranscriber`（懒加载、模型缓存、线程内执行）；`group_words_into_lines`；`lyric_lines_to_srt`；`LyricsUnavailableError` |
| `core/compositor/music_timeline.py` | `plan_uniform_spans(total, clip)` 纯函数；`build_timed_video_track(clips, spans, out, w, h)`：ffmpeg `filter_complex` 逐段 trim/pad + concat，一次编码 |
| `core/pipelines/music_video.py` | `MusicVideoPipeline(MultiScenePipeline)`，覆写 `_build_scenes`、`_build_reference_images`（空）、`_generate_audio`、`_generate_subtitles`、`_composite_final`、`_get_watermark_language_text` |
| `tests/test_music_video_lyrics.py` | 分行、SRT、转写器降级（替身）、模型缺失降级 |
| `tests/test_music_video_timeline.py` | 分段纯函数；timeline 实际时长与帧数（真实 ffmpeg，无 ffmpeg 时跳过） |
| `tests/test_music_video_pipeline.py` | 分段（ceil、余数、30 段上限）；歌词成功 / 失败降级 / 关闭 / 伴奏；LLM 失败与部分补齐；断点续传；无 TTS；字幕副本；合成后时长 ±0.2 s 与响度不变（真实 ffmpeg）；`run()` 端到端（替身视频 API / LLM / 转写器） |
| `tests/test_music_video_routes.py` | 创建接口：400 / 413（残留文件清理）/ 422（扩展名、空文件、时长越界、无法读取、尺寸非法、缺歌曲）；成功路径与默认值；工厂分支 |

### 2.2 修改

| 路径 | 改动 |
|------|------|
| `models/task.py` | `TaskType.MUSIC_VIDEO = "music_video"`；新增 `MusicVideoTask`；注册到 `_TASK_TYPE_MAP` 与 `AnyTaskState` |
| `core/screenwriter/scenes.py`（Screenwriter 所在包） | 新增 `generate_music_video_prompts(style, windows) -> List[str]` 及其提示词构造 |
| `core/compositor/concatenator/audio_overlay.py` | `concat_videos_with_audio_overlay` / `_ffmpeg_mux_aligned` 新增关键字参数 `audio_volume: float = 1.5`（默认值保持旧行为） |
| `core/artifacts.py` | 新增 `_MUSIC_VIDEO_STEPS`、`_music_video_artifact_defs()`，并在 `_get_steps` / `_get_artifact_defs` 分派；`_SCHEMA_HINTS` 增加 `lyrics_json`；歌曲音频的标签为 `artSong`（不复用旁白用的 `artAudio`） |
| `core/dependency_graph.py` | 增加 `MUSIC_VIDEO` 的产物边（视频片段 / 歌曲音轨 / 字幕 → 成片）与参数边（仅 video / final_video；与 POETRY 不同，歌曲与字幕不随分辨率变化） |
| `tests/test_artifacts.py`、`tests/test_dependency_graph.py` | 增加 `MUSIC_VIDEO` 用例：步骤与产物清单、产物标签、影响范围、歌词 JSON 只读、参数边 |
| `web/deps.py` | `create_pipeline_for_type` 增加 `MUSIC_VIDEO` 分支 |
| `web/app_state.py` | `TASK_TYPE_WEIGHTS[MUSIC_VIDEO] = 3` |
| `web/routes/task_creation_routes.py` | 新增 `POST /api/tasks/music-video` |
| `core/i18n_backend.py` | 新增 `progress.music_video.*` 与 `validation.song_*` / `validation.music_video_size_invalid`（zh + en） |
| `requirements.txt` | 新增 `faster-whisper>=1.2.0,<2` 与 `av>=16.0.0,<17` |
| `server.py` | 模块文档字符串的端点清单增加一行 |
| `frontend/src/types.ts` | `TaskType` 联合类型增加 `'music_video'` |
| `frontend/src/steps.ts` | `STEPS.music_video`、`STEP_FIELD_MAP.music_video` |
| `frontend/src/components/CreatePanel.vue` | 增加标签页；挂载 `MusicVideoForm`；`manualSupported` 排除 `music_video` |
| `frontend/src/components/forms/MusicVideoForm.vue` | 新增表单（文件选择、风格、画幅、歌词与字幕开关、提示文案） |
| `frontend/src/composables/useTaskSubmit.ts` | `SUBMIT_APIS.music_video` |
| `frontend/src/api/index.ts` | `submitMusicVideo(fd)` → `POST /api/tasks/music-video` |
| `frontend/src/components/{GalleryPanel,ProgressHeader,TaskListPanel}.vue` | 类型名称映射 `music_video → typeMusicVideo` |
| `frontend/src/components/ProgressPage.vue` | `INPUT_FIELDS` / `CONFIG_FIELDS` 增加 `music_video` |
| `frontend/src/i18n/langs/*.json`（22 种语言） | 新增 17 个文案键（`ttMusicVideo`、`typeMusicVideo`、`tiSong`、`artSong`、`artLyrics`、`mvStep*`、`mv*` 表单文案）；zh 与 en 为基准，其余 20 种语言同步补齐，否则 `scripts/i18n_check.py` 返回 1。步骤与产物字段复用既有 `pStepVideoGen` / `pStepSubtitle` / `pStepConcat` / `artSubtitle` / `artVideo` / `artFinalVideo` |
| `static/` | 仅通过 `cd frontend && npm run build` 生成 |
| `AGENTS.md` §6.1 | 日志前缀表增加 `[MusicVideo]`、`[Lyrics]` |
| `docs/dev/pipeline_products.md`、`docs/public/features.md` | 补充产物与功能说明 |

---

## 三、数据模型

### 3.1 `MusicVideoTask(BaseTaskState)`

```python
class MusicVideoTask(BaseTaskState):
    task_type: Literal[TaskType.MUSIC_VIDEO] = TaskType.MUSIC_VIDEO

    # 输入
    song_name: str = ""             # 用户上传的原始文件名（展示用）
    song_file: str = ""             # 上传后的源文件路径（uploads/ 下）
    song_duration: float = 0.0      # 秒，上传时探测
    style: str = ""                 # 视觉风格；为空时使用默认值
    clip_duration: int = 10         # 每段目标时长（秒）

    # 歌词
    lyrics_enabled: bool = True
    lyric_lines: List[dict] = []    # [{"start": float, "end": float, "text": str}]

    # 分段（精确浮点跨度，合成以此为准）
    scene_spans: List[List[float]] = []   # [[start, end], ...]
    scenes: List[SceneTask] = []          # 沿用既有 SceneTask（duration 为请求时长 int）

    # 步骤（与 MultiScene 模板一一对应）
    step_build_scenes: StepStatus = PENDING
    step_reference_images: StepStatus = PENDING
    step_video_generation: StepStatus = PENDING
    step_audio: StepStatus = PENDING
    step_subtitle: StepStatus = PENDING
    step_concatenation: StepStatus = PENDING

    # 产物
    combined_audio: str = ""
    combined_subtitle: str = ""
    subtitle_styles_path: str = ""
```

`audio_config` 不使用；`subtitle_config.enabled` 作为字幕开关（由路由写入）。旧的 `task_state.json` 不受影响（新类型，无历史数据）。

### 3.2 `SceneTask` 的约定

- `scene_prompt`：LLM 生成的画面描述；`end_frame_prompt` 留空（不走尾帧链）。
- `narration_text`：该段时间跨度内的歌词文本（仅用于展示与产物，不驱动 TTS）。
- `duration`：提交给视频 API 的整数秒（`max(3, ceil(span))`）。
- `video_file` / `video_id`：沿用多场景约定。

---

## 四、关键流程

### 4.1 创建（路由）

1. 检查 API Key（缺失 → 400）。
2. 校验 `video_width/height` 仅为 `(1280,720)` 或 `(768,1152)`（否则 422）。
3. 扩展名白名单校验（422）。
4. **分块读取**上传内容并累计字节，超过 `MAX_SONG_BYTES = 50 MB` 立即中止并返回 413（不把整个文件读入内存）。
5. 写入 `uploads/music_{uuid}{ext}`（`safe_join`）。
6. `probe_duration`：返回 0 或异常 → 422 `song_unreadable`；`< 10` 或 `> 300` → 422 `song_duration_range`；失败时删除文件。
7. 构造 `MusicVideoTask`，`subtitle_config.enabled = subtitle_enabled`，`lyrics_enabled = lyrics_enabled`，`ui_language = get_current_lang()`。
8. `create_pipeline_for_type` → 注册 `active_pipelines` → `tm.create` → `mark_task_queued` → `launch_background_task(run_pipeline_with_concurrency(...))`。

### 4.2 `_build_scenes`（幂等）

1. 若 `self._state.scenes` 已存在 → 直接返回（断点续传）。
2. 转码：`song.mp3`（`-vn -c:a libmp3lame -q:a 2`）；`song_duration = probe_duration(song.mp3)`；为 0 → 抛错。
3. 歌词（若 `lyrics_enabled`）：
   - 进度以 `step_build_scenes` 上报（`running`；消息键 `progress.music_video.lyrics_running` / `lyrics_done` / `lyrics_empty` / `lyrics_skipped`），不新增步骤字段；
   - `await asyncio.to_thread(transcribe_lyrics, song.mp3)`；
   - 任何异常（`LyricsUnavailableError` 或其他）→ 记录 `[Lyrics]` 警告，`lyric_lines = []`，`emit` 「已跳过歌词」；
   - 成功 → `group_words_into_lines` → 写 `lyrics.json`、`lyrics.srt`。
4. `spans = plan_uniform_spans(song_duration, clip_duration)`。
5. 每段窗口文本 → `await asyncio.to_thread(screenwriter.generate_music_video_prompts, style, windows)`；异常或条数不符由模板补齐（见 PRD §3.4）。
6. 写入 `scenes`、`scene_spans`，`update_state`，保存 `prompts.json`。

### 4.3 `_generate_audio`

- 若 `song.mp3` 已存在则跳过转码；设置 `combined_audio = song.mp3`；返回 `None`（无 `sub_maker`，因此字幕步骤不依赖 TTS 结果）。

### 4.4 `_generate_subtitles`

- `subtitle_config.enabled` 为假或 `lyric_lines` 为空 → 不生成字幕，直接返回。
- 否则复制 `lyrics.srt` → `full_subtitle.srt`，调用 `_set_subtitle_paths(srt, "")`。

### 4.5 `_composite_final`

1. 若 `final_video.mp4` 已存在 → 返回。
2. `clips = [s.video_file for s in scenes]`；任一缺失 → `RuntimeError`。
3. `timeline = build_timed_video_track(clips, scene_spans, timeline.mp4, W, H)`：
   - 每段：`fps=24` → `scale=W:H:force_original_aspect_ratio=decrease` → `pad=W:H` → `setsar=1` → `tpad=stop_mode=clone:stop_duration=<span>` → `trim=duration=<span>` → `setpts=PTS-STARTPTS`；
   - `concat=n=N:v=1:a=0` → `libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -an`。
4. `VideoConcatenator.concat_videos_with_audio_overlay([timeline], song.mp3, srt_or_None, final_video.mp4, MUSIC_SUBTITLE_STYLE if has_srt else None, subtitle_styles_path or None, 1.0)`（末位为 `audio_volume=1.0`）。
5. 删除 `timeline.mp4`；返回 `final_video.mp4`（水印由基类在模板末尾处理）。

### 4.6 `audio_overlay` 的音量参数

- `concat_videos_with_audio_overlay(..., audio_volume: float = 1.5)` 与 `_ffmpeg_mux_aligned(..., audio_volume)` 新增参数；默认 1.5 与旧行为一致。
- 增益滤镜由 `_volume_filter(audio_volume)` 生成：为 1.0 时返回空串（不加滤镜）。
- 快速路径（`_ffmpeg_mux_aligned`）：音频滤镜为 `[1:a]apad=whole_dur=…{volume}`，1.0 时只有 `apad`。
- 回退路径 Step 4（ffmpeg，早于 moviepy 合成）：音频不足时 `apad=pad_dur=…{volume}` 转码；音频足够且 `audio_volume≠1.0` 时单独 `volume` 转码；为 1.0 时直接使用原 mp3，不重编码。moviepy 合成步骤使用 Step 4 的输出，不做二次缩放。
- 音乐视频传 `audio_volume=1.0`，歌曲音轨不被放大。`with_volume_scaled` 仅用于 `composite_anchor_video`，本次未改动。
- 既有调用方不传该参数，行为完全不变。

---

## 五、分段与歌词纯函数（规范）

```python
def plan_uniform_spans(total: float, clip: int) -> list[list[float]]:
    """前 N-1 段为 clip 秒，末段为余数；N = ceil(total / clip)；total <= 0 抛 ValueError。"""

def group_words_into_lines(words, *, max_gap=0.8, max_chars=36, max_words=10, min_line=0.6):
    """按间隔/字数/词数切行；过短行并入前一行；返回 LyricLine 列表（时间单调、不重叠）。"""

def lyric_lines_to_srt(lines) -> str:
    """使用 srt.Subtitle 生成 SubRip 文本；空列表返回空字符串。"""
```

转写器：

```python
class FasterWhisperTranscriber:
    def transcribe(self, audio_path: str) -> list[WordTiming]:
        # 懒加载 faster_whisper；模型名取 AGNES_LYRICS_MODEL（默认 small）
        # WhisperModel(name, device="cpu", compute_type="int8")
        # model.transcribe(path, word_timestamps=True, vad_filter=False,
        #                  condition_on_previous_text=False, beam_size=5)
        # 过滤 no_speech_prob > 0.6 或 avg_logprob < -1.0 的 segment
        # 展开 segment.words → WordTiming(start, end, word, probability)
```

模块级 `transcribe_lyrics(path) -> list[LyricLine]` 组合转写与分行；内部异常统一包装为 `LyricsUnavailableError`（导入失败、模型加载失败）或原样抛出（推理异常，由调用方降级）。

---

## 六、LLM 提示词（生成器）

`generate_music_video_prompts(style, windows)`：

- system：要求输出 **JSON 对象** `{"prompts": ["...", ...]}`，条数必须等于 `len(windows)`，每条为单段画面描述（英文或中文均可，不含字幕、不含人物对白）。
- user：列出 `第 i 段（时长 d 秒）：<窗口文本>`；窗口为 `instrumental` 时要求以纯画面表达情绪。
- 调用 `self._chat_json`（既有方法）；解析失败 → 抛 `ValueError`，由流水线捕获并使用模板补齐。

---

## 七、错误处理与降级

| 场景 | 行为 |
|------|------|
| 歌词依赖缺失 / 模型下载失败 / 推理异常 | 警告 + 跳过字幕，任务继续 |
| 歌词为空（纯伴奏） | 同上，进度文案区分「未识别到歌词」 |
| LLM 失败或条数不符 | 模板补齐，任务继续 |
| 单段视频失败（重试耗尽） | 沿用 `MultiScenePipeline`：任务失败，可「重试任务」续传 |
| 合成失败 | 任务失败，可续传（timeline 中间文件保留，续传时重新生成） |
| 停止 / 关机 | 沿用 `PipelineShutdown` 与 `CheckpointPause` 行为（本类型不支持手动暂停，不产生 CheckpointPause） |

---

## 八、产物与依赖图

- `core/artifacts.py`：
  - `_MUSIC_VIDEO_STEPS = [(step_build_scenes, build_scenes), (step_reference_images, reference_images), (step_video_generation, video_gen), (step_audio, audio), (step_subtitle, subtitle), (step_concatenation, concatenate)]`（`step_reference_images` 为空操作，与 MultiScene 模板保持一致）；
  - `_music_video_artifact_defs()`：

    | type | 文件 | 范围 | 标签 key | 说明 |
    |------|------|------|----------|------|
    | `video` | `scene_{i}/video.mp4`（附 `task.json`、`curl.sh`） | scene | `artVideo` | 分段 AI 片段（无声） |
    | `audio` | `song.mp3`（字段 `combined_audio`） | task | `artSong` | 歌曲转码，成片唯一音轨 |
    | `lyrics_json` | `lyrics.json` | task | `artLyrics` | 歌词识别结果，只读参考 |
    | `subtitle` | `full_subtitle.srt`（字段 `combined_subtitle`） | task | `artSubtitle` | 仅开启字幕且有歌词行时存在 |
    | `final_video` | `final_video.mp4`（字段 `final_video_file`） | task | `artFinalVideo` | 成片 |

  - `_SCHEMA_HINTS` 增加 `lyrics_json`；`audio` 的 `schema_hint` 由定义内显式给出（`_schema_hint_for` 优先取定义内的值）。
- `core/dependency_graph.py`（`MUSIC_VIDEO` 条目）：
  - 产物边：`T_VIDEO:*` → `{T_FINAL_VIDEO}`；`T_AUDIO` → `{T_FINAL_VIDEO}`；`T_SUBTITLE` → `{T_FINAL_VIDEO}`。`lyrics_json` 不入图（只读，修改不影响成片）。无分镜（`T_SCRIPT`）与尾帧链。
  - 参数边：`resolution` / `video_width` / `video_height` → `{T_VIDEO, T_FINAL_VIDEO}`。与 POETRY 不同：歌曲与歌词字幕不随分辨率变化，因此不包含 `T_AUDIO` / `T_SUBTITLE`。
  - 场景级：`_is_scoped` / `_scope_count` 将 `video` 视为场景级产物（数量 = 分段数）。

---

## 九、配置与环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `AGNES_LYRICS_MODEL` | `small` | faster-whisper 模型名或本地路径 |
| `HF_ENDPOINT` | 官方 | huggingface_hub 原生支持，用于镜像 |
| `AGNES_VIDEO_RATE_LIMIT` | `1 × Key 数` | 既有；决定音乐视频的提交速度 |

常量（代码内，不暴露为环境变量）：`MAX_SONG_BYTES = 50 * 1024 * 1024`、`MIN_SONG_SECONDS = 10`、`MAX_SONG_SECONDS = 300`、`CLIP_SECONDS = 10`。

---

## 十、测试计划

| 层 | 用例 | 方式 | 文件 |
|----|------|------|------|
| 纯函数 | `plan_uniform_spans`：整除、余数、极短、非法输入 | 单元 | `test_music_video_timeline.py` |
| 纯函数 | `group_words_into_lines`：间隔 / 字数 / 词数切行、短行合并、单调性；`lyric_lines_to_srt`：格式、空输入 | 单元 | `test_music_video_lyrics.py` |
| 转写 | 替身模型返回固定词表 → 行与 SRT 正确；模型不可用 → `LyricsUnavailableError`（流水线跳过字幕）；`no_speech_prob` / `avg_logprob` 过滤 | 单元（monkeypatch） | `test_music_video_lyrics.py` |
| 时间轴 | 3 段 + 余数的真实 ffmpeg 合成：时长与帧数 | 集成（需 ffmpeg） | `test_music_video_timeline.py` |
| 流水线 | 分段数 = ceil(T/10)（5 分钟 = 30 段）；歌词成功 / 失败 / 关闭 / 伴奏；LLM 失败与部分补齐；断点续传（已有 `scene_i/video.mp4` 不重新提交）；无 TTS（`_generate_audio_with_fallback` 不被调用）；字幕副本 | 单元（替身） | `test_music_video_pipeline.py` |
| 合成 | 歌曲即唯一音轨：成片时长与歌曲相差 ≤ 0.2 s；平均响度与歌曲相差 ≤ 1 dB（不放大）；合成后删除 `timeline.mp4` | 集成（需 ffmpeg） | `test_music_video_pipeline.py` |
| 端到端 | `run()`：23 秒歌曲 → 3 段（请求 10 / 10 / ≥3 秒）→ `completed`，成片时长与音量符合上条 | 端到端（需 ffmpeg） | `test_music_video_pipeline.py` |
| 路由 | 400（缺 Key）；413（超限并清理残留文件）；422（扩展名、空文件、时长 < 10 或 > 300 秒、无法读取、尺寸非法、缺歌曲）；成功路径的默认值（风格、1280×720、歌词与字幕开启）；768×1152 与开关；工厂分支 | TestClient，后台任务替身 | `test_music_video_routes.py` |
| 产物与依赖 | 产物清单与标签（`artSong` / `artLyrics`）；场景视频 / 音轨 / 字幕的影响范围；歌词 JSON 只读；分辨率参数边 | 单元 | `test_artifacts.py`、`test_dependency_graph.py` |
| 前端 | Vitest（12 项）；`vue-tsc` + `vite build`；`static/` 与构建产物一致；`i18n_check.py` 返回 0（仅保留既有的「疑似未翻译」提醒） | 工程 | — |
| 回归 | 全量 `pytest` 无新增失败（既有失败用例与基线一致）；`ruff check`；`./scripts/run_mock_regression.sh` | 工程 | — |

环境注意（沙箱）：运行需要 ffmpeg 的测试前，把 `/tmp/audit-bin`（含 ffmpeg / ffprobe）加入 PATH；pytest 工作目录使用 `AGNES_REGRESSION_WORKING_DIR=/tmp/...`，避免在仓库内产生忽略目录。真实 faster-whisper 转写无法在沙箱验证（huggingface.co 不可达），由替身覆盖流水线分支。

---

## 十一、兼容性

- 既有六种任务类型的代码路径不变；`audio_overlay` 新参数默认值保持旧行为。
- `TaskType` 新增枚举值不影响旧 `task_state.json` 的反序列化（旧数据不会出现该类型）。
- 新增依赖只在运行歌词识别时加载；导入失败不影响应用启动。
- Docker 镜像体积增加约 100 MB（不含模型）；模型缓存在容器内 `HF_HOME`，容器重建后需重新下载。
