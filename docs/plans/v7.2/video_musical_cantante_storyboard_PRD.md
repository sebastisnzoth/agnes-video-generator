# PRD：音乐视频 —— 歌手/演员（照片或 AI 生成）+ AI 分镜故事板（v7.2）

- **版本线**：v7.2（增量，承接 v7.1 音乐视频）
- **配套设计**：`docs/plans/v7.2/system_design.md`
- **状态**：✅ 设计定稿，实现中
- **触发**：新增功能（需求分析 → PRD → system_design 增量 → 实现）

---

## 一、背景与问题（需求分析）

v7.1 的音乐视频任务只产出「逐段画面」：whisper 识别歌词 → LLM 按 10 秒窗口生成画面
prompt → 逐段 t2v（纯文本，无人物一致性）→ 合成。存在两个缺口：

1. **没有歌手/演员**：视频里没有演唱者形象。用户希望（a）上传自己的照片作为每段
   视频的参考图保持同一张脸；（b）不给照片时由 AI 按文字描述生成演员形象再用作参考。
2. **没有可交付的分镜故事板**：歌词（AI 识别）与画面描述（LLM 生成）只散落在
   `prompts.json` / `lyrics.json` 中，没有一份把「时间段 + 歌词 + 画面」对齐呈现的
   story board 文档供用户与外部 Agent 阅读。

## 二、目标 / 非目标

### 目标

- **G1** 三种歌手模式：`none`（默认，等同 v7.1 行为）/ `photo`（用户照片）/
  `ai`（文字描述 → AI 生成演员图）。photo 与 ai 均产出任务目录内的参考图
  `singer.png`，并作为**每一段**视频提交时的 reference image（ti2vid），保证全片人物一致。
- **G2** `_build_scenes` 阶段产出 **`storyboard.json`**：按段对齐「时间跨度 + AI 识别
  歌词 + LLM 画面描述」，附歌曲/风格/歌手元信息，注册为一等产物（可展示、可下载）。
- **G3** 生成分镜的 LLM 调用感知歌手（有歌手时把表演者描述注入 prompt），使画面
  描述自然包含表演者。
- **G4** 全量 i18n（前端 22 语言 + 后端 CATALOG zh/en）、产物清单、依赖图、
  检查点清单同步更新；不回退既有行为与测试。

### 非目标

- 不引入手动暂停点 / 手动模式（music_video 仍为全自动，`CreatePanel` 排除不变）。
- 不做多参考图（keyframes 模式）：单张表演者参考图 → ti2vid。
- 不新增独立的「故事板 LLM 调用」：故事板内容 = 既有两次 AI 产出（whisper 歌词 +
  LLM 画面描述）的确定性组装（成本与稳定性优先，见 §7 选型依据）。
- 不改造 `prompts.json` 的既有契约（`scene_prompts` 等字段保持不变）。
- 不支持事后编辑 `storyboard.json` 驱动重生成（只读参考，见 FR-5）。

## 三、用户流程

```
音乐视频表单
  ├─ 歌曲（必填，v7.1 不变）
  ├─ 新增「歌手 / 演员」区块
  │    ├─ 模式：无 / 使用我的照片 / 用 AI 生成
  │    ├─ photo：上传图片（必填，png/jpg/jpeg/webp/gif/bmp ≤10MB）+ 可选描述
  │    └─ ai：描述（必填，≤500 字符，如「银色长发、皮夹克的年轻女歌手」）
  └─ 提交 → 任务创建（singer_mode/singer_prompt/singer_photo 落 state）
       ├─ Phase 1  build_scenes   ：歌词识别 → 分段 → LLM 分镜（感知歌手）→ storyboard.json
       ├─ Phase 2  reference_images：photo→转换落盘 singer.png ｜ ai→t2i 生成 singer.png
       ├─ Phase 3  逐段提交       ：reference_image_paths=[singer.png] → ti2vid
       └─ Phase 4-6 音频/字幕/合成（不变）
产物面板：新增「Storyboard」「歌手形象」两项；时间线新增「参考图」步骤。
```

## 四、功能需求

