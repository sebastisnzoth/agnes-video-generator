# 增量 PRD：音乐视频（上传歌曲 → AI 分段视频 + 自动歌词字幕）

- **版本线**：v7.1（新增功能，遵循 AGENTS.md「四、AI Agent 触发词 → 新增功能」流程：需求分析 → 增量 PRD → 增量 system_design → 实现）
- **状态**：🟡 需求已确认，方案设计完成，待实现
- **关联文档**：`docs/plans/v7.1/system_design.md`（增量设计）、`docs/plans/v5.0/whisperx_alignment_evaluation_DONE.md`（对齐方案归档，本次按其 §7.2「范围纪律」触发复评，见 §五）、`docs/dev/pipeline_products.md`、`docs/dev/agnes_video_upstream_behavior.md`

---

## 一、背景与目标

### 1.1 背景

用户希望上传一首歌曲，由系统自动生成与歌曲同步的音乐视频。现有六种任务类型都以「文本 / 图片 → 视频」为入口，没有任何音频输入能力：仓库内不存在背景音乐、歌词、节拍检测相关代码（已检索 `whisper|librosa|beat|bgm|lyric|lrc|amix|volume`，仅命中多场景音频的 `amix` 合并）。

### 1.2 目标

1. 用户在网页新增的「音乐视频」标签中上传一首歌（≤ 5 分钟），填写可选的视觉风格，即可得到一个 **时长与歌曲完全一致** 的 MP4，歌曲作为唯一音轨。
2. 视频画面由 **Agnes 视频 API 按时间分段逐段生成**（AI 片段），复用现有多场景流水线（`MultiScenePipeline`）的并发提交、等待、重试与断点续传。
3. 歌词通过 **自动语音识别** 获得词级时间戳，生成歌词字幕叠加到成片上（可关闭）。
4. 同时提供 `POST /api/tasks/music-video` 接口，与其他六种任务类型的交付形态一致。

### 1.3 非目标（v7.1 明确不做）

- 不做人工粘贴歌词对齐（仅自动识别；粘贴歌词可作为后续迭代）。
- 不做 Ken Burns 图片动效、音频可视化（波形/频谱）两种替代视觉模式（已被用户否决，本期只做 AI 片段）。
- 不做按节拍/乐句切镜（分段统一为固定时长，见 §三.3）。
- 不做人工暂停点（手动模式）：音乐视频类型不支持 `manual` 执行模式（前端隐藏，后端不读取 `pause_points`）。
- 不做音频生成（不生成伴奏、不做变声）；不做多首歌曲拼接。
- 不使用 Agnes 视频模型的「音频参考」能力：当前 `core/api/agnes_video.py` 未实现音频输入，且该能力仅在付费的 2.5 系列元数据中声明，未经验证。

---

## 二、用户故事与交互

| # | 用户故事 | 验收要点 |
|---|----------|----------|
| U1 | 作为用户，我可以选择一个音频文件（mp3 / wav / m4a / aac / ogg / flac / opus）上传 | 不支持的扩展名、超过 50 MB、超过 300 秒、低于 10 秒均给出可读的错误提示（前后端 i18n） |
| U2 | 我可以填写视觉风格（可空） | 风格文本进入分镜提示词；为空时使用中性默认风格 |
| U3 | 我可以选择横屏 1280×720（默认）或竖屏 768×1152 | 与现有 v2.0 视频模型支持的尺寸一致 |
| U4 | 我可以开关歌词字幕（默认开） | 关闭后不做语音识别，也不叠加字幕 |
| U5 | 提交后我在进度页看到：歌曲解析 → 歌词识别（可选）→ 分段规划 → 逐段视频 → 合成 | 进度与断点续传与其他类型一致 |
| U6 | 成片是一个 MP4，时长与歌曲一致，音轨就是我上传的歌曲 | 见 §六 验收标准 |

交互约定：

- 表单位于创建面板的新标签「🎵 音乐视频」。
- 提交成功后跳转进度页（与其他类型一致）。
- 表单显示一行提示：「每 10 秒一个 AI 视频片段，片段提交受 API 限速（每个 Key 每分钟 1 次），5 分钟歌曲通常需要 30 分钟以上。」（文案由 i18n 管理）

---

## 三、功能需求

### 3.1 音频输入与校验

