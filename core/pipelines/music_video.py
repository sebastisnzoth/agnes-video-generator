"""core.pipelines.music_video -- 音乐视频流水线（类型 7 / v7.1）。

用户上传一首歌（≤5 分钟）→ 转码为 ``song.mp3`` 并探测时长 → （可选）faster-whisper
识别歌词 → 按 10 秒分段（段数 N = ceil(T/10)，末段为余数）→ LLM 依据每段歌词窗口
拟定画面描述 → 逐段调用 Agnes 视频 API 生成片段 → 精确定长裁剪拼接为无声时间轴 →
以歌曲本身作为唯一音轨合成成片。

与其他多场景流水线的区别：
- 无 TTS：音频步骤只做转码（``_generate_audio`` 覆写，返回 ``None``）；
- 字幕 = 歌词识别结果（关闭或识别失败时无字幕，任务照常完成）；
- 合成不走 ``concat_scenes_single_pass``：先用 ``build_timed_video_track`` 把各段
  裁剪/补帧到精确时长，再以 ``audio_volume=1.0`` 叠加歌曲，保证成片时长与歌曲一致、
  音量不被放大。

设计依据：docs/plans/v7.1/system_design.md §四。
"""

import asyncio
import json
import logging
import math
import os
import shutil
from typing import List, Optional, Tuple

from core.api.agnes_image import AgnesImageAPI
from core.api.agnes_video import AgnesVideoAPI
from core.audio.lyrics import LyricLine, get_lyrics_model_name, lyric_lines_to_srt, transcribe_lyrics
from core.compositor.concatenator import VideoConcatenator
from core.compositor.ffmpeg_tool import probe_duration
from core.compositor.music_timeline import build_timed_video_track, plan_uniform_spans, transcode_song_to_mp3
from core.config import DEFAULT_IMAGE_MODEL, DEFAULT_TEXT_MODEL
from core.pipelines import MultiScenePipeline
from core.screenwriter import Screenwriter
from models.task import MusicVideoTask, SceneTask, SubtitleStyle

logger = logging.getLogger(__name__)

# 未指定风格时的默认视觉风格（PRD §八）
DEFAULT_MUSIC_VIDEO_STYLE = "cinematic music video, consistent color grading"

# v7.2：歌手/演员参考图与分镜故事板（产物清单见 system_design §二/§五）
SINGER_IMAGE_FILENAME = "singer.png"
STORYBOARD_FILENAME = "storyboard.json"
# ai 模式描述缺失时的兜底 t2i 提示词（路由已强制必填，此处仅防御测试直建态）
_DEFAULT_SINGER_PROMPT_EN = (
    "a singer performing on stage, upper body, facing camera, cinematic lighting, no text"
)
# photo 模式未填描述时注入分镜提示词的占位（人物一致性由参考图本身承担）
_PERFORMER_FROM_REFERENCE = "the performer from the reference image"
_PERFORMER_GENERIC = "the lead singer"

# 上传与时长约束（PRD §三.1、system_design §4.1）
SONG_UPLOAD_EXTS = frozenset({".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"})
MAX_SONG_BYTES = 50 * 1024 * 1024
MIN_SONG_SECONDS = 10
MAX_SONG_SECONDS = 300
# 每段目标时长（秒）：段数 N = ceil(T / CLIP_SECONDS)，最多 30 段
CLIP_SECONDS = 10
# 仅支持横屏 16:9 与竖屏 2:3（与 Agnes 视频 API 的常用分辨率一致）
MUSIC_VIDEO_SIZES = frozenset({(1280, 720), (768, 1152)})

# 任务目录内的产物文件名（产物清单见 system_design §八）
SONG_FILENAME = "song.mp3"
LYRICS_JSON_FILENAME = "lyrics.json"
LYRICS_SRT_FILENAME = "lyrics.srt"
SUBTITLE_SRT_FILENAME = "full_subtitle.srt"
TIMELINE_FILENAME = "timeline.mp4"
FINAL_VIDEO_FILENAME = "final_video.mp4"

# 该段没有歌词时的窗口占位（仅用于 LLM 提示与日志，不作为字幕）
INSTRUMENTAL_TAG = "instrumental"