| ID | 需求 | 说明 |
|----|------|------|
| FR-1 | 歌手模式字段 | `singer_mode ∈ {none, photo, ai}`，默认 `none`（Pydantic 默认值保证旧 `task_state.json` 兼容）。photo 需要 `singer_photo`（uploads/ 路径）；ai 需要 `singer_prompt`（必填）。 |
| FR-2 | 路由校验 | 顺序：模式白名单 → ai 模式 prompt 必填/≤500 → photo 模式文件存在+图片扩展名白名单 → 歌曲存在 → 保存照片（≤10MB 分块，空文件 422）→ 保存歌曲（≤50MB）→ 时长探测/范围。**任一后续失败必须清理先前已落盘的照片与歌曲**（对齐 v7.1 的 413 残留清理约定）。 |
| FR-3 | 参考图生成（Phase 2） | 覆写 `_build_reference_images`：`none` → no-op（现状）；`photo` → 把上传照片 EXIF 短边校正后转存为 `working_dir/singer.png`（Pillow，>2048px 缩边）；`ai` → `AgnesImageAPI.generate_single_image(prompt=singer_prompt, size=WxH)` 存 `singer.png`。断点续传：`singer.png` 已存在且非空 → 跳过。 |
| FR-4 | 逐段参考图 | 覆写 `_get_scene_ref_images` → `[state.singer_image]`（文件存在时），否则 `[]`。既有 `submit_video` 1 张参考图 → `ti2vid` 模式自动生效，无需改 API 层。 |
| FR-5 | Storyboard 产物 | `storyboard.json` 由 `_build_scenes` 写出（含 resume 补写路径：scenes 已存在但文件缺失时从 state 重建，不调 LLM）。**只读参考**：与 `lyrics_json` 一致，不入依赖图 product 边（编辑不影响下游，schema_hint 注明）。 |
| FR-6 | LLM 感知歌手 | `generate_music_video_prompts(style, windows, performer="")` 增加关键字可选参数；`performer` 非空时注入 system/user prompt（「同一表演者贯穿全片」+ 描述）。`none` 模式调用与 v7.1 逐字节一致。模板兜底 `_template_prompt` 同样接收可选 performer。 |
| FR-7 | 失败语义 | photo 源文件缺失 / Pillow 转换失败 / t2i API 失败 → **任务失败**（RuntimeError + i18n 消息），不做静默降级——歌手是用户显式选择的核心输入，悄悄丢失比失败更糟（区别于歌词「增值项降级」）。 |
| FR-8 | 产物注册 | `storyboard.json`（step `build_scenes`，label `artStoryboard`，json，task scope）与 `singer.png`（step `reference_images`，label `artSingerImage`，image，task scope，`fields=["singer_image"]`）。检查点粗映射：storyboard→`scenes`，singer_image→`references`。 |
| FR-9 | 依赖图 | 新增 `T_STORYBOARD` / `T_SINGER_IMAGE`。**singer_image → {video, final_video}**（替换参考图 → 段视频与成片受影响，级联删除对应文件后 resume 会用新图重生成）；storyboard 无 product 边（只读）。`_TYPE_TO_CHECKPOINT_COARSE` 同步登记。 |
| FR-10 | 工厂注入 | `web/deps.py` MUSIC_VIDEO 分支传入 `image_model`；`MusicVideoPipeline.__init__` 构造 `AgnesImageAPI`。mock 回归 conftest 补 `core.pipelines.music_video.AgnesImageAPI` patch 路径。 |
| FR-11 | 前端表单 | `MusicVideoForm.vue` 新增「歌手 / 演员」卡片：三选一模式 + 条件文件上传/描述文本域 + 提交校验（photo 缺文件、ai 缺描述分别报错）；FormData 追加 `singer_mode`/`singer_prompt`/`singer_photo`；埋点追加 `singer` 字段。 |
| FR-12 | 时间线 | `steps.ts` 的 `music_video` 步骤列表在 `build_scenes` 与 `video_generation` 之间插入 `reference_images`（labelKey `mvStepReference`），`STEP_FIELD_MAP.music_video` 同步补映射。 |
| FR-13 | 检查点状态 | `_checkpoint_to_step_field` 增加 `MusicVideoTask` 分支（此前缺失导致 music_video 检查点状态恒为 pending，与 poetry 1.5c 同类问题）。 |