- 上传字段名 `song`，单文件。
- 扩展名白名单：`.mp3 .wav .m4a .aac .ogg .flac .opus`（与 ffmpeg 解码能力一致）。
- **大小上限 50 MB**，在读取时即按分块累计校验（现有图片/视频上传无大小上限，见审计项 M-02，本功能不沿用该缺陷）。
- 落盘到 `helpers.get_upload_dir()`，文件名由服务端 UUID 生成，经 `safe_join` 锚定（与现有上传一致，防路径穿越）。
- 时长探测使用 `core/compositor/ffmpeg_tool.probe_duration`（三级兜底，不依赖 ffprobe 是否存在）。
- 时长限制：`10 s ≤ duration ≤ 300 s`。超限在创建接口直接返回 422，并删除已落盘文件。
- 任务开始时把歌曲统一转码为任务目录下的 `song.mp3`（`-vn -c:a libmp3lame -q:a 2`），后续所有步骤只使用该文件，产物名固定，便于产物清单与续传。

### 3.2 歌词识别（自动，可关闭）

- 引擎：**faster-whisper**（CTranslate2 实现的 Whisper），CPU 推理，`compute_type=int8`，词级时间戳开启。选型理由见 §五。
- 模型：默认 `small`，可通过环境变量 `AGNES_LYRICS_MODEL` 覆盖（`tiny / base / small / medium / large-v3`）。首次使用时由 huggingface_hub 下载模型（默认 HF 源；可用 `HF_ENDPOINT` 指向镜像）。
- 语言：自动检测（不由用户指定）。
- 幻觉抑制（歌曲中的伴奏段落常被识别为虚假文本）：
  - `condition_on_previous_text=False`；
  - 丢弃 `no_speech_prob > 0.6` 或 `avg_logprob < -1.0` 的片段；
  - 默认关闭 VAD（`vad_filter=False`），避免切掉轻声演唱；该取舍需要用真实歌曲校准（见 §七 风险）。
- 分行规则（纯函数，可单测）：词间隔 > 0.8 秒、或当前行字符数 ≥ 36、或词数 ≥ 10 时换行；单行时长 < 0.6 秒则并入前一行。
- 产物：`lyrics.json`（行列表：start / end / text，秒）与 `lyrics.srt`（SubRip，时间轴即歌曲时间轴）。
- **降级**：模型未安装、模型下载失败、识别异常、识别结果为空 → 记录 `[Lyrics]` 警告，`lyric_lines` 置空，流程继续（无字幕成片）。与现有 TTS 失败降级为静音 + 字幕的策略一致（AGENTS.md §6.2）。

### 3.3 分段规划

- 固定片段时长 `clip_duration = 10` 秒（与 Agnes v2.0 的 10 秒预设一致）。
- 段数 `N = ceil(song_duration / 10)`；最长 5 分钟歌曲 → 最多 30 段。
- 段时间跨度：前 N-1 段为 10 秒，最后一段为剩余时长（可为小数）。时间跨度以 **精确浮点数** 保存在 `scene_spans` 中，合成时以此为准，而不是以 Agnes 返回的片段实际时长为准（Agnes 的 10 秒预设实际约 10.04 秒，不修正会累积约 1 秒/5 分钟的漂移）。
- 请求 Agnes 的时长取 `max(3, ceil(span))`；v2.0 与 2.5 系列均接受该范围，超出部分由合成阶段裁剪。

### 3.4 分镜提示词

- 对每一段，构造「窗口文本」：该段时间跨度内与歌词行有交集的行拼接；无歌词时写「instrumental」。
- 调用 `Screenwriter.generate_music_video_prompts(style, windows)`（LLM 一次性返回 N 条画面描述，JSON 对象 `{"prompts": [...]}`）。
- 容错：返回条数不足则用模板补齐（`"{style}, scene {i}/{N}, mood: {window}"`）；返回条数过多则截断；LLM 调用失败时整体使用模板，记录警告，不中断任务。
- 风格默认值：未填写时为 `cinematic music video, consistent color grading`。

### 3.5 视频生成

- 复用 `MultiScenePipeline._generate_videos`（两阶段：批量提交 + 并发等待，重试 3 次，断点续传写 `scene_{i}/task.json`）。
- 文生视频（t2v），不使用参考图（`_build_reference_images` 为空实现）。
- 视频 API 使用用户在设置中选中的视频模型（默认 `agnes-video-v2.0`，免费）。

### 3.6 音频与字幕