# 歌词字幕样式：居中偏下、白字黑描边、半透明底（用户只开关，不选样式）
MUSIC_SUBTITLE_STYLE = SubtitleStyle(
    font="STHeitiMedium.ttc",
    color="white",
    position=("center", "bottom-80"),
    fontsize=44,
    stroke_color="black",
    stroke_width=2,
    bg_color=(0, 0, 0, 140),
)

# build_scenes 阶段内的子进度（阶段边界由 MultiScenePipeline 决定：0.00 ~ 0.15）
_PROGRESS_SONG_READY = 0.02
_PROGRESS_LYRICS_RUNNING = 0.04
_PROGRESS_LYRICS_DONE = 0.08
_PROGRESS_PROMPTS_RUNNING = 0.10
# reference_images 阶段（v7.2 歌手/演员，阶段边界 0.15 ~ 0.30）
_PROGRESS_SINGER_RUNNING = 0.16
_PROGRESS_SINGER_DONE = 0.28
# 合成阶段起点（阶段边界 0.90 ~ 0.98）
_PROGRESS_COMPOSITE = 0.90


def _stage_singer_photo(src: str, dst: str) -> None:
    """把用户上传的歌手照片转存为任务目录内的 PNG（v7.2，在线程中执行）。

    EXIF 方向校正 → 最长边 >2048 等比缩至 2048 → 保存 ``dst``（固定扩展名
    ``singer.png``，避免 data-URI MIME 与真实字节不一致；产物清单按固定文件名
    解析）。Pillow 为硬依赖（requirements.txt），任一环节异常向上抛，
    由调用方包成 RuntimeError 使任务失败。
    """
    from PIL import Image, ImageOps

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        # PNG 不支持 CMYK/YCbCr 等模式；统一转 RGB（保留已兼容的 L/RGBA/P）
        if im.mode not in ("L", "LA", "RGB", "RGBA", "P"):
            im = im.convert("RGB")
        if max(im.size) > 2048:
            im.thumbnail((2048, 2048))
        im.save(dst, "PNG")