## 五、验收标准

1. `.venv/bin/python -m pytest tests/ -q` 相对基线**零新增失败**（基线既有 4 个环境失败：
   `test_server_app` lifespan×2（Vercel `lifespan=None` 设计冲突）、
   `test_voice_multilang`×2（edge-tts 网络不可达））。
2. slow/mock 回归（`-m slow`，含 `tests/mock_regression` 36 例）全绿；
   新增 AI 歌手端到端用例断言 `singer.png`、`storyboard.json` 存在。
3. `python scripts/i18n_check.py` 退出码 0（22 语言全量 key 对齐、en 不得等于 zh）。
4. `cd frontend && npm run build` 成功（vue-tsc 类型检查通过、static/ 重建）。
5. 变更 Python 文件全部通过 `py_compile`；`ruff check` 对变更文件无新增告警。
6. 旧行为回归：`singer_mode=none`（默认）时 —— 分镜 prompt 与 v7.1 逐字节一致、
   `_build_reference_images` no-op、`submit_video` 收到的 `reference_image_paths == []`、
   旧 `task_state.json` 反序列化不报错。
7. 校验矩阵（路由测试）：模式非法 422 / photo 缺文件 422 / 扩展名非图片 422 /
   照片超 10MB 413 / 照片空文件 422 / ai 缺描述 422 / 描述超 500 字符 422 /
   成功路径 state 三字段正确落盘、照片文件存在于 uploads/。

## 六、边界与错误处理

| 场景 | 行为 |
|------|------|
| `singer_mode` 非法值 | 422 `validation.singer_mode_invalid`（带 current 值） |
| photo 模式未上传 | 422 `validation.singer_photo_required`（保存歌曲**之前**拦截） |
| 照片扩展名非白名单 | 422 `validation.singer_photo_format`（列出允许扩展名） |
| 照片 > 10MB | 413 `validation.singer_photo_too_large`（分块读，半成品自清理 + 已存歌曲回滚） |
| 照片空文件 | 422 `validation.singer_photo_unreadable` |
| ai 描述缺失 / >500 字符 | 422 `validation.singer_prompt_required` / `validation.singer_prompt_too_long` |
| 时长探测失败/越界 | 422（沿用 v7.1），**额外清理已存照片** |
| 运行期照片文件丢失 | 任务失败 `progress.music_video.singer_photo_missing` |
| Pillow 转换失败 | 任务失败 `progress.music_video.singer_failed`（带 reason） |
| t2i API 失败 | 任务失败 `progress.music_video.singer_failed`（带 reason）；resume 重跑 Phase 2 |
| LLM 分镜失败 | 沿用 v7.1 模板降级（`prompt_source=template`），storyboard 照常写出 |
| 歌词识别失败 | 沿用 v7.1 降级；storyboard 歌词段为空 / `instrumental` |
| 旧任务 resume（无新字段） | Pydantic 默认值 `none`/`""` → 全部走 v7.1 路径；scenes 存在但 storyboard 缺失 → 从 state 补写 |

## 七、选型依据

1. **参考图放 Phase 2 而非 Phase 1**：模板顺序 `build_scenes → reference_images →
   videos` 已就位（`_MUSIC_VIDEO_STEPS` 早含 `step_reference_images`），t2i 与
   whisper/LLM 互不阻塞，且断点续传粒度天然正确（图失败只重跑 Phase 2）。
2. **单图 ti2vi 而非 keyframes**：`submit_video` 对 1 张参考图走 `payload["image"]
   + mode=ti2vid`（v2.0/v2.5 均已支持，anchor 同款路径），足够保证同一张脸；
   多图 keyframes 需要多模型验证，收益低。
