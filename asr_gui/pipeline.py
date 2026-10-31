"""把「视频链接 → 文字稿」的流程编排成可测试的一串步骤。

GUI 与命令行共用这一层，区别只在于注入的 ``transcribe`` / ``emit_segments``：

* GUI 里的 ``transcribe`` 把进度塞进 ``queue.Queue``，由主线程写 txt；
* CLI 里的实现直接写文件、打印日志。

这样「字幕优先、分 P 拆分、失败回退语音转写、取消」这些规则只写一份。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import bili
import sources
from sources import SubtitleSegment

#: ``transcribe(媒体文件, 本页 jsonl, 时间轴偏移) -> 成功与否``
TranscribeCallback = Callable[[Path, Path, float], bool]
#: ``emit_segments(字幕片段, 时间轴偏移)``
EmitSegmentsCallback = Callable[[list[SubtitleSegment], float], None]
LogCallback = Callable[[str], None]
#: ``progress(已完成秒数, 总秒数, 当前说明)``
ProgressCallback = Callable[[float, float, str], None]
#: ``download_progress(已下载字节, 总字节, 说明)``
DownloadProgressCallback = Callable[[int, int, str], None]


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


@dataclass
class PipelineResult:
    ok: bool
    total_seconds: float = 0.0
    pages_done: int = 0
    subtitle_pages: int = 0
    error: str = ""
    cancelled: bool = False
    titles: list[str] = field(default_factory=list)
    #: [(分 P 号, 该 P 在总时间轴上的起始秒)]，供调用方把分段 jsonl 合并成完整 txt
    page_offsets: list[tuple[int, float]] = field(default_factory=list)


def _noop_log(_message: str) -> None:
    return None


def run_url_pipeline(
    url: str,
    *,
    transcript_dir: Path,
    stem: str,
    downloads_dir: Path,
    transcribe: TranscribeCallback,
    emit_segments: EmitSegmentsCallback,
    cookies: dict[str, str] | None = None,
    pages_spec: str = "",
    subtitle_first: bool = True,
    cookies_from_browser: str = "",
    cookiefile: str = "",
    log: LogCallback = _noop_log,
    progress: ProgressCallback | None = None,
    download_progress: DownloadProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> PipelineResult:
    """下载/取字幕并把一集或多分 P 的内容交给 ``transcribe``。"""
    cancelled = lambda: cancel is not None and cancel.is_set()  # noqa: E731 - 保持内联，避免多余闭包
    text = (url or "").strip()
    if not sources.is_supported_url(text):
        return PipelineResult(ok=False, error="不是有效的 http(s) 视频链接。")

    if bili.is_bilibili_url(text):
        return _run_bilibili(
            text,
            transcript_dir=transcript_dir,
            stem=stem,
            downloads_dir=downloads_dir,
            transcribe=transcribe,
            emit_segments=emit_segments,
            cookies=cookies,
            pages_spec=pages_spec,
            subtitle_first=subtitle_first,
            cookies_from_browser=cookies_from_browser,
            cookiefile=cookiefile,
            log=log,
            progress=progress,
            download_progress=download_progress,
            cancelled=cancelled,
            cancel=cancel,
        )
    return _run_single(
        text,
        stem=stem,
        transcript_dir=transcript_dir,
        downloads_dir=downloads_dir,
        transcribe=transcribe,
        cookies_from_browser=cookies_from_browser,
        cookiefile=cookiefile,
        log=log,
        progress=progress,
        download_progress=download_progress,
        cancelled=cancelled,
        cancel=cancel,
    )


def _run_single(
    url: str,
    *,
    stem: str,
    transcript_dir: Path,
    downloads_dir: Path,
    transcribe: TranscribeCallback,
    cookies_from_browser: str,
    cookiefile: str,
    log: LogCallback,
    progress: ProgressCallback | None,
    download_progress: DownloadProgressCallback | None,
    cancelled: Callable[[], bool],
    cancel: threading.Event | None,
) -> PipelineResult:
    """非 B 站链接：直接下整段音频再转写（每 300 秒分块）。"""
    log("开始下载音频…")
    result = sources.download_audio(
        url,
        downloads_dir,
        progress_cb=download_progress,
        cancel=cancel,
        log=log,
        cookies_from_browser=cookies_from_browser or None,
        cookiefile=cookiefile or None,
    )
    if result.cancelled:
        return PipelineResult(ok=False, cancelled=True, error="已取消下载。")
    if not result.ok or result.path is None:
        return PipelineResult(ok=False, error=result.error or "下载失败。")
    log(f"下载完成：{result.title or result.path.name}")
    if cancelled():
        return PipelineResult(ok=False, cancelled=True, error="已取消。")
    jsonl_path = transcript_dir / f"{stem}.jsonl"
    ok = transcribe(result.path, jsonl_path, 0.0)
    return PipelineResult(
        ok=ok,
        error="" if ok else "转写中断。",
        pages_done=1 if ok else 0,
        titles=[result.title],
    )


def _run_bilibili(
    url: str,
    *,
    transcript_dir: Path,
    stem: str,
    downloads_dir: Path,
    transcribe: TranscribeCallback,
    emit_segments: EmitSegmentsCallback,
    cookies: dict[str, str] | None,
    pages_spec: str,
    subtitle_first: bool,
    cookies_from_browser: str,
    cookiefile: str,
    log: LogCallback,
    progress: ProgressCallback | None,
    download_progress: DownloadProgressCallback | None,
    cancelled: Callable[[], bool],
    cancel: threading.Event | None,
) -> PipelineResult:
    """B 站：先列分 P，每 P 先试 AI 字幕，没有字幕的才下载音频转写。"""
    try:
        video = bili.fetch_video(url, cookies)
        pages = bili.select_pages(video.pages, pages_spec)
    except bili.BiliError as exc:
        return PipelineResult(ok=False, error=str(exc))

    if video.title:
        log(f"标题：{video.title}")
    if len(video.pages) > 1:
        chosen = "全部分 P" if not pages_spec.strip() else pages_spec.strip()
        log(
            f"检测到分 P 视频：共 {len(video.pages)} 个，本次处理 "
            f"{len(pages)} 个（{chosen}）"
        )

    total_seconds = sum(page.duration for page in pages) or 1.0
    offset = 0.0
    done = 0
    subtitle_pages = 0
    page_offsets: list[tuple[int, float]] = []
    for index, page in enumerate(pages, start=1):
        page_offsets.append((page.page, offset))
        if cancelled():
            return PipelineResult(
                ok=False, total_seconds=offset, pages_done=done, cancelled=True,
                error="已取消。",
            )
        log(f"[{index}/{len(pages)}] {page.label}（{format_duration(page.duration)}）")
        segments: list[SubtitleSegment] | None = None
        if subtitle_first:
            segments = _try_subtitles(video, page, cookies, log)
        if segments:
            emit_segments(segments, offset)
            subtitle_pages += 1
            offset += page.duration or (segments[-1].end if segments else 0.0)
            done += 1
            if progress is not None:
                progress(offset, total_seconds, f"{page.label}（字幕）")
            continue

        media_url = bili.with_page(url, page.page) if len(video.pages) > 1 else url
        downloaded = sources.download_audio(
            media_url,
            downloads_dir,
            progress_cb=download_progress,
            cancel=cancel,
            log=log,
            cookies_from_browser=cookies_from_browser or None,
            cookiefile=cookiefile or None,
        )
        if downloaded.cancelled:
            return PipelineResult(
                ok=False, total_seconds=offset, pages_done=done, cancelled=True,
                error="已取消下载。", page_offsets=page_offsets,
            )
        if not downloaded.ok or downloaded.path is None:
            return PipelineResult(
                ok=False, total_seconds=offset, pages_done=done,
                subtitle_pages=subtitle_pages, page_offsets=page_offsets,
                error=f"{page.label} 下载失败：{downloaded.error or '未知原因'}",
            )
        jsonl_path = transcript_dir / f"{stem}.p{page.page}.jsonl"
        if not transcribe(downloaded.path, jsonl_path, offset):
            return PipelineResult(
                ok=False, total_seconds=offset, pages_done=done,
                subtitle_pages=subtitle_pages, page_offsets=page_offsets,
                error=f"{page.label} 转写中断。",
            )
        offset += page.duration or 0.0
        done += 1
        if progress is not None:
            progress(offset, total_seconds, page.label)

    return PipelineResult(
        ok=True,
        total_seconds=offset,
        pages_done=done,
        subtitle_pages=subtitle_pages,
        titles=[video.title] if video.title else [],
        page_offsets=page_offsets,
    )


def _try_subtitles(
    video: bili.BiliVideo,
    page: bili.BiliPage,
    cookies: dict[str, str] | None,
    log: LogCallback,
) -> list[SubtitleSegment] | None:
    """取这一 P 的 AI 字幕；取不到（未登录、无字幕轨、下载失败）返回 None。"""
    try:
        track = bili.find_subtitle(video.bvid, video.aid, page.cid, cookies)
    except bili.BiliError as exc:
        log(f"字幕轨查询失败，改走语音转写：{exc}")
        return None
    if track is None:
        log("没有可用的 B 站字幕轨，改走语音转写。")
        return None
    try:
        segments = bili.fetch_subtitle_segments(track.url, cookies)
    except bili.BiliError as exc:
        log(f"字幕下载失败，改走语音转写：{exc}")
        return None
    if not segments:
        log("字幕轨是空的，改走语音转写。")
        return None
    log(f"使用 B 站字幕（{track.language}）：{len(segments)} 段，跳过下载与转写。")
    return segments


__all__ = ["PipelineResult", "format_duration", "run_url_pipeline"]