- 不调用 TTS。`_generate_audio` 覆写为「转码歌曲为 `song.mp3`」。
- 字幕：若开启且识别到歌词，写 `full_subtitle.srt` 并通过 `_set_subtitle_paths` 写回状态；使用音乐专用字幕样式（居中偏下、白字黑描边、半透明底，固定样式，用户只开关）。

### 3.7 合成

1. **定长拼接（新）**：`build_timed_video_track`——对每段执行 `trim` / `tpad` 到精确时间跨度，统一缩放/填充到目标分辨率与 24 fps，`concat` 一次编码为无声时间轴视频。
2. **叠加歌曲与字幕**：复用 `VideoConcatenator.concat_videos_with_audio_overlay`，新增参数 `audio_volume`（默认 1.5 保持旧行为；音乐视频传 1.0，避免把歌曲放大 3.5 dB 造成削波）。
3. 成片 `final_video.mp4`；随后按全局配置叠加水印（`BasePipeline._apply_watermark`）。

---

## 四、接口与数据

### 4.1 新接口

`POST /api/tasks/music-video`（`multipart/form-data`）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `song` | file | 是 | — | 歌曲文件，见 §3.1 |
| `style` | string | 否 | `""` | 视觉风格，≤ 500 字符 |
| `video_width` | int | 否 | 1280 | 仅允许 1280 或 768 |
| `video_height` | int | 否 | 720 | 与宽度成对：1280×720 或 768×1152 |
| `lyrics_enabled` | bool | 否 | true | 是否识别歌词 |
| `subtitle_enabled` | bool | 否 | true | 是否叠加歌词字幕（需 `lyrics_enabled` 且识别成功） |
| `creative_name` | string | 否 | `music_{id}` | 任务名称 |
| `ui_language` | — | — | — | 沿用 `X-Agnes-UI-Lang` 请求头 |

成功响应遵循 AGENTS.md §6.4：`{"ok": true, "task_id": "...", "dir_name": "..."}`。

错误响应：

| 场景 | 状态码 |
|------|--------|
| 未配置 API Key | 400 |
| 扩展名不支持 / 尺寸非法 / 无法解码 / 时长越界 | 422 |
| 文件超过 50 MB | 413 |

### 4.2 任务状态（`MusicVideoTask`）

在 `BaseTaskState` 上新增（见 system_design §3.1）：`song_file`、`song_name`、`song_duration`、`style`、`clip_duration`、`lyrics_enabled`、`lyric_lines`、`scene_spans`、`scenes`、六个 `step_*` 字段、`combined_audio`、`combined_subtitle`、`subtitle_styles_path`。

### 4.3 产物（任务目录）

| 文件 | 说明 | 产物类型 |
|------|------|----------|
| `song.mp3` | 转码后的歌曲（唯一音轨） | audio |
| `lyrics.json` | 歌词行（秒） | json |
| `lyrics.srt` / `full_subtitle.srt` | 歌词字幕 | subtitle |
| `scene_{i}/video.mp4` | 第 i 段 AI 视频 | video（场景级） |
| `scene_{i}/task.json`、`curl.sh` | 视频任务续传信息 | 辅助文件 |
| `final_video.mp4` | 成片 | final_video |

---

## 五、技术选型与对 v5.0 WhisperX 决策的复评

v5.0 的 `whisperx_alignment_evaluation_DONE.md` 结论是「不采用」，并在 §7.2 写明：**外部人声 / 上传音频出现时，需重新评估声学对齐依赖**。本功能正是该触发条件，因此在此复评。

| 方案 | 依赖体积（PyPI 实测） | 是否需要 PyTorch | 结论 |
|------|----------------------|------------------|------|
| WhisperX 3.8.6 | 依赖 torch ~2.8、torchaudio、pyannote-audio、transformers、triton 等；ADR 估计 ~2.5 GB | 是 | **不采用**：体积大；本功能不需要说话人分离；该版本仍是原 ADR 的主要风险来源 |
| **faster-whisper 1.2.1** | 直接依赖：ctranslate2（x86_64 cp311 wheel 39.4 MB）、onnxruntime（23.6 MB）、av、tokenizers、huggingface-hub、tqdm | 否 | **采用**：同一 Whisper 权重，词级时间戳原生支持，无 torch |

附加约束：