class MusicVideoPipeline(MultiScenePipeline):
    """音乐视频生成流水线。

    歌曲 → （歌词）→ 定长分段 → 逐段视频 → 定长时间轴 → 歌曲作为唯一音轨合成。
    v7.2：可选歌手/演员参考图（照片转存 / AI 生成）+ 分镜故事板 storyboard.json。
    """

    def __init__(
        self,
        api_key: str,
        task_id: str,
        dir_name: Optional[str] = None,
        chat_model: str = DEFAULT_TEXT_MODEL,
        image_model: str = DEFAULT_IMAGE_MODEL,
        video_model: str = "agnes-video-v2.0",
        progress_callback: Optional[callable] = None,
        shutdown_event: Optional = None,
    ):
        super().__init__(api_key, task_id, dir_name, progress_callback, shutdown_event)
        self.image_generator = AgnesImageAPI(api_key=api_key, model=image_model)
        self.video_api = AgnesVideoAPI(api_key=api_key, model=video_model)
        self.screenwriter = Screenwriter(api_key=api_key, model=chat_model)
        self._state: Optional[MusicVideoTask] = None

    @property
    def state(self) -> Optional[MusicVideoTask]:
        return self._state

    # ------------------------------------------------------------------
    # 基类钩子
    # ------------------------------------------------------------------

    def _get_watermark_language_text(self) -> str:
        """水印语言检测文本：优先歌词（前几行），其次歌名与风格。"""
        head = " ".join(str(item.get("text", "")) for item in (self._state.lyric_lines or [])[:8])
        return head or self._state.song_name or self._state.style

    def _get_init_message(self) -> str:
        return self._t("progress.music_video.init")

    def _get_pausable_steps(self) -> set:
        """音乐视频没有需要人工介入的步骤，不提供手动暂停点。"""
        return set()

    async def _build_reference_images(self) -> None:
        """Step: 生成歌手/演员参考图（v7.2）。

        - ``none``：保持 v7.1 行为（无参考图，纯 t2v）；
        - ``photo``：用户照片转存为 ``singer.png``；
        - ``ai``：t2i 按描述生成 ``singer.png``。

        ``singer.png`` 已存在时跳过（断点续传）。歌手是用户显式选择的核心输入，
        任何失败都抛错使任务失败（PRD FR-7），而非静默降级。
        """
        state = self._state
        mode = state.singer_mode or "none"
        if mode == "none":
            return

        dst = os.path.join(self.working_dir, SINGER_IMAGE_FILENAME)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            state.singer_image = dst
            self.task_manager.update_state(singer_image=dst)
            logger.info("[MusicVideo] singer image already exists, skipping")
            return

        if mode == "photo":
            await self._emit(
                "step_reference_images", "running",
                self._t("progress.music_video.singer_photo_running"),
                _PROGRESS_SINGER_RUNNING,
            )
            src = state.singer_photo or ""
            if not src or not os.path.exists(src):
                raise RuntimeError(self._t("progress.music_video.singer_photo_missing"))
            try:
                await asyncio.to_thread(_stage_singer_photo, src, dst)
            except Exception as e:
                logger.error("[MusicVideo] stage singer photo failed: %s", e)
                raise RuntimeError(
                    self._t("progress.music_video.singer_failed", reason=str(e))
                ) from e
        else:  # ai
            await self._emit(
                "step_reference_images", "running",
                self._t("progress.music_video.singer_ai_running"),
                _PROGRESS_SINGER_RUNNING,
            )
            prompt = (state.singer_prompt or "").strip() or _DEFAULT_SINGER_PROMPT_EN
            size = f"{state.video_width}x{state.video_height}"
            try:
                img_output = await self.image_generator.generate_single_image(
                    prompt=prompt, size=size,
                )
                await img_output.save(dst)
            except Exception as e:
                logger.error("[MusicVideo] singer image generation failed: %s", e)
                raise RuntimeError(
                    self._t("progress.music_video.singer_failed", reason=str(e))
                ) from e

        state.singer_image = dst
        self.task_manager.update_state(singer_image=dst)
        await self._emit(
            "step_reference_images", "completed",
            self._t("progress.music_video.singer_done"), _PROGRESS_SINGER_DONE,
        )

    def _get_scene_ref_images(self, scene: SceneTask, index: int) -> List[str]:
        """v7.2：所有段共用同一张歌手/演员参考图（ti2vid 保持人物一致）。"""
        state = self._state
        singer_image = state.singer_image if state else ""
        if singer_image and os.path.exists(singer_image):
            return [singer_image]
        return []

    # ------------------------------------------------------------------
    # Phase 1: 分段（歌曲转码 → 歌词 → 定长切段 → 逐段提示词）
    # ------------------------------------------------------------------

    async def _build_scenes(self) -> None:
        """构建分段：产出 ``scenes`` 与精确浮点 ``scene_spans``。

        断点续传：``scenes`` 与 ``scene_spans`` 已存在时直接复用，不重复调用 LLM 与识别。
        """
        state = self._state
        if state.scenes and state.scene_spans:
            logger.info("[MusicVideo] _build_scenes: SKIP (scenes already exist)")
            # v7.2：旧任务/中断恢复时 scenes 已在但故事板缺失 → 从 state 补写（不调 LLM）
            storyboard_path = os.path.join(self.working_dir, STORYBOARD_FILENAME)
            if not os.path.exists(storyboard_path):
                try:
                    self._write_storyboard_from_state()
                except Exception as e:
                    logger.warning("[MusicVideo] storyboard backfill failed: %s", e)
            return

        self._check_shutdown()
        song_path = await self._prepare_song()
        await self._emit(
            "step_build_scenes", "running",
            self._t("progress.music_video.song_ready", seconds=f"{state.song_duration:.1f}"),
            _PROGRESS_SONG_READY,
        )

        lines: List[LyricLine] = []
        if state.lyrics_enabled:
            self._check_shutdown()
            recognized = await self._recognize_lyrics(song_path)
            if recognized is not None:
                lines = recognized
                self._persist_lyrics(lines)

        spans = plan_uniform_spans(state.song_duration, state.clip_duration)
        windows = [self._lyric_window(lines, start, end) for start, end in spans]
        style = (state.style or "").strip() or DEFAULT_MUSIC_VIDEO_STYLE
        state.style = style
        performer = self._resolve_performer_text()

        self._check_shutdown()
        prompts, source = await self._generate_prompts(style, windows, performer=performer)

        scenes: List[SceneTask] = []
        for i, (start, end) in enumerate(spans):
            window = windows[i]
            scenes.append(SceneTask(
                index=i,
                scene_prompt=prompts[i],
                narration_text="" if window == INSTRUMENTAL_TAG else window,
                # 请求时长向上取整（≥3 秒）；合成时以精确跨度裁剪，见 _composite_final
                duration=max(3, math.ceil(round(end - start, 6))),
            ))

        state.scene_spans = spans
        state.scenes = scenes
        state.lyric_lines = [line.to_dict() for line in lines]
        self.task_manager.update_state(
            scenes=[s.model_dump() for s in scenes],
            scene_spans=spans,
            lyric_lines=state.lyric_lines,
        )
        self.save_prompts({
            "style": style,
            "scene_prompts": prompts,
            "lyric_windows": windows,
            "scene_spans": spans,
            "prompt_source": source,
        })
        self._write_storyboard(style, spans, windows, prompts, source)
        await self._emit(
            "step_build_scenes", "running",
            self._t("progress.music_video.storyboard_done", n=len(spans)),
            _PROGRESS_PROMPTS_RUNNING,
        )

    def _resolve_performer_text(self) -> str:
        """按歌手模式解析注入分镜提示词的表演者文本（v7.2，纯读 state）。

        ``none`` 返回空串（提示词与 v7.1 逐字节一致）；``photo`` 未填描述时用
        占位短语（人物一致性由参考图承担）；``ai`` 描述为路由必填，空值仅可能
        来自测试直建态，回退通用短语。
        """
        state = self._state
        mode = state.singer_mode or "none"
        if mode == "none":
            return ""
        text = (state.singer_prompt or "").strip()
        if text:
            return text
        if mode == "photo":
            return _PERFORMER_FROM_REFERENCE
        return _PERFORMER_GENERIC

    def _write_storyboard(
        self, style: str, spans: List[List[float]], windows: List[str],
        prompts: List[str], prompt_source: str,
    ) -> str:
        """写出 ``storyboard.json``（v7.2：时间段 + AI 歌词 + LLM 画面的组装产物）。"""
        state = self._state
        mode = state.singer_mode or "none"
        storyboard = {
            "format_version": "1.0",
            "song": {"name": state.song_name, "duration": state.song_duration},
            "style": style,
            "singer": {
                "mode": mode,
                "description": (state.singer_prompt or "").strip(),
                "image": SINGER_IMAGE_FILENAME if mode != "none" else None,
            },
            "prompt_source": prompt_source,
            "segments": [
                {
                    "index": i,
                    "start": round(float(start), 3),
                    "end": round(float(end), 3),
                    "lyrics": windows[i],
                    "visual": prompts[i],
                }
                for i, (start, end) in enumerate(spans)
            ],
        }
        path = os.path.join(self.working_dir, STORYBOARD_FILENAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(storyboard, f, ensure_ascii=False, indent=2)
        logger.info("[MusicVideo] storyboard saved → %s (%d segments)", path, len(spans))
        return path

    def _write_storyboard_from_state(self) -> str:
        """resume 补写：scenes 已存在时从 state 重建故事板（不调 LLM）。"""
        state = self._state
        lyric_dicts = state.lyric_lines or []
        windows: List[str] = []
        for start, end in state.scene_spans:
            texts = [
                str(d.get("text", "")) for d in lyric_dicts
                if float(d.get("end", 0.0)) > start and float(d.get("start", 0.0)) < end
            ]
            windows.append(" / ".join(t for t in texts if t) or INSTRUMENTAL_TAG)
        prompts = [s.scene_prompt for s in state.scenes]

        # prompt_source 以同一次生成写入的 prompts.json 为准，缺失时标 unknown
        prompt_source = "unknown"
        prompts_path = os.path.join(self.working_dir, "prompts.json")
        try:
            with open(prompts_path, "r", encoding="utf-8") as f:
                prompt_source = str(json.load(f).get("prompt_source") or "unknown")
        except (OSError, ValueError):
            pass

        return self._write_storyboard(
            state.style, state.scene_spans, windows, prompts, prompt_source,
        )

    async def _prepare_song(self) -> str:
        """确保 ``song.mp3`` 存在并写回 ``song_duration``（幂等，可重复调用）。

        Returns:
            ``song.mp3`` 的绝对路径。

        Raises:
            RuntimeError: 源文件缺失、转码失败或时长不可读。
        """
        state = self._state
        dst = os.path.join(self.working_dir, SONG_FILENAME)
        if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
            if not state.song_file or not os.path.exists(state.song_file):
                raise RuntimeError(self._t("progress.music_video.song_missing"))
            await asyncio.to_thread(transcode_song_to_mp3, state.song_file, dst)

        duration = await asyncio.to_thread(probe_duration, dst)
        if duration <= 0:
            raise RuntimeError(self._t("progress.music_video.song_unreadable"))
        state.song_duration = round(float(duration), 3)
        state.combined_audio = dst
        self.task_manager.update_state(song_duration=state.song_duration, combined_audio=dst)
        return dst

    async def _recognize_lyrics(self, song_path: str) -> Optional[List[LyricLine]]:
        """自动识别歌词。任何失败都降级，不中断任务。

        Returns:
            识别成功时返回行列表（纯伴奏可为空列表）；识别失败时返回 ``None``。
        """
        await self._emit(
            "step_build_scenes", "running",
            self._t("progress.music_video.lyrics_running"), _PROGRESS_LYRICS_RUNNING,
        )
        try:
            lines = await asyncio.to_thread(transcribe_lyrics, song_path)
        except Exception as e:  # 歌词是增值项：依赖缺失 / 模型下载失败 / 推理异常一律降级
            logger.warning("[Lyrics] recognition unavailable, continuing without lyrics: %s", e)
            await self._emit(
                "step_build_scenes", "running",
                self._t("progress.music_video.lyrics_skipped"), _PROGRESS_LYRICS_DONE,
            )
            return None

        if lines:
            message = self._t("progress.music_video.lyrics_done", n=len(lines))
        else:
            message = self._t("progress.music_video.lyrics_empty")
        await self._emit("step_build_scenes", "running", message, _PROGRESS_LYRICS_DONE)
        return lines

    def _persist_lyrics(self, lines: List[LyricLine]) -> None:
        """写入 ``lyrics.json``（识别成功即写，允许为空）与 ``lyrics.srt``（有行时写）。"""
        payload = {"model": get_lyrics_model_name(), "lines": [line.to_dict() for line in lines]}
        with open(os.path.join(self.working_dir, LYRICS_JSON_FILENAME), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        if lines:
            with open(os.path.join(self.working_dir, LYRICS_SRT_FILENAME), "w", encoding="utf-8") as f:
                f.write(lyric_lines_to_srt(lines))

    @staticmethod
    def _lyric_window(lines: List[LyricLine], start: float, end: float) -> str:
        """取与时间段 ``[start, end)`` 有交集的歌词行，按行拼接为窗口文本。"""
        texts = [line.text for line in lines if line.end > start and line.start < end]
        return " / ".join(texts) if texts else INSTRUMENTAL_TAG

    async def _generate_prompts(
        self, style: str, windows: List[str], performer: str = "",
    ) -> Tuple[List[str], str]:
        """逐段画面描述：LLM 优先，条数不足或调用失败时由模板补齐。

        Args:
            performer: v7.2 歌手/演员描述；非空时透传给 LLM（关键字参数，
                空串时提示词与 v7.1 一致）。

        Returns:
            ``(prompts, source)``，``source`` ∈ {``llm``, ``partial``, ``template``}。
        """
        n = len(windows)
        await self._emit(
            "step_build_scenes", "running",
            self._t("progress.music_video.prompts_running"), _PROGRESS_PROMPTS_RUNNING,
        )
        generated: List[str] = []
        try:
            generated = await asyncio.to_thread(
                self.screenwriter.generate_music_video_prompts, style, windows,
                performer=performer,
            )
        except Exception as e:
            logger.warning("[MusicVideo] LLM prompts failed, using template prompts: %s", e)

        prompts = [
            generated[i] if i < len(generated) else self._template_prompt(style, i, n, performer)
            for i in range(n)
        ]
        if len(generated) >= n:
            source = "llm"
        elif generated:
            source = "partial"
        else:
            source = "template"
        if source != "llm":
            logger.warning("[MusicVideo] prompt source=%s (%d/%d from LLM)", source, len(generated), n)
            await self._emit(
                "step_build_scenes", "running",
                self._t("progress.music_video.prompts_fallback"), _PROGRESS_PROMPTS_RUNNING,
            )
        return prompts, source

    @staticmethod
    def _template_prompt(style: str, index: int, total: int, performer: str = "") -> str:
        """LLM 不可用时的模板提示词：只含风格与段落位置，不含歌词，避免画面出现文字。

        v7.2：``performer`` 非空时拼接表演者（与 LLM 路径保持同一信息面）。
        """
        head = f"{style}, {performer}" if performer else style
        return f"{head}, segment {index + 1} of {total}, no on-screen text"

    # ------------------------------------------------------------------
    # Phase 3: 视频生成 → 基类 _generate_videos（逐段提交 + 续传）
    # ------------------------------------------------------------------
    # 无需覆写：每段 prompt 取 scene_prompt，时长取 max(duration, 3)，
    # 已存在的 scene_{i}/video.mp4 直接复用（断点续传）。

    # ------------------------------------------------------------------
    # Phase 4: 音频（仅转码，不调用 TTS）
    # ------------------------------------------------------------------

    async def _generate_audio(self) -> Optional[object]:
        """确保 ``song.mp3`` 存在。无 TTS，因此不返回 ``sub_maker``。"""
        await self._prepare_song()
        return None

    # ------------------------------------------------------------------
    # Phase 5: 字幕（歌词 → full_subtitle.srt）
    # ------------------------------------------------------------------

    async def _generate_subtitles(self, sub_maker: Optional[object] = None) -> None:
        """关闭字幕或没有歌词时直接返回；否则复制 ``lyrics.srt`` 为 ``full_subtitle.srt``。"""
        state = self._state
        lyrics_srt = os.path.join(self.working_dir, LYRICS_SRT_FILENAME)
        if not state.subtitle_config.enabled or not state.lyric_lines or not os.path.exists(lyrics_srt):
            logger.info("[MusicVideo] subtitles skipped (disabled or no lyrics)")
            return
        subtitle_path = os.path.join(self.working_dir, SUBTITLE_SRT_FILENAME)
        shutil.copyfile(lyrics_srt, subtitle_path)
        self._set_subtitle_paths(subtitle_path, "")

    # ------------------------------------------------------------------
    # Phase 6: 合成（定长时间轴 + 歌曲音轨）
    # ------------------------------------------------------------------

    async def _composite_final(self) -> str:
        """各段裁剪/补帧到精确跨度 → 拼接无声时间轴 → 叠加歌曲（音量 1.0）与字幕。

        成片时长 = max(时间轴, 歌曲) ≈ 歌曲时长（``-t`` 强制截断）；音轨即歌曲本身。
        """
        state = self._state
        output_path = os.path.join(self.working_dir, FINAL_VIDEO_FILENAME)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path

        clip_paths: List[str] = []
        for scene in state.scenes:
            if not scene.video_file or not os.path.exists(scene.video_file):
                raise RuntimeError(self._t("progress.music_video.clip_missing", i=scene.index + 1))
            clip_paths.append(scene.video_file)

        song_path = state.combined_audio or os.path.join(self.working_dir, SONG_FILENAME)
        if not os.path.exists(song_path):
            raise RuntimeError(self._t("progress.music_video.song_missing"))

        timeline_path = os.path.join(self.working_dir, TIMELINE_FILENAME)
        await self._emit(
            "step_concatenation", "running",
            self._t("progress.music_video.composite_running"), _PROGRESS_COMPOSITE,
        )
        await asyncio.to_thread(
            build_timed_video_track,
            clip_paths, state.scene_spans, timeline_path, state.video_width, state.video_height,
        )

        has_srt = bool(state.subtitle_config.enabled and state.combined_subtitle
                       and os.path.exists(state.combined_subtitle))
        await asyncio.to_thread(
            VideoConcatenator.concat_videos_with_audio_overlay,
            [timeline_path],
            song_path,
            state.combined_subtitle if has_srt else None,
            output_path,
            MUSIC_SUBTITLE_STYLE if has_srt else None,
            state.subtitle_styles_path or None,
            1.0,  # audio_volume：歌曲已是正常响度，不做 edge_tts 式的 1.5 倍放大
        )

        try:
            os.remove(timeline_path)
        except OSError:
            pass
        return output_path
