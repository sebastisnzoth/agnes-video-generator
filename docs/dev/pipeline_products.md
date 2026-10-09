# 各流水线产物产出逻辑梳理（v5.0-dev）

> 本文档梳理 5 条视频生成流水线的**产物产出逻辑**，重点说明三类核心产物：
> **视频 Prompt（视觉描述）**、**旁白内容（配音文本）**、**字幕（SRT 时间线）**。
> 作为后续维护与排错的权威参考。
>
> 关联文档：`docs/plans/v5.0/whisperx_alignment_evaluation_DONE.md`（v2.0 字幕 cue 精确对齐方案）。

---

## 0. 总览

| 流水线 | 任务类型 | 视频来源 | 旁白来源 | 字幕 | 多场景 |
|--------|----------|----------|----------|------|--------|
| CreativeVideoPipeline | `creative` | LLM 分镜（idea→story→script） | LLM 单段旁白（清洗后） | ✅ cue 对齐 | ✅ |
| ManuscriptVideoPipeline | `manuscript` | LLM 逐段分镜（稿件→段落） | 用户稿件原文（分段） | ✅ cue 对齐 | ✅ |
| AnchorVideoPipeline | `anchor` | 数字人视频（用户读稿） | 用户读稿（script_text） | ✅ cue 对齐 | 单段 |
| PoetryVideoPipeline | `poetry` | LLM 拆诗分镜（原诗→场景） | LLM 拆诗原句（清洗后） | ✅ 逐场景 cue | ✅ |
| MusicVideoPipeline | `music_video` | LLM 逐段画面描述（按歌词窗口）；失败用模板 | ❌ 无 TTS（歌曲即音轨） | ✅ 歌词 SRT（识别成功时） | ✅ 按 10 秒分段 |
| SimpleVideoPipeline | `simple` | 文生/图生视频（用户 prompt） | ❌ 无 | ❌ 无 | ❌ |

> `MultiScenePipeline`（`core/pipelines/multi_scene.py`）是**多场景共用基类**，
> 提供 `_generate_audio` / `_generate_subtitles` / `_get_narration_text` 等钩子，
> `CreativeVideoPipeline` 即继承自它。它本身**无独立 API 路由**。

---

## 1. 通用机制（所有流水线共用）

### 1.1 字幕产出：`generate_subtitles_common`
位置：`core/pipelines/__init__.py` → `BasePipeline.generate_subtitles_common`

统一字幕生成入口，按段数分支：

- **多段（`num_segments > 1`）**：
  - 有 `sub_maker` 且 `use_cue_timeline=True` → `generate_cue_aware_srt`（**v2.0 cue 精确对齐**）
  - 否则 → `_generate_scene_aware_srt`（legacy 启发式，按音频时长等比缩放段落时长）
- **单段（`num_segments == 1`）**：
  - 有 `sub_maker` → `cues_to_srt`（词级 cue）
  - 否则 → `text_to_srt`（纯文本估算时长）
- 统一后处理：`enforce_max_lines`（≤2 行/条，中文短字幕规范）
- 可选 LLM 样式：`style_mode="llm"` 时调用 `generate_subtitle_styles` 生成 `subtitle_styles.json`

### 1.2 v2.0 Cue 精确对齐（字幕时间线的"真值"）
- **时间线真值** = EdgeTTS `SubMaker.cues`（WordBoundary 逐词时间戳），即合成音频的源头时间轴。
- 最终成片时间轴从 0 连续（`final_dur = max(video_dur, audio_dur)`），
  故 **cue 时间即字幕显示时间，`offset = 0`**。
- `generate_cue_aware_srt`（`core/audio/subtitle.py`）：
  - **策略 A（文本锚定）**：归一化拼接各段文本算字符区间，按 cue 累计归一化字符位置归属场景（免疫 TTS 场景间停顿漂移）。
  - **策略 B（时间区间兜底）**：文本不可还原时用 `scene_start_times` / `scene_durations` 累加区间归属。
  - 末尾整体钳制到 `audio_duration`（防 prominence 时长加成把末句 end 拉出）。

### 1.3 续传安全：sub_maker 恢复（v5.0-dev 修复）
**问题**：续传时音频文件已存在，`_step_audio` 被跳过直接返回 `None`，导致 `sub_maker` 丢失，
字幕退回 legacy 启发式（v2.0 cue 对齐失效）。
**修复**：`BasePipeline._recover_sub_maker()` —— 音频跳过时，若字幕开启且 `use_cue_timeline`，
仅重新 `harvest_cues`（不重生成音频字节）恢复 cues。已接入：
creative / manuscript / anchor / multi_scene / poetry（逐场景）。