3. **storyboard = 组装而非新 LLM 调用**：其两个内容来源（whisper 歌词、LLM 画面）
   已是 AI 产出；再发一次 LLM 只会把相同信息换个格式，徒增成本、延迟与失败面
   （mock 回归还需新增 fixture 匹配）。确定性组装的输出可测试、可复现。
4. **photo 转存而非直用 uploads/ 路径**：任务目录自包含（产物面板可见、级联删除
   随任务走），且统一 PNG 扩展名避免 data-URI MIME 与真实字节不一致。
5. **歌手失败即任务失败**：与歌词「增值项降级」相反——歌手是用户本次任务的显式
   核心输入（先选模式、再上传/描述），静默丢脸等于交付与需求不符的成片。
6. **storyboard 只读**：music_video 无手动模式，`_build_scenes` 在 scenes 存在时
   一律跳过，没有任何回读 storyboard 的路径；登记 product 边会承诺做不到的重生成
   （与 `lyrics_json` 同理）。singer_image 则真实参与每次提交（文件被替换/删除后
   级联清掉段视频即可重生成），所以入图。

## 八、i18n 清单

### 前端（14 key × 22 语言，i18n_check 硬门禁）

| key | zh 示例 | 用途 |
|-----|---------|------|
| `mvSingerSection` | 歌手 / 演员 | 卡片标题 |
| `mvSingerNone` | 不使用 | 模式单选 |
| `mvSingerPhoto` | 使用我的照片 | 模式单选 |
| `mvSingerAi` | 用 AI 生成 | 模式单选 |
| `mvChoosePhoto` | 选择照片 | 文件按钮 |
| `mvNoPhoto` | 未选择照片 | 文件占位 |
| `mvSingerPromptLabel` | 表演者描述 | 文本域标签 |
| `mvSingerPromptPh` | 例：银色长发、黑色皮夹克的年轻女歌手… | 文本域占位 |
| `mvSingerHint` | 照片或 AI 生成的形象会作为每段视频的参考图，保持全片同一人物；描述留空（照片模式）时画面以参考图为准。 | 帮助文案 |
| `mvNeedPhoto` | 请先上传歌手照片 | 提交校验 |
| `mvNeedPrompt` | 请填写表演者描述 | 提交校验 |
| `mvStepReference` | 参考图 | 时间线步骤 |
| `artStoryboard` | 分镜故事板 | 产物标签 |
| `artSingerImage` | 歌手形象 | 产物标签 |

### 后端 CATALOG（13 key，zh/en 双语 parity 测试）

`validation.singer_mode_invalid` / `validation.singer_photo_required` /
`validation.singer_photo_format` / `validation.singer_photo_too_large` /
`validation.singer_photo_unreadable` / `validation.singer_prompt_required` /
`validation.singer_prompt_too_long` / `progress.music_video.singer_photo_running` /
`progress.music_video.singer_ai_running` / `progress.music_video.singer_done` /
`progress.music_video.singer_failed` / `progress.music_video.singer_photo_missing` /
`progress.music_video.storyboard_done`。

日志前缀沿用 `[MusicVideo]`（AGENTS.md 约定）。

## 九、测试计划

| 套件 | 新增/调整 |
|------|-----------|
| `tests/test_music_video_routes.py` | §5.7 校验矩阵 7 例 + 成功路径字段断言 + 时长失败照片回滚 |
| `tests/test_music_video_pipeline.py` | Phase 2 三分支（none/photo/ai）、断点续传、`_get_scene_ref_images`、storyboard 内容与 resume 补写、performer 注入 kwarg、提交携带参考图、构造函数 wire `image_generator` |
| `tests/test_artifacts.py` | defs 计数 5→7、id 集合、label、`checkpoint_for_artifact` 两个新映射 |
| `tests/test_dependency_graph.py` | singer_image 影响面（video+final）、storyboard 只读（affected==[]） |
| `tests/test_backend_i18n.py` | 新 key zh/en parity（既有 parity 测试自动覆盖） |
| `tests/mock_regression/` | conftest 补 image API patch 路径；新增 AI 歌手 e2e（断言 singer.png + storyboard.json + 完成） |
| `scripts/i18n_check.py` | 22 语言 14 key 全量落位 |
