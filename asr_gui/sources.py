"""输入来源：视频链接下载（yt-dlp）与字幕文件解析。

这个模块只做「把外部来源变成可交付的素材或段落」，不碰 tkinter，也不碰转写引擎：

* :func:`download_audio` —— 给一个视频页链接（B 站、抖音、YouTube…），用 yt-dlp 只取音频轨，
  下载到指定目录，返回本地文件路径。支持进度回调与取消。
* :func:`parse_subtitle_file` —— 读 ``.srt`` / ``.vtt``，返回带时间轴的段落，用来直接出稿。
* :func:`write_jsonl` —— 把段落按引擎的 JSONL 格式落盘（续跑逻辑与引擎互通）。

设计取舍：只用 yt-dlp 下载，不用它转码（转码依赖外部 ffmpeg），音频抽取与解码仍交给
项目的 PyAV 流程，这样打包成 exe 时不必再塞一个 ffmpeg。
"""
from __future__ import annotations

import html
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SUBTITLE_SUFFIXES = (".srt", ".vtt")
SUPPORTED_URL_PREFIXES = ("http://", "https://")

# 时间戳：00:00:01,000 / 00:00:01.000 / 0:00:01.500 / 01:02.500（VTT 允许省略小时）
_TS_FULL = re.compile(r"(?P<h>\d{1,3}):(?P<m>\d{1,2}):(?P<s>\d{1,2})[.,](?P<ms>\d{1,3})")
_TS_SHORT = re.compile(r"(?<!\d)(?P<m>\d{1,2}):(?P<s>\d{1,2})[.,](?P<ms>\d{1,3})")
_TAG = re.compile(r"<[^>]*>")
_ARROW = "-->"

ProgressCallback = Callable[[int, int, str], None]
LogCallback = Callable[[str], None]


@dataclass
class SubtitleSegment:
    """一段字幕。字段名与 engine.Segment 保持一致，便于共用 JSONL。"""

    start: float
    end: float
    text: str


@dataclass
class DownloadResult:
    """下载结果。``cancelled`` 单独标出来，避免把「用户点了停止」当成失败报错。"""

    ok: bool
    path: Path | None = None
    title: str = ""
    error: str = ""
    cancelled: bool = False


# --------------------------------------------------------------------- 字幕解析
def _parse_timestamp(value: str) -> float | None:
    """把 ``00:01:02,500`` / ``01:02.500`` 转成秒；解析不了返回 None。"""
    text = value.strip()
    match = _TS_FULL.search(text)
    if match:
        return (
            int(match.group("h")) * 3600
            + int(match.group("m")) * 60
            + int(match.group("s"))
            + int(match.group("ms").ljust(3, "0")) / 1000.0
        )
    match = _TS_SHORT.search(text)
    if match:
        return (
            int(match.group("m")) * 60
            + int(match.group("s"))
            + int(match.group("ms").ljust(3, "0")) / 1000.0
        )
    return None


def clean_text(value: str) -> str:
    """去掉字幕标记（``<i>``、``<c.loud>``、``<00:00:01.000>``）并解码实体。"""
    text = _TAG.sub("", value)
    text = html.unescape(text)
    text = text.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _cue_segments(content: str) -> list[SubtitleSegment]:
    """SRT / VTT 共用的解析：按空行切块，找带 ``-->`` 的时间行。"""
    segments: list[SubtitleSegment] = []
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.strip("\ufeff").rstrip() for line in block.split("\n")]
        if not lines:
            continue
        head = lines[0].strip()
        if head.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            # VTT 头或注释块：整块跳过（NOTE 块里也可能有 -->）
            continue
        arrow_index = next((i for i, line in enumerate(lines) if _ARROW in line), None)
        if arrow_index is None:
            continue
        parts = lines[arrow_index].split(_ARROW, 1)
        start = _parse_timestamp(parts[0])
        end = _parse_timestamp(parts[1].split()[0]) if parts[1].strip() else None
        if start is None:
            continue
        text = clean_text(" ".join(lines[arrow_index + 1 :]))
        if not text:
            continue
        if end is None or end < start:
            end = start
        segments.append(SubtitleSegment(start=start, end=end, text=text))
    segments.sort(key=lambda item: item.start)
    return segments


def parse_srt(content: str) -> list[SubtitleSegment]:
    """解析 SRT 文本。"""
    return _cue_segments(content)


def parse_vtt(content: str) -> list[SubtitleSegment]:
    """解析 WebVTT 文本。"""
    return _cue_segments(content)


def parse_subtitle_file(path: str | Path) -> list[SubtitleSegment]:
    """按扩展名解析字幕文件；扩展名不认识时按内容嗅探（含 ``-->`` 即当字幕）。"""
    target = Path(path)
    raw = target.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            content = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"字幕文件编码无法识别：{target}")

    suffix = target.suffix.lower()
    if suffix == ".vtt":
        return parse_vtt(content)
    if suffix == ".srt":
        return parse_srt(content)
    if _ARROW in content:
        return _cue_segments(content)
    raise ValueError(f"不支持的字幕格式：{target.suffix or '(无扩展名)'}，只支持 .srt / .vtt")