### 1.4 路径 B：音频关 + 字幕开（v2.0）
配置：`audio_config.enabled=False` + `subtitle_config.enabled=True` + `harvest_cues_when_audio_off=True`
→ 调用 `EdgeTTSEngine.harvest_cues()` 仅采集 cues，丢弃音频字节（合成仍需 silent 占位轨）。
各流水线 audio 步骤均已接入该分支。

### 1.5 旁白清洗（v5.0-dev 修复，见 §5）
`clean_narration_text()`（`core/screenwriter.py`）剥离 LLM 可能回显的 Markdown 结构
（`#` 标题 / `**` 加粗 / `-` 列表）与元数据行（`故事标题` / `目标受众` / `**受众**：` 等），
合并为**单独一段纯文本**后送 TTS。

### 1.6 上游产物分辨率与方向（v7.0 实测固化）

> 事实依据：`docs/dev/agnes_video_upstream_behavior.md` §5；问题清单：`docs/plans/v7.0/upstream_error_handling_plan.md` U3/U4。

**v2.0（像素协议）——标准档保真、非标准档被吸附：**

| 请求 width×height | 产物容器（ffprobe） | 结论 |
|---|---|---|
| 768×1152（本项目竖屏档） | 768×1152 | ✅ 保真 |
| 1280×720（本项目横屏档，true 16:9） | 1280×720 | ✅ 保真 |
| 1152×768（旧 demo「16:9」，实为 3:2） | 1088×832 | ❌ 吸附到 ≈4:3 |
| 768×1360 | 704×1280 | ❌ 吸附但恰好落回 9:16 |

**2.5 系列（比例协议）——比例保真、绝对像素不保真**：`size` 固定 720P，`aspect_ratio` 为枚举；
`16:9` 恰好落在 1280×720，`3:4` 落在 834×1112。UI 标注的 `720×1280` / `720×960` 只是**比例示意**。

**两条硬约束：**

1. **禁止用 `perf_params` 做产物校验**——它是推理内部尺寸（如 v2.0 请求 768×1152 时为 832×1088），
   与容器尺寸不同；校验只能读产物文件（`ffprobe` / `moviepy`），回归 F3 现行做法正确。
2. **按宽高比校验发现不了「容器对、画面躺倒」**——2.5-flash 竖屏（9:16）上游只交换容器宽高、
   不旋转像素（产物内无 rotation 元数据），需方向校正或提示；见 §1.7。

### 1.7 2.5 系列竖屏（9:16）画面躺倒与校正开关（v7.0 U3）

- **现象**：`aspect_ratio=9:16` 时产物容器 720×1280（竖），但画面内容是横屏构图顺时针躺倒 90°；
  `16:9` / `3:4` 实测正常（缺陷范围目前收窄到 9:16 一档）。
- **默认行为**：UI 在 2.5 系列选中 `9:16` 时给出显式提示（`vmPortraitRotateHint`），**不自动修改画面**（零风险）。
- **可选校正**：环境变量 `AGNES_FIX_V25_PORTRAIT_ROTATION=1` 开启探测式校正——
  下载保存后由 `VideoOutput._maybe_fix_rotation` 判定「推理内部尺寸为横屏（`perf_params.width > height`）
  且产物容器为竖屏」这一躺倒签名，命中才 `ffmpeg transpose=2`（逆时针 90°，实测可还原正确构图），
  日志前缀 `[UpstreamRotate]`。任何一步失败保留原片，不影响主流程。
- **校正点**：`VideoOutput.save()`（下载之后、场景产物注册与缩放/拼接之前），
  保证后续拼接、字幕叠加拿到的已是正向素材。

---

## 2. CreativeVideoPipeline（`creative`）

**流程**：`idea` → `develop_story`(LLM 故事) → `write_script`(LLM 分镜+旁白) →
`generate_end_frame_prompts`(LLM 尾帧) → `_step_generate_narrations`(LLM 单段旁白) →
`_step_audio`(EdgeTTS) → `_step_subtitle` → `_step_concatenate`。

