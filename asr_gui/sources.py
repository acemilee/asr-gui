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
    cookies_from_browser: str | None = None,
    cookiefile: str | Path | None = None,
) -> dict:
    """构造 yt-dlp 的 options（抽成纯函数便于测试）。

    只下音频轨、不转码。分 P 由调用方逐个改写 ``?p=N`` 后单独下载（一次只下一个），
    因此这里开 ``noplaylist``——避免点一下链接顺手拖回整个合集。

    ``cookies_from_browser`` 直接用浏览器里的登录态（会员 / 风控内容需要）；
    ``cookiefile`` 用于浏览器扩展导出的 Netscape cookie 文件。
    """
    options: dict = {
        "format": "bestaudio/best",
        "paths": {"home": str(target_dir)},
        "outtmpl": "%(title).80s-%(id)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # 网络抖一下不该让整页失败：多给几次机会，退避重试（1,2,4,8,20,20…）
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "retry_sleep_functions": {
            "http": _retry_backoff,
            "fragment": _retry_backoff,
            "extractor": _retry_backoff,
        },
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
    if cookies_from_browser:
        # yt-dlp 只接受元组形式：("chrome",) / ("chrome", "Default")
        options["cookiesfrombrowser"] = (cookies_from_browser,)
    if cookiefile:
        options["cookiefile"] = str(cookiefile)
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


def _retry_backoff(n: int = 0, **_kwargs) -> float:
    """yt-dlp 的 ``retry_sleep_functions``：第 n 次重试前等多久（秒，封顶 20）。

    yt-dlp 内部是按 **关键字** 调用的 —— ``sleep_func(n=count - 1)``（见
    ``yt_dlp/utils/_utils.py`` 的 ``RetryManager.report_retry``）。参数名必须叫 ``n``
    并容忍多余关键字；写成 ``attempt`` 会在**第一次重试**时抛
    ``TypeError: _retry_backoff() got an unexpected keyword argument 'n'``
    （v0.1.3 首版就这么翻车的：单测用位置参数，永远测不出来）。
    """
    return float(min(2 ** min(max(0, int(n)), 5), 20))


#: 网络类失败的典型字样 → 给人看的解释。命中就补一句「多为代理/VPN 不稳定」。
_NETWORK_HINTS = (
    ("10061", "连接被拒绝（WinError 10061）"),
    ("10054", "连接被对端强制关闭（WinError 10054）"),
    ("10053", "连接被本机中止（WinError 10053）"),
    ("remote end closed", "对端关闭了连接"),
    ("connection reset", "连接被重置"),
    ("timed out", "读取超时"),
    ("temporary failure in name resolution", "DNS 解析失败"),
)


class _CancelSignal(BaseException):
    """用户点「停止」时的内部信号（BaseException 才会穿透 yt-dlp 的 except Exception）。"""


def describe_download_error(exc: BaseException) -> str:
    """把 yt-dlp 的异常翻译成一句能指导用户的话（原始信息保留在后面）。"""
    raw = str(exc)
    lowered = raw.lower()
    for needle, label in _NETWORK_HINTS:
        if needle in lowered:
            return (
                f"下载失败：{label}。这条链路是 yt-dlp 直连 B 站 CDN，出现这种情况几乎都是"
                "网络侧问题（代理 / VPN / 加速器中途断开、DNS 被劫持、站点限速），"
                "不是转写引擎的问题。\n"
                "建议：关掉或换一个代理 / VPN，确认浏览器能正常播放这个视频后，"
                "直接再点一次「开始转写」——已下载的部分会续传，不会从头来。\n"
                f"原始错误：{raw}"
            )
    return f"{exc.__class__.__name__}: {raw}"


def download_audio(
    url: str,
    target_dir: str | Path,
    progress_cb: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    log: LogCallback | None = None,
    cookies_from_browser: str | None = None,
    cookiefile: str | Path | None = None,
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
            # 用 BaseException 子类：yt-dlp 内部只 catch Exception，这样点了「停止」
            # 会立刻终止，而不是被它的重试逻辑又拉起 10 次。
            raise _CancelSignal()
        if status.get("status") != "downloading":
            return
        done = int(status.get("downloaded_bytes") or 0)
        total = int(status.get("total_bytes") or status.get("total_bytes_estimate") or 0)
        if progress_cb is not None:
            progress_cb(done, total, "下载中")

    options = build_download_options(
        home,
        progress_hook=hook,
        log=log,
        cookies_from_browser=cookies_from_browser,
        cookiefile=cookiefile,
    )
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
    except _CancelSignal:
        return DownloadResult(ok=False, cancelled=True, error="已取消下载。")
    except Exception as exc:  # yt-dlp 的异常类型很多，统一兜底
        if cancel is not None and cancel.is_set():
            return DownloadResult(ok=False, cancelled=True, error="已取消下载。")
        return DownloadResult(ok=False, error=describe_download_error(exc))


# ----------------------------------------------------------------- 登录态（cookie）
#: 界面下拉用：(yt-dlp 的浏览器代号, 显示文案)。空代号 = 不使用登录态。
COOKIE_BROWSERS = (
    ("", "不使用"),
    ("chrome", "Chrome"),
    ("edge", "Edge"),
    ("firefox", "Firefox"),
)
COOKIE_BROWSER_LABELS = {key: label for key, label in COOKIE_BROWSERS}


def browser_cookies(
    browser: str,
    domain: str = "bilibili.com",
    log: LogCallback | None = None,
) -> dict[str, str]:
    """从浏览器读取 cookie（借 yt-dlp 的 cookie 提取器，不自己解浏览器数据库）。

    读取失败（浏览器在跑、库里没登录、没装 yt-dlp）都只记一行日志并返回空字典：
    没有登录态时大部分公开视频仍可转写，不该因此整体失败。
    """
    if not browser:
        return {}
    try:
        import yt_dlp
    except ImportError:
        if log is not None:
            log("缺少 yt-dlp，无法读取浏览器登录态。")
        return {}
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            jar = ydl.cookiejar
            cookies = {
                cookie.name: cookie.value
                for cookie in jar
                if cookie.value and domain in (cookie.domain or "")
            }
    except Exception as exc:  # 浏览器锁 / 无权限 / 未登录，统一降级
        if log is not None:
            log(f"读取浏览器登录态失败（{browser}）：{exc.__class__.__name__}: {exc}")
        return {}
    if log is not None:
        if cookies:
            log(f"已读取 {browser} 登录态：{len(cookies)} 个 cookie。")
        else:
            log(f"{browser} 里没有 {domain} 的登录态，按未登录处理。")
    return cookies


def load_cookie_file(path: str | Path) -> dict[str, str]:
    """读 Netscape 格式的 cookie 文件（浏览器扩展导出的那种）。

    文件不存在或没法读时返回空字典：调用方按「没有登录态」继续跑即可。
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    cookies: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("# ") or line == "# Netscape HTTP Cookie File":
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        name, value = fields[5], fields[6]
        if name and value:
            cookies[name] = value
    return cookies


__all__ = [
    "COOKIE_BROWSERS",
    "COOKIE_BROWSER_LABELS",
    "SUBTITLE_SUFFIXES",
    "DownloadResult",
    "SubtitleSegment",
    "build_download_options",
    "browser_cookies",
    "clean_text",
    "describe_download_error",
    "download_audio",
    "is_supported_url",
    "load_cookie_file",
    "parse_srt",
    "parse_subtitle_file",
    "parse_vtt",
    "write_jsonl",
]