def write_jsonl(path: str | Path, segments: list[SubtitleSegment]) -> None:
    """按引擎的格式写 JSONL（``{"start":…,"end":…,"text":…}`` 每行一段）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for segment in segments:
            handle.write(
                json.dumps(
                    {"start": segment.start, "end": segment.end, "text": segment.text},
                    ensure_ascii=False,
                )
                + "\n"
            )


# ----------------------------------------------------------------- 视频链接下载
def is_supported_url(value: str) -> bool:
    """只接受 http(s) 链接（本地路径走「本地文件」）。"""
    text = (value or "").strip()
    return text.lower().startswith(SUPPORTED_URL_PREFIXES) and len(text) > len("https://")


def build_download_options(
    target_dir: str | Path,
    progress_hook: Callable[[dict], None] | None = None,
    log: LogCallback | None = None,
) -> dict:
    """构造 yt-dlp 的 options（抽成纯函数便于测试）。

    只下音频轨、不转码；分 P / 播放列表只取第一个，避免一次拖几个小时。
    """
    options: dict = {
        "format": "bestaudio/best",
        "paths": {"home": str(target_dir)},
        "outtmpl": "%(title).80s-%(id)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "windowsfilenames": True,
        "overwrites": False,
        "continuedl": True,
    }
    if progress_hook is not None:
        options["progress_hooks"] = [progress_hook]
    if log is not None:
        options["logger"] = _LoggerShim(log)
    return options


class _LoggerShim:
    """把 yt-dlp 的日志转成界面上的一行文字（默认它会往 stdout 打）。"""

    def __init__(self, log: LogCallback) -> None:
        self._log = log

    def debug(self, message: str) -> None:
        text = str(message).strip()
        if text.startswith("[debug]"):
            return
        if text.startswith("[download]"):
            text = text[len("[download]") :].strip()
        if text:
            self._log(text)

    def warning(self, message: str) -> None:
        self._log(f"警告：{str(message).strip()}")

    def error(self, message: str) -> None:
        self._log(f"错误：{str(message).strip()}")


def _pick_file(info: dict, target_dir: str | Path) -> Path | None:
    """从 yt-dlp 的 info 里找出实际落盘的文件。"""
    for entry in info.get("requested_downloads") or []:
        candidate = entry.get("filepath") or entry.get("_filename")
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    for key in ("filepath", "_filename"):
        candidate = info.get(key)
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    # 兜底：按 id 在目标目录里找（处理 yt-dlp 改了扩展名的情况）
    video_id = info.get("id") or ""
    if video_id:
        for candidate in sorted(Path(target_dir).glob(f"*{video_id}*"), reverse=True):
            if candidate.is_file():
                return candidate
    return None


def download_audio(
    url: str,
    target_dir: str | Path,
    progress_cb: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    log: LogCallback | None = None,
) -> DownloadResult:
    """用 yt-dlp 把这个链接的音频下载到 ``target_dir``。

    返回 :class:`DownloadResult`；用户点停止导致的取消会标成 ``cancelled=True``（不算失败）。
    """
    text = (url or "").strip()
    if not is_supported_url(text):
        return DownloadResult(ok=False, error="不是有效的 http(s) 视频链接。")

    try:
        import yt_dlp  # 延迟导入：只用本地文件/字幕时不依赖它
    except ImportError:
        return DownloadResult(
            ok=False,
            error="缺少 yt-dlp，无法下载视频链接。请先安装依赖："
            "python -m pip install -r requirements.txt",
        )

    home = Path(target_dir)
    home.mkdir(parents=True, exist_ok=True)

    def hook(status: dict) -> None:
        if cancel is not None and cancel.is_set():
            raise RuntimeError("__cancelled__")
        if status.get("status") != "downloading":
            return
        done = int(status.get("downloaded_bytes") or 0)
        total = int(status.get("total_bytes") or status.get("total_bytes_estimate") or 0)
        if progress_cb is not None:
            progress_cb(done, total, "下载中")

    options = build_download_options(home, progress_hook=hook, log=log)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(text, download=True)
            if info is None:
                return DownloadResult(ok=False, error="yt-dlp 没有返回视频信息。")
            if info.get("_type") == "playlist":
                entries = info.get("entries") or []
                if entries:
                    info = entries[0]
            title = str(info.get("title") or "").strip()
            path = _pick_file(info, home)
            if path is None:
                return DownloadResult(ok=False, title=title, error="下载结束但没找到落盘文件。")
            return DownloadResult(ok=True, path=path, title=title)
    except Exception as exc:  # yt-dlp 的异常类型很多，统一兜底
        if cancel is not None and cancel.is_set():
            return DownloadResult(ok=False, cancelled=True, error="已取消下载。")
        return DownloadResult(ok=False, error=f"{exc.__class__.__name__}: {exc}")


__all__ = [
    "SUBTITLE_SUFFIXES",
    "DownloadResult",
    "SubtitleSegment",
    "build_download_options",
    "clean_text",
    "download_audio",
    "is_supported_url",
    "parse_srt",
    "parse_subtitle_file",
    "parse_vtt",
    "write_jsonl",
]