### 视频 Prompt
- `script.json`（`scenes` 字段）：每场景视觉描述，来自 `write_script`。
- `end_frame_prompts.json`：每场景尾帧描述，来自 `generate_end_frame_prompts`。
- 多场景衔接含过渡帧 prompt（`_localize_transition_prompt`）。
- `character_reference.png`：角色参考图（来自 `extract_character_description` + 图像生成）。

### 旁白内容
- **单段纯文本**，`prompts.json.narrations[0]` + `_state.narrations[0]`。
- 生成：`screenwriter.generate_narration_for_video(story, scenes, total_duration)`，
  返回后**经 `clean_narration_text` 清洗**。
- 回退：LLM 返回过短/空 → 用清洗后的 `story`（`_trim_to_sentence`）兜底。
- 续传复用已有旁白时**再清洗一次**（修复历史脏数据）。
- 字幕分段：单段旁白经 `_split_narration_into_scenes(narration, num_scenes)` 均匀切分为每场景文本。

### 字幕
- 产物：`combined_narration.srt`。
- 多场景（`num_scenes > 1`）：`sub_maker` 存在 → `generate_cue_aware_srt`（v2.0）；否则 legacy。
- 续传安全：`_step_audio` 跳过时经 `_recover_sub_maker` 重采 cues。

### 产物文件清单
`story.txt` · `script.json` · `prompts.json` · `character_reference.png`
· `end_frame_prompts.json` · `scene_{i}/task.json` · `combined_narration.mp3` / `.srt` · `final_video.mp4`

---

## 3. ManuscriptVideoPipeline（`manuscript`）

**流程**：用户 `manuscript_text` → `_split_text`(分段) → 逐段 `generate_scene_prompt_for_paragraph`(LLM 分镜)
→ `_generate_audio`(EdgeTTS 整段) → `_generate_subtitles`。

### 视频 Prompt
- 逐段场景 prompt：来自 `screenwriter.generate_scene_prompt_for_paragraph`（每段稿件 → 视觉描述）。

### 旁白内容
- **用户稿件原文**（分段），非 LLM 生成，无元数据风险。
- `full_text = "\n\n".join(p.text for p in paragraphs)`。

### 字幕
- 产物：`full_subtitle.srt`（或 `generate_subtitles_common` 默认名）。
- 多段（段落 > 1）：`sub_maker` 存在 → `generate_cue_aware_srt`；否则 legacy。
- 续传安全：经 `_recover_sub_maker`。

---

## 4. AnchorVideoPipeline（`anchor`）

**流程**：用户 `script_text`(读稿) → `generate_anchor`(数字人图像/循环 prompt)
→ `_generate_videos`(数字人视频) → `_generate_audio`(EdgeTTS 读稿) → `_generate_subtitles`。

### 视频 Prompt
- **数字人视频**，非分镜场景。prompt 来自：
  - `generate_anchor_smooth_loop_prompt`（平滑循环）
  - `generate_anchor_model_audio_prompt`（模型音频）
- 见 `prompts.json` 中 `anchor_*` 字段。

### 旁白内容
- **用户读稿 `script_text`**（用户输入字段，非 LLM），单段。
- `audio_source="model"` 模式：数字人自带音频，跳过 TTS。

### 字幕
- 单段（`segment_texts=[full_text]`）：`sub_maker` 存在 → `cues_to_srt`；否则 `text_to_srt`。
- 续传安全：经 `_recover_sub_maker`。

---

## 5. PoetryVideoPipeline（`poetry`）

**流程**：用户 `poem_text` → `generate_poetry_scenes`(LLM 拆诗)
→ 逐场景视频生成 → 逐场景 `_generate_audio`(EdgeTTS 朗诵) → 逐场景 `_generate_subtitles` → 合成拼接。

### 视频 Prompt
- 逐场景 `scene_prompt`：来自 `screenwriter.generate_poetry_scenes`（原诗 → 视觉描述）。

### 旁白内容
- 逐场景 `narration_text` = **原诗句**（`generate_poetry_scenes` 返回，经 `clean_narration_text` 清洗）。
- 用户可在表单中逐场景绑定自定义诗句（覆盖 LLM 结果）。

### 字幕
- 逐场景 `scene_{i}/subtitle.srt`，**逐场景独立 cue 对齐**（v2.0）：
  - 缓存 `_scene_sub_makers[idx]`（不持久化）；
  - 有 cues → `SubtitleGenerator.cues_to_srt`；否则回退 `text_to_srt`（按音频时长）。
- 续传安全：逐场景跳过时经 `_recover_sub_maker` 重采该场景 cues。

---

## 6. SimpleVideoPipeline（`simple`）

**流程**：用户 `prompt` → `_submit_and_wait`(Agnes 视频 API，t2v/i2v) → 水印。

### 视频 Prompt
- 用户 prompt 直接作为视频生成描述（无 LLM 二次加工）。

### 旁白 / 字幕
- **均无**（纯视觉视频，无配音无字幕）。

---

## 7. 本轮修复记录（v5.0-dev）

### 7.1 旁白把元数据念出来（创意视频）— 已修复
- **现象**：`prompts.json.narrations[0]` 为结构化文档（`# 故事标题` / `## 目标受众` / `**受众**：`），
  被 TTS 念出并烧录进字幕。
- **根因**：`generate_narration_for_video` 把含 Markdown 结构的整篇 `story` 喂给 LLM，
  仅 `strip_code_fence` 未剥离结构/元数据；续传因 `len>5` 跳过重生成，脏数据固化。
- **修复**：
  1. 新增 `clean_narration_text()`（`core/screenwriter.py`）剥离标题/加粗/列表与元数据行，合并为单段纯文本。
  2. `generate_narration_for_video` 返回前清洗；creative 回退用清洗后的 `story`；续传复用旁白时再清洗。
  3. poetry 拆诗旁白同样清洗。
  4. 单元测试 `tests/test_narration_cleaning.py`（6 项）覆盖中英文元数据、纯正文保留、空串回退。

### 7.2 续传导致字幕退回 legacy（v2.0 cue 失效）— 已修复
- **现象**：续传时音频文件已存在，`_step_audio` 跳过返回 `None` → 字幕走 legacy 而非 cue 对齐。
- **修复**：`BasePipeline._recover_sub_maker()` 在音频跳过时仅重采 cues（不重生成音频），
  接入 creative / manuscript / anchor / multi_scene / poetry。

### 7.3 验证
- `tests/test_narration_cleaning.py` + `tests/test_cue_aware_srt.py`：19 项单测全绿。
- 真实污染串（`鞋择其主` prompts.json）经 `clean_narration_text` 正确清空（触发回退）。
- 端到端：提交全新创意任务 `4dcc33b5077f` 验证（后台运行中，确认旁白 SRT 为纯文本且走 cue 路径）。


---

## 8. MusicVideoPipeline（`music_video`，v7.1）

**流程**：用户歌曲 → 转码为 `song.mp3` → 探测时长 → 歌词识别（可选，faster-whisper）→ 按 10 秒切段 →
LLM 逐段画面描述 → 逐段 AI 视频（无声）→ 歌曲作为唯一音轨合成 → 水印。

### 视频 Prompt
- 逐段 `scene_prompt`：`screenwriter.generate_music_video_prompts(style, windows)`，窗口文本为该段时间跨度内的歌词行（无歌词时标注为伴奏）。
- LLM 失败或条数不符 → `_template_prompt` 补齐；`prompts.json.prompt_source` 取值 `llm` / `partial` / `template`。

### 旁白内容
- 不生成旁白，**不调用 TTS**。`scene.narration_text` 仅保存该段歌词，用于展示与产物，不驱动配音。
- `_generate_audio` 只做歌曲转码，`combined_audio` 指向 `song.mp3`，即成片唯一音轨（音量 1.0，不放大）。

### 字幕
- 歌词识别成功 → `lyrics.json`（全部歌词行，只读参考）+ `lyrics.srt`。
- 开启字幕且存在歌词行 → 复制为 `full_subtitle.srt` 并烧录（`MUSIC_SUBTITLE_STYLE`）。
- 识别失败、伴奏或字幕关闭 → 不生成字幕文件，任务照常完成。

### 分段与合成
- 段数 `N = ceil(T / 10)`：前 N−1 段各 10 秒，末段为余数；请求时长 `max(3, ceil(跨度))`。
- `build_timed_video_track`：一次 ffmpeg 滤镜图完成逐段 trim / pad / concat（无声时间轴）。
- `concat_videos_with_audio_overlay(..., audio_volume=1.0)`：叠加歌曲与字幕，成片时长 ≈ 歌曲时长。

### 续传
- `scenes` 已存在时 `_build_scenes` 直接返回（不重新识别歌词、不重新生成提示词）。
- 已完成的 `scene_{i}/video.mp4` 不重新提交；成片已存在时直接返回。