- `av`（PyAV）的 wheel 按 Python 版本分布：最新 19.x 只提供 cp312；17.x 只提供 cp311。为覆盖 cp311–cp313，依赖声明固定为 `av>=16.0.0,<17`（16.x 在三个版本都有 x86_64 wheel）。
- 模型权重不随仓库或镜像发布，由 huggingface_hub 运行时下载，缓存在 `HF_HOME`。Docker 镜像不会因模型变大，但容器重建后需重新下载。
- 新增依赖体积：约 100 MB 量级的 wheel（不含模型），远低于 ADR 的 2.5 GB 估计。
- **本沙箱无法访问 huggingface.co**（已验证返回 000），因此真实模型识别无法在本环境端到端验证；测试以替身转写器覆盖逻辑，真实识别质量需用真实歌曲人工验收。

替代方案（未采用）：

- 仅用歌词文本按时长均分（无依赖）：实现最简单，但字幕与人声无关，用户已明确选择自动识别，故不作为默认方案。可作为识别失败时的后续降级，本期不做。

---

## 六、验收标准

1. **功能**：上传一首 3 分钟 mp3，提交后任务完成，`final_video.mp4` 时长与歌曲时长差 ≤ 0.2 秒，音轨为歌曲本身（可用 `ffmpeg -i` 核对音频流存在，且音量未被放大：与原曲的峰值电平差 ≈ 0 dB）。
2. **分段**：段数 = `ceil(duration / 10)`；每段提交给 Agnes 的时长 ≤ 12 秒；最后一段时间跨度等于余数。
3. **歌词**：识别成功且开启字幕时，`lyrics.json` 与 `full_subtitle.srt` 存在，成片含字幕；识别失败时任务仍完成，进度页给出「已跳过歌词」提示，无字幕。
4. **无 TTS**：整个流程不调用 `edge_tts` 或 `SilentTTSEngine`。
5. **断点续传**：视频生成中途失败后点击「重试任务」，已完成的 `scene_{i}/video.mp4` 不会重新提交。
6. **校验**：§4.1 表中的错误码全部可触发并有测试覆盖；超过 50 MB 时不会写满磁盘（分块累计校验）。
7. **i18n**：`zh.json` 与 `en.json` 的 key 集合完全一致，其余 20 种语言同步补齐（`i18n_check.py` 对非 en 语言缺失返回 1）；`python scripts/i18n_check.py` 退出码 0（允许既有的「疑似未翻译」提醒）；`cd frontend && npm run build` 成功，`static/` 与源码同步。
8. **工程**：`ruff` 通过；新增测试通过；现有 1467 个通过的后端测试不回退；`./scripts/run_mock_regression.sh` 通过。
9. **提交**：commit message 使用英文 Conventional Commits 格式（AGENTS.md §九 铁律）。

---

## 七、风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| 视频 API 限速：每个 Key 每分钟 1 次提交 | 5 分钟歌曲需要 ≥ 30 分钟仅用于提交 | 表单提示；多 Key 可线性缩短（`AGNES_VIDEO_RATE_LIMIT`）；文档说明 |
| 免费视频队列拥塞（503 `video_queue_full`，可持续 10 分钟以上）；单次推理约 15 分钟硬上限 | 长任务失败概率上升 | 沿用 v7.0 的排队重试轨道；失败后可「重试任务」续传，不重复已成功片段 |
| Whisper 对带伴奏的演唱识别不稳定（文本错误、时间偏移、幻觉） | 歌词字幕不准 | 幻觉过滤；可关闭字幕；文档明确「自动识别仅供参考」；后续可加入粘贴歌词校正（不在本期） |
| 首次使用需下载模型（small ≈ 数百 MB） | 首次任务耗时长 | 进度页显示「下载/加载歌词模型」；`AGNES_LYRICS_MODEL` 可改小模型 |
| 真实识别无法在沙箱验证 | 上线前质量未知 | 替身转写器覆盖全部逻辑分支；真实歌曲人工验收作为发布前检查项 |
| 版权：用户上传的歌曲不一定有使用权 | 合规 | 上传表单增加一行提示：请确保拥有该音频的使用权 |

---

## 八、开放问题与默认决定

用户未明确回答、本期按以下默认值实现（如需更改请在评审时指出）：

- 视觉风格缺省文本、片段时长 10 秒、单文件 50 MB 上限、最短 10 秒。
- 横屏 1280×720 为默认分辨率。
- 歌词字幕默认开启。
- 不支持手动执行模式（不出现暂停点）。
- 视频模型沿用设置中的选择（默认 v2.0 免费）。
- 任一片段在重试耗尽后失败 → 整个任务失败并可续传（与其他多场景类型一致）；不以静态图替代失败片段。
