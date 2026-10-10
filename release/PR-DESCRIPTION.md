# fix(music-video + ffmpeg): 修复歌曲上传被静默拒绝、Docker 下探测全失效，并补 v7.2 release notes

## Summary

四个独立问题，一次 PR：

### 1. 歌曲「传不上去 / 存不住」（用户报障，已复现）

上传**标称 5 分钟**的歌曲 → `422`，且已落盘的上传文件随即被回滚删除。

根因是容器/编码器的 padding：MP3（LAME 延迟 + 补齐）实测把 300.0s 的音源探测成
**300.04s**，而校验用的是严格 `duration > MAX_SONG_SECONDS`（300）。于是**界面与
文档承诺的「最长 5 分钟」整段不可达**——只有 4:59.9 以内才过。校验失败后
`_unlink_quietly` 删掉已上传文件，用户只看到「传不上去」。

实测边界（真实服务端）：`299.05s → 200`、`300.04s → 422(bug)`、`301.04s → 422(正确)`。

修复：新增 `SONG_DURATION_TOLERANCE_S = 0.25`，上下界同时放宽。取值远高于各类
容器 padding 上限，又远小于 0.5s（保证 300.5s 仍被拒，与既有用例一致）。
**容忍只作用于校验**：落库的 `song_duration` 仍是真实探测值（300.04），不截断歌曲。

顺带把校验文案从纯秒数改为「10 秒 – 5 分钟」，与前端 `mvSongHint` 及 22 语言文档口径一致。

### 2. serverless（Vercel）下超限上传撞平台层 413

Vercel Serverless Functions 对**请求体**有 4.5 MB 硬上限，且拦截发生在应用之前：
超限请求根本到不了 FastAPI，客户端只拿到一个无法解析的 HTML 413，前端显示「未知错误」。

修复：`effective_max_song_bytes()` 按运行时收紧上限（本地/Docker 仍 50 MB；
serverless 收到 4.5 MB 以内，留 0.5 MB 给 multipart 边界与其他表单字段），
经 `GET /api/config` 的 `max_song_bytes` / `serverless` 下发给前端，
前端在**提交前**校验并给出可读提示（新 key `mvSongTooLarge`，22 语言齐全）。
后端同样按该上限校验（API 客户端场景），复用既有 `validation.song_too_large` 文案。

> ⚠️ 这不解决 serverless 下 `/tmp` 临时存储的问题：任务产物在实例回收后仍会丢失。
> 那需要外部对象存储，属于 PR #1（persistent video worker）的范围，本 PR 不涉及。

### 3. Docker 里 ffprobe 为 None → 三处探测静默降级

`Dockerfile` 只把 imageio-ffmpeg 的静态 `ffmpeg` 软链进 PATH，**不提供 ffprobe**，
所以 `resolve_binary("ffprobe")` 在容器里恒为 `None`。三个 call site 把它直接塞进
`subprocess.run([...])` → `TypeError` 被 `except Exception` 吞掉：

| 位置 | Docker 里的后果 |
|---|---|
| `_try_ffmpeg_copy_concat` | 2.1a `-c copy` 快路径**从未生效**，每次拼接退化成 moviepy 全量重编码（路线图 2.1 承诺的 3~10 倍提速对该部署形态完全未兑现） |
| `_get_duration` | 恒返回 0.0，音视频时长比较与尾部补齐基于错值 |
| `_get_video_size` | 恒回退竖屏 (768, 1152)，1280×720 横屏任务的 SRT→ASS 字幕画布按竖屏计算 |

修复：三者改走 `ffmpeg_tool` 已有的三级兜底（ffprobe → `ffmpeg -i` → default）；
新增 `probe_video_signature()`（宽高 + 平均帧率，含 `ffmpeg -i` 回退解析）。

### 4. v7.2 release notes 与文档缺口

v7.1 / v7.2 已合入 master 但未发版：`APP_VERSION` 仍报 7.0.5，release notes 停在 v7.0.5。

- `core/config.py`：`APP_VERSION` → `7.2.0`
- 新增 `docs/public/release-notes/release_notes_v7.2.0.md`（英文，含 CI 抽取锚点 `## What's New`，合并 v7.1 + v7.2）
- `features.zh.md`：音乐视频整节缺失，补齐；`features.md`：补歌手/演员模式与 storyboard 产物
- `api.md`：补 `POST /api/tasks/music-video`

**未打 `v7.2.0` tag**：按发布规范，release notes 需你先批准。

## Verification

- **真实服务端复现 + 修复后复验**：5:00 歌曲 `422 → 200`，`song.mp3` 落盘（1.4 MB，
  `song_duration=300.04` 未截断）；5:01 仍 `422`，文案可读。
- `tests/test_music_video_routes.py`：新增 14 例（时长边界 9 例 + serverless 上限 3 例，
  并修正 2 处旧用例改为 patch `effective_max_song_bytes`）。**已验证修复前 5 例失败**。
- `tests/test_routes.py`：新增 2 例（`/api/config` 下发上限，serverless 下收紧）。
- 新增 `tests/test_no_ffprobe_environment.py`（5 例，slow）：真实 ffmpeg + 真实素材，
  仅屏蔽 ffprobe 复刻容器环境。**已验证修复前 3 例失败**（快路径 False / 时长 0.0 /
  尺寸恒 768×1152）、修复后全绿；另 5 例 `probe_video_signature` 单元。
- 3 处测试 helper 的裸 `"ffmpeg"` 改用 `resolve_binary`，使整套测试在无系统 ffmpeg 的环境也能运行。
- 全量 `pytest`：基线 **1574 passed / 17 failed** → **1611 passed / 4 failed**
  （4 例为既有网络依赖失败：`test_server_app` lifespan ×2、`test_voice_multilang` ×2）。
  同一套另在「无系统 ffmpeg」配置下跑过，结果一致。
- `ruff check`（CI 范围）/ `py_compile` / `scripts/i18n_check.py`（exit 0，22 语言齐全）均通过。
- 前端：`npm run build`（`vue-tsc --noEmit` + `vite build`）通过，`static/` 产物已重新构建提交；
  `npm test`（vitest）12/12。

## 范围说明

- `frontend/package-lock.json` 未改动（安装时为绕过被墙的 mirror 临时改写，已还原）。
- 未改动任何既有行为：本地/Docker 的 50 MB 与 5 分钟上限语义不变，
  只是边界容错与错误提示更诚实。
