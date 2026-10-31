"""B 站只读接口：分 P 清单与 AI 字幕轨（纯标准库，测试可注入传输层）。

只用公开接口，不做任何写操作：

    1. ``x/web-interface/view``  取标题 / bvid / aid / 分 P 清单
    2. ``x/player/pagelist``     取分 P 清单（view 不可用时的兜底）
    3. ``x/player/wbi/v2``       取某个分 P（cid）的字幕轨清单
    4. ``subtitle_url``          直接下载 AI 字幕 JSON

B 站对 ``api.bilibili.com`` 的请求要求真实浏览器 UA + Referer/Origin；wbi 接口
在未签名时可能返回非 0 code，故与上游一致：非 0 时补一次 wbi 签名重试。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from sources import SubtitleSegment

BILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
PAGELIST_API = "https://api.bilibili.com/x/player/pagelist"
PLAYER_API = "https://api.bilibili.com/x/player/wbi/v2"
BILI_REFERER = "https://www.bilibili.com/"

# 优先级与上游保持一致：中文 AI 字幕优先，其次人工字幕，最后英文/日文。
SUBTITLE_LANGUAGES = (
    "ai-zh",
    "zh-hans",
    "zh-cn",
    "zh",
    "zh-hant",
    "zh-tw",
    "ai-en",
    "en",
    "ai-ja",
    "ja",
)

# wbi 签名用的混淆表（B 站前端公开常量；轮换时这里会失效，届时只需替换两个串）。
_WBI_IMG_KEY = "7cd084941338484aae1ad9425b84077c"
_WBI_SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"

_HOSTS = ("bilibili.com", "b23.tv")
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)


class BiliError(Exception):
    """接口调用失败；消息直接面向用户，便于原样展示。"""


@dataclass(frozen=True)
class BiliPage:
    page: int
    cid: int
    part: str
    duration: float

    @property
    def label(self) -> str:
        title = self.part.strip() or f"P{self.page}"
        return f"P{self.page} {title}"


@dataclass(frozen=True)
class BiliVideo:
    bvid: str
    aid: int
    title: str
    pages: tuple[BiliPage, ...]

    @property
    def total_duration(self) -> float:
        return sum(page.duration for page in self.pages)


@dataclass(frozen=True)
class BiliSubtitle:
    language: str
    url: str


def is_bilibili_url(value: str) -> bool:
    """是否是 B 站视频链接（含 b23.tv 短链）。"""
    try:
        host = (urllib.parse.urlparse(value).hostname or "").lower()
    except ValueError:
        return False
    return any(host == item or host.endswith("." + item) for item in _HOSTS)


def _resolve_short_url(url: str, timeout: float = 10.0) -> str:
    """展开 b23.tv 短链：只取重定向目标，不拉页面（避开视频页 412 风控）。"""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):  # noqa: D102 - 禁止跟随
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": BILI_USER_AGENT})
    try:
        with opener.open(request, timeout=timeout) as response:
            return str(response.url or url)
    except urllib.error.HTTPError as exc:
        return str(exc.headers.get("Location") or url)
    except (urllib.error.URLError, OSError):
        return url


def parse_video_id(url: str, timeout: float = 10.0) -> tuple[str, int]:
    """解析 (bvid, aid)；两者都解析不到时返回 ("", 0)。"""
    target = url
    for _ in range(2):
        match = _BV_RE.search(target)
        if match:
            return match.group(1), 0
        match = _AV_RE.search(target)
        if match:
            return "", int(match.group(1))
        if "b23.tv" not in target:
            break
        resolved = _resolve_short_url(target, timeout=timeout)
        if resolved == target:
            break
        target = resolved
    return "", 0


def _wbi_sign(params: dict[str, Any]) -> dict[str, Any]:
    mixin_key = "".join(sorted(_WBI_IMG_KEY + _WBI_SUB_KEY))[:32]
    signed = dict(params)
    signed["wts"] = int(time.time())
    query = urllib.parse.urlencode(sorted(signed.items()))
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return signed


def build_api_headers(cookies: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": BILI_USER_AGENT,
        "Referer": BILI_REFERER,
        "Origin": BILI_REFERER.rstrip("/"),
    }
    if cookies:
        headers["Cookie"] = "; ".join(
            f"{name}={value}" for name, value in cookies.items() if value
        )
    return headers


def _get_json(
    url: str,
    params: dict[str, Any],
    headers: dict[str, str],
    timeout: float = 20.0,
) -> dict[str, Any]:
    """请求并解析 JSON；非 0 code 时补 wbi 签名重试一次。测试会替换本函数。"""
    query = urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"{url}?{query}", headers=headers), timeout=timeout
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BiliError(f"接口请求失败：{exc}") from exc
    if data.get("code") == 0:
        return data
    signed = urllib.parse.urlencode(_wbi_sign(params))
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"{url}?{signed}", headers=headers), timeout=timeout
        ) as response:
            retried = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        retried = data
    if retried.get("code") == 0:
        return retried
    message = retried.get("message") or "未知错误"
    raise BiliError(
        f"接口返回 code={retried.get('code')}：{message}"
        "（部分内容需要登录态，可在「登录态」里选择浏览器）"
    )


def _pages_from(data: dict[str, Any] | list[Any] | None) -> tuple[BiliPage, ...]:
    """从 view 的 ``data.pages`` 或 pagelist 的 ``data``（本身就是列表）里取分 P。"""
    if isinstance(data, list):
        items: list[Any] = data
    elif isinstance(data, dict):
        items = data.get("pages") or []
    else:
        items = []
    pages = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            pages.append(
                BiliPage(
                    page=int(item.get("page") or 0),
                    cid=int(item.get("cid") or 0),
                    part=str(item.get("part") or ""),
                    duration=float(item.get("duration") or 0.0),
                )
            )
        except (TypeError, ValueError):
            continue
    pages.sort(key=lambda page: page.page)
    return tuple(pages)


def fetch_video(
    url: str,
    cookies: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> BiliVideo:
    """取标题与分 P 清单；view 打不开（412 风控）时退回 pagelist。"""
    bvid, aid = parse_video_id(url, timeout=timeout)
    if not bvid and not aid:
        raise BiliError("链接里没有解析出 BV 号 / av 号，请贴视频页地址。")
    headers = build_api_headers(cookies)
    params = {"bvid": bvid} if bvid else {"aid": aid}
    title = ""
    pages: tuple[BiliPage, ...] = ()
    try:
        view = _get_json(VIEW_API, params, headers, timeout=timeout)
        data = view.get("data") or {}
        title = str(data.get("title") or "")
        bvid = bvid or str(data.get("bvid") or "")
        aid = aid or int(data.get("aid") or 0)
        pages = _pages_from(data)
    except BiliError:
        pages = ()
    if not pages:
        listed = _get_json(PAGELIST_API, params, headers, timeout=timeout)
        pages = _pages_from(listed.get("data"))
    if not pages:
        raise BiliError("没有取到分 P 清单，可能是风控或链接不是视频页。")
    return BiliVideo(bvid=bvid, aid=aid, title=title, pages=pages)


def find_subtitle(
    bvid: str,
    aid: int,
    cid: int,
    cookies: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> BiliSubtitle | None:
    """取某个分 P 的首个可用字幕轨；没有字幕时返回 None。"""
    params: dict[str, Any] = {"cid": cid}
    if bvid:
        params["bvid"] = bvid
    else:
        params["aid"] = aid
    try:
        player = _get_json(PLAYER_API, params, build_api_headers(cookies), timeout=timeout)
    except BiliError:
        return None
    subtitle = (player.get("data") or {}).get("subtitle") or {}
    tracks = subtitle.get("subtitles") or []
    by_language: dict[str, str] = {}
    for track in tracks:
        language = str(track.get("lan") or "")
        url = str(track.get("subtitle_url") or "")
        if not language or not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        by_language.setdefault(language, url)
    for language in SUBTITLE_LANGUAGES:
        if language in by_language:
            return BiliSubtitle(language=language, url=by_language[language])
    return None


def _segments_from_payload(payload: str) -> list[SubtitleSegment]:
    """解析 AI 字幕 JSON：``{"body": [{"from": 0.0, "to": 1.5, "content": "…"}]}``。"""
    try:
        data = json.loads(payload)
    except ValueError:
        return []
    body = data.get("body")
    if not isinstance(body, list):
        return []
    segments: list[SubtitleSegment] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("from") or 0.0)
            end = float(item.get("to") or start)
        except (TypeError, ValueError):
            continue
        segments.append(SubtitleSegment(start=start, end=max(start, end), text=text))
    segments.sort(key=lambda segment: segment.start)
    return segments


def fetch_subtitle_segments(
    subtitle_url: str,
    cookies: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> list[SubtitleSegment]:
    """下载并解析某条字幕轨。"""
    headers = {
        "User-Agent": BILI_USER_AGENT,
        "Referer": BILI_REFERER,
    }
    if cookies:
        headers["Cookie"] = "; ".join(
            f"{name}={value}" for name, value in cookies.items() if value
        )
    try:
        with urllib.request.urlopen(
            urllib.request.Request(subtitle_url, headers=headers), timeout=timeout
        ) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        raise BiliError(f"字幕下载失败：{exc}") from exc
    return _segments_from_payload(payload)


def select_pages(
    pages: tuple[BiliPage, ...], spec: str = ""
) -> list[BiliPage]:
    """按 ``1,3-5`` 规格挑选分 P；空规格返回全部分 P。"""
    if not spec.strip():
        return list(pages)
    wanted = parse_page_spec(spec)
    if not wanted:
        return list(pages)
    selected = [page for page in pages if page.page in wanted]
    if not selected:
        raise BiliError(
            f"分 P 规格 {spec!r} 没有匹配到任何分 P（共 {len(pages)} 个）。"
        )
    return selected


def parse_page_spec(spec: str) -> set[int]:
    """把 ``1,3-5`` 解析成 {1,3,4,5}；非法片段忽略。"""
    numbers: set[int] = set()
    for chunk in re.split(r"[,，\s]+", spec.strip()):
        if not chunk:
            continue
        if "-" in chunk:
            left, _, right = chunk.partition("-")
            try:
                start, end = int(left), int(right)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            numbers.update(range(start, end + 1))
            continue
        try:
            numbers.add(int(chunk))
        except ValueError:
            continue
    return {number for number in numbers if number > 0}


def with_page(url: str, page: int) -> str:
    """给 B 站链接补/改 ``?p=N``，保留其他查询参数。"""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "p"]
    query.append(("p", str(page)))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )
