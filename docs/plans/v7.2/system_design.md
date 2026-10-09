# 增量系统设计：音乐视频歌手/演员参考图 + 分镜故事板（v7.2）

- **版本线**：v7.2
- **配套 PRD**：`docs/plans/v7.2/video_musical_cantante_storyboard_PRD.md`（需求与验收以 PRD 为准）
- **状态**：🟡 设计完成，实现中
- **性质**：增量设计。仅描述新增/改动；v7.1 未提及的模块沿用既有实现。

---

## 一、架构位置

复用 v7.1 的全部管线骨架，改动集中在四个接缝：

```
浏览器 MusicVideoForm.vue（新增「歌手/演员」卡片）
   │ POST /api/tasks/music-video (multipart: song + singer_mode/singer_prompt/singer_photo)
   ▼
task_creation_routes.create_music_video_task
   │ 模式/描述/照片校验（§3） → 落盘 uploads/ → 探测时长 → 建 MusicVideoTask（新字段）
   ▼
deps.create_pipeline_for_type(MUSIC_VIDEO) ── 新增传 image_model
   ▼
MusicVideoPipeline（__init__ 新增 AgnesImageAPI）
   ├─ Phase 1 build_scenes      : [改] LLM 分镜感知 performer + 写 storyboard.json
   ├─ Phase 2 reference_images   : [改] no-op → photo 转存 / ai t2i → singer.png
   ├─ Phase 3 generate_videos    : [改] _get_scene_ref_images → [singer.png] → ti2vid
   └─ Phase 4-6 不变
```

## 二、数据模型（`models/task.py`）

`MusicVideoTask` 追加 4 字段（Pydantic 默认值 → 旧 `task_state.json` 兼容）：

```python
# v7.2：歌手/演员（每段视频的参考图）+ 分镜故事板
singer_mode: str = "none"   # "none" | "photo" | "ai"
singer_photo: str = ""      # 用户照片落盘路径（uploads/，仅 photo 模式）
singer_prompt: str = ""     # 表演者描述（ai 模式必填；photo 模式可选）
singer_image: str = ""      # 任务目录内参考图（working_dir/singer.png，Phase 2 写）
```

`storyboard.json` 为固定文件名（同 `prompts.json`/`lyrics.json` 惯例），不占 state 字段。

## 三、路由（`web/routes/task_creation_routes.py`）

### 3.1 新常量与辅助

```python
SINGER_PHOTO_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
MAX_SINGER_PHOTO_BYTES = 10 * 1024 * 1024      # 分块读，复用 _SONG_CHUNK_BYTES 模式
SINGER_PROMPT_MAX_CHARS = 500
```

新增 `async def _save_singer_photo_upload(upload, upload_dir) -> str`：结构照抄
`_save_song_upload`（扩展名白名单 → 422；分块累计超限 → 413 + 半成品自清理；
空文件 → 422），文件名 `singer_{uuid}{ext}` + `safe_join`。

### 3.2 端点签名与校验顺序

新增参数：`singer_mode: str = Form("none")`、`singer_prompt: str = Form("")`、
`singer_photo: Optional[UploadFile] = File(None)`。

```
1  API Key                        → 400                        （不变）
2  分辨率 ∈ MUSIC_VIDEO_SIZES     → 422                        （不变）
3  singer_mode ∈ {none,photo,ai}  → 422 singer_mode_invalid    （新，纯校验）
4  ai：prompt 非空且 ≤500         → 422 prompt_required/too_long（新，纯校验）
5  photo：文件存在 + 扩展名白名单  → 422 photo_required/format   （新，纯校验）
6  song 存在                      → 422 song_missing           （不变）
7  photo 模式：保存照片            → 413/422（新；失败时无已存文件需回滚）
8  保存歌曲（分块 ≤50MB）          → 413/422（失败 → _unlink 照片）
9  probe_duration > 0             → 422（失败 → _unlink 歌曲+照片）
10 时长 ∈ [10,300]s               → 422（失败 → _unlink 歌曲+照片）
11 MusicVideoTask(＋4 字段) → 工厂/TaskManager/launch（不变链路）
```

> 关键差异：v7.1 把「歌曲缺失」放在第 3 位；本增量把**全部纯校验前移**到任何
> 磁盘写入之前，照片与歌曲互为回滚对象，满足 PRD §6 的清理矩阵。

## 四、流水线（`core/pipelines/music_video.py`）

### 4.1 构造与常量

```python
from core.api.agnes_image import AgnesImageAPI
from core.config import DEFAULT_IMAGE_MODEL, DEFAULT_TEXT_MODEL

SINGER_IMAGE_FILENAME = "singer.png"
STORYBOARD_FILENAME = "storyboard.json"
_DEFAULT_SINGER_PROMPT_EN = "a singer performing on stage, upper body, facing camera, no text"

def __init__(..., chat_model=DEFAULT_TEXT_MODEL, image_model=DEFAULT_IMAGE_MODEL,
             video_model="agnes-video-v2.0", ...):
    ...
    self.image_generator = AgnesImageAPI(api_key=api_key, model=image_model)
```

Phase 2 子进度常量：`_PROGRESS_SINGER_RUNNING = 0.16`、`_PROGRESS_SINGER_DONE = 0.28`
（落在模板阶段窗口 `build_end=0.15 → reference_end=0.30` 内）。

### 4.2 Phase 2：`_build_reference_images`（覆写 no-op）

```python
async def _build_reference_images(self) -> None:
    mode = state.singer_mode or "none"
    if mode == "none": return                      # G1 默认 = v7.1 行为
    dst = working_dir/singer.png
    if exists(dst) and size>0:                     # 断点续传
        state.singer_image = dst; update_state(...); return
    if mode == "photo":
        emit(running, singer_photo_running)
        if not state.singer_photo or not exists: raise RuntimeError(t(singer_photo_missing))
        await to_thread(_stage_singer_photo, src, dst)   # 失败 → raise singer_failed
    else:  # ai
        emit(running, singer_ai_running)
        prompt = state.singer_prompt.strip() or _DEFAULT_SINGER_PROMPT_EN
        out = await self.image_generator.generate_single_image(
            prompt=prompt, size=f"{w}x{h}")        # 异常 → raise singer_failed
        await out.save(dst)
    state.singer_image = dst; update_state(singer_image=dst)
    emit(done, singer_done)
```

模块级纯函数 `_stage_singer_photo(src, dst)`（`asyncio.to_thread` 执行）：
Pillow `Image.open` → `ImageOps.exif_transpose` → 最长边 >2048 等比缩至 2048 →
`save(dst, "PNG")`。任一环节异常向上抛（PRD FR-7：歌手失败即任务失败）。

### 4.3 Phase 2.5：`_get_scene_ref_images`（覆写）

```python
def _get_scene_ref_images(self, scene, index) -> List[str]:
    img = state.singer_image if state else ""
    return [img] if img and os.path.exists(img) else []
```

基类逐段提交处 `submit_video(reference_image_paths=ref_images)`：
1 张 → 既有 `ti2vid` 分支（`agnes_video.submit_video` 0/1/≥2 三态，无需改动）。

### 4.4 Phase 1：performer 注入 + storyboard 写出

`_build_scenes` 改动点（其余顺序不变）：

```python
performer = self._resolve_performer_text(style)     # 新
prompts, source = await self._generate_prompts(style, windows, performer=performer)
...
self.save_prompts({...})                            # 契约不变
self._write_storyboard(style, spans, windows, prompts, source)   # 新
```

`_resolve_performer_text(style)`（纯函数，读 state）：

| mode | singer_prompt | 返回 |
|------|---------------|------|
| none | — | `""`（与 v7.1 逐字节一致） |
| photo | 有 | 用户描述 |
| photo | 空 | `"the performer from the reference image"` |
| ai | 有/空 | 用户描述 / `"the lead singer"`（空仅测试直建态；路由已强制必填） |

`_generate_prompts` 把 `performer` 作为**关键字**参数传给
`screenwriter.generate_music_video_prompts(style, windows, performer=performer)`
——既有测试用 `call_args[0]` 解包两元组，关键字入参不破坏断言。模板兜底
`_template_prompt(style, index, total, performer="")`：performer 非空时插到
style 之后（`f"{style}, {performer}, segment …"`）。

`_write_storyboard(...)` 写 `storyboard.json`（UTF-8、indent=2、ensure_ascii=False）：

```json
{
  "format_version": "1.0",
  "song": {"name": "<song_name>", "duration": 123.4},
  "style": "…",
  "singer": {"mode": "none|photo|ai", "description": "…", "image": "singer.png"|null},
  "prompt_source": "llm|partial|template",
  "segments": [
    {"index": 0, "start": 0.0, "end": 10.0,
     "lyrics": "第一句 / 第二句" | "instrumental", "visual": "<scene_prompt>"}
  ]
}
```

- `singer.image`：mode≠none 时为预测文件名 `singer.png`（Phase 2 尚未执行，文件
  可能尚不存在；产物清单以实际存在为准）。
- resume 补写：`state.scenes and state.scene_spans` 早退分支中，若
  `storyboard.json` 缺失 → `_write_storyboard_from_state()`（窗口由
  `state.lyric_lines` 字典 + `scene_spans` 重算；`prompt_source` 读回
  `prompts.json`，缺失时 `"unknown"`），不调 LLM。

完成后 emit `progress.music_video.storyboard_done`（`n=len(spans)`）。

### 4.5 Screenwriter（`core/screenwriter/scenes.py`）

`generate_music_video_prompts` 签名尾增 `performer: str = ""`（关键字调用）：

- `performer == ""`：system/user prompt 与 v7.1 完全一致（mock fixture 匹配不受扰动）。
- `performer != ""`：system_prompt 追加
  `The same performer appears in every segment: {performer}. Feature them where natural…`；
  user_prompt 追加 `Performer: {performer}` 行。

## 五、产物与依赖图

### 5.1 `core/artifacts.py`

`_music_video_artifact_defs()` 尾部追加两条：

```python
{"type": "storyboard", "step_key": "build_scenes", "label": "artStoryboard",
 "category": "json",  "scope": "task", "file": "storyboard.json", "fields": []},
{"type": "singer_image", "step_key": "reference_images", "label": "artSingerImage",
 "category": "image", "scope": "task", "file": "singer.png", "fields": ["singer_image"]},
```

- `_ARTIFACT_TO_CHECKPOINT_COARSE` += `{"storyboard": "scenes", "singer_image": "references"}`。
- `_SCHEMA_HINTS` += 两条（storyboard 注明只读；singer_image 注明替换后需级联重生成段视频）。
- `_checkpoint_to_step_field` 增加 `MusicVideoTask` 分支（PRD FR-13）：
  `scenes→step_build_scenes, references→step_reference_images, videos→step_video_generation,
  audio→step_audio, subtitle→step_subtitle, final→step_concatenation`。
  （修复前：music_video 检查点状态恒 `pending`；无手动模式故仅影响展示与
  级联 approved 重置——后者因 `manual_config=None` 恒 no-op，行为安全。）
- `_MUSIC_VIDEO_STEPS` 已含 `step_reference_images`，无需改动。

### 5.2 `core/dependency_graph.py`

```python
T_STORYBOARD = "storyboard"
T_SINGER_IMAGE = "singer_image"

_PRODUCT_EDGES[MUSIC_VIDEO] += {
    T_SINGER_IMAGE: {T_VIDEO, T_FINAL_VIDEO},   # 真实读取方：每次提交的 ref
    # storyboard 无边 = 只读参考（同 lyrics_json）
}
_TYPE_TO_CHECKPOINT_COARSE += {"storyboard": "scenes", "singer_image": "references"}
```

`singer_image` 走 `checkpoint_for_artifact → "references"` 后，级联计划从
`reference_images` 步骤向后清段视频文件 → resume 用新图重生成（PRD FR-9）。

## 六、工厂与测试接缝

| 位置 | 改动 |
|------|------|
| `web/deps.py` | `MUSIC_VIDEO` 分支补 `image_model=image_model` |
| `tests/mock_regression/conftest.py` | `mock_image_api` paths += `core.pipelines.music_video.AgnesImageAPI` |
| `frontend/src/steps.ts` | `STEPS.music_video` 插 `{key:'reference_images', labelKey:'mvStepReference'}`；`STEP_FIELD_MAP.music_video` += `reference_images:'step_reference_images'` |
| `frontend/src/components/forms/MusicVideoForm.vue` | 歌手卡片（PRD FR-11）：`form.singerMode/singerPrompt/singerFile`；提交校验 + FormData + 埋点 `singer` |
| i18n | 前端 14 key × 22 语言（追加各文件尾部，set 校验与顺序无关）；后端 CATALOG 13 key zh/en（§PRD 八） |

## 七、风险与回滚

| 风险 | 缓解 |
|------|------|
| ti2vid 对 v2.0/v2.5 的画面语义差异 | anchor 已走同一路径（1 ref → ti2vid），模型侧无新分支；mock 回归断言提交参数即可 |
| 照片 EXIF 方向/超大尺寸 | `exif_transpose` + 2048 缩边；submit 时 `normalize_reference_path` 仍按视频尺寸二次归一（dst 缓存，30 段只归一一次） |
| performer 注入污染既有 LLM 行为 | `performer=""`（默认/none 模式）分支与 v7.1 字节一致；关键字传参不破坏 `call_args[0]` 断言 |
| storyboard 与 prompts.json 双份漂移 | storyboard 由同一份 `prompts` 变量同事务写出；prompts.json 契约不动 |
| 旧任务（无字段）resume | Pydantic 默认值 + 早退分支补写 storyboard，两条路径均有测试 |
