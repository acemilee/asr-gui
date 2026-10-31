"""bili.py 的离线测试：只替换传输层 ``bili._get_json``，不发真实网络请求。"""

from __future__ import annotations

import json

import bili
import pytest

VIEW_ENVELOPE = {
    "code": 0,
    "data": {
        "bvid": "BV1xx411c7mD",
        "aid": 123456,
        "title": "测试视频",
        "pages": [
            {"page": 1, "cid": 111, "part": "第一集", "duration": 60},
            {"page": 2, "cid": 222, "part": "第二集", "duration": 90},
        ],
    },
}

PAGELIST_ENVELOPE = {
    "code": 0,
    "data": {"pages": [{"page": 1, "cid": 111, "part": "第一集", "duration": 60}]},
}

PLAYER_WITH_SUBTITLE = {
    "code": 0,
    "data": {
        "subtitle": {
            "subtitles": [
                {"lan": "en", "subtitle_url": "//example.com/en.json"},
                {"lan": "ai-zh", "subtitle_url": "//example.com/ai-zh.json"},
            ]
        }
    },
}

PLAYER_NO_SUBTITLE = {"code": 0, "data": {"subtitle": {"subtitles": []}}}

SUBTITLE_JSON = json.dumps(
    {
        "body": [
            {"from": 1.0, "to": 3.5, "content": "第一句"},
            {"from": 4.0, "to": 6.0, "content": "第二句"},
        ]
    }
)


class _Calls:
    """按 URL 顺序返回预设响应的假 ``_get_json``。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen: list[str] = []

    def __call__(self, url, params, headers, timeout=20.0):
        self.seen.append(url)
        if not self.responses:
            raise bili.BiliError("没有更多假响应了")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def patch_get_json(monkeypatch):
    def install(*responses):
        fake = _Calls(*responses)
        monkeypatch.setattr(bili, "_get_json", fake)
        return fake

    return install


@pytest.fixture
def no_network(monkeypatch):
    """短链展开不许真的联网。"""

    def fake_resolve(url, timeout=10.0):
        return url

    monkeypatch.setattr(bili, "_resolve_short_url", fake_resolve)


class TestVideoId:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://www.bilibili.com/video/BV1xx411c7mD/?spm_id_from=333",
            "https://m.bilibili.com/video/BV1xx411c7mD?p=2",
            " https://b23.tv/BV1xx411c7mD ",
        ],
    )
    def test_finds_bvid(self, url, no_network):
        assert bili.parse_video_id(url)[0] == "BV1xx411c7mD"

    def test_av_number(self, no_network):
        assert bili.parse_video_id("https://www.bilibili.com/video/av170001") == ("", 170001)

    def test_no_id_returns_empty(self, no_network):
        assert bili.parse_video_id("https://www.bilibili.com/video/") == ("", 0)

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.bilibili.com/video/BV1xx411c7mD", True),
            ("https://b23.tv/abc", True),
            ("https://space.bilibili.com/1", True),
            ("https://www.youtube.com/watch?v=abc", False),
            ("", False),
        ],
    )
    def test_is_bilibili_url(self, url, expected):
        assert bili.is_bilibili_url(url) is expected


class TestPageSpec:
    @pytest.mark.parametrize(
        "spec,expected",
        [
            ("", set()),
            ("1", {1}),
            ("1,3-5", {1, 3, 4, 5}),
            ("1，3-5", {1, 3, 4, 5}),
            (" 2 , 4 ", {2, 4}),
            ("9-7", {7, 8, 9}),
            ("1,3-5,9-7", {1, 3, 4, 5, 7, 8, 9}),
            ("abc", set()),
            ("0", set()),
        ],
    )
    def test_parse(self, spec, expected):
        assert bili.parse_page_spec(spec) == expected


class TestWithPage:
    @pytest.mark.parametrize(
        "url,page,expected",
        [
            (
                "https://www.bilibili.com/video/BV1xx411c7mD",
                2,
                "https://www.bilibili.com/video/BV1xx411c7mD?p=2",
            ),
            (
                "https://www.bilibili.com/video/BV1xx411c7mD?p=1",
                3,
                "https://www.bilibili.com/video/BV1xx411c7mD?p=3",
            ),
            (
                "https://www.bilibili.com/video/BV1xx411c7mD?spm=1&p=1",
                4,
                "https://www.bilibili.com/video/BV1xx411c7mD?spm=1&p=4",
            ),
        ],
    )
    def test_rewrite(self, url, page, expected):
        assert bili.with_page(url, page) == expected


class TestFetchVideo:
    def test_view_success(self, patch_get_json, no_network):
        patch_get_json(VIEW_ENVELOPE)
        video = bili.fetch_video("https://www.bilibili.com/video/BV1xx411c7mD")
        assert video.bvid == "BV1xx411c7mD"
        assert video.title == "测试视频"
        assert [page.part for page in video.pages] == ["第一集", "第二集"]
        assert video.pages[1].label == "P2 第二集"
        assert video.total_duration == pytest.approx(150.0)

    def test_view_error_falls_back_to_pagelist(self, patch_get_json, no_network):
        patch_get_json(bili.BiliError("boom"), PAGELIST_ENVELOPE)
        video = bili.fetch_video("https://www.bilibili.com/video/BV1xx411c7mD")
        assert [page.page for page in video.pages] == [1]
        assert video.title == ""

    def test_both_fail(self, patch_get_json, no_network):
        patch_get_json(bili.BiliError("boom"), bili.BiliError("boom2"))
        with pytest.raises(bili.BiliError):
            bili.fetch_video("https://www.bilibili.com/video/BV1xx411c7mD")

    def test_rejects_non_video_url(self, patch_get_json, no_network):
        patch_get_json(VIEW_ENVELOPE)
        with pytest.raises(bili.BiliError):
            bili.fetch_video("https://www.youtube.com/watch?v=abc")


class TestSelectPages:
    def _pages(self):
        return (
            bili.BiliPage(page=1, cid=111, part="第一集", duration=60),
            bili.BiliPage(page=2, cid=222, part="第二集", duration=90),
        )

    def test_all_by_default(self):
        assert [page.page for page in bili.select_pages(self._pages(), "")] == [1, 2]

    def test_subset(self):
        assert [page.page for page in bili.select_pages(self._pages(), "2")] == [2]

    def test_out_of_range_raises(self):
        with pytest.raises(bili.BiliError):
            bili.select_pages(self._pages(), "5")


class TestFindSubtitle:
    def test_prefers_ai_zh_over_en(self, patch_get_json):
        calls = patch_get_json(PLAYER_WITH_SUBTITLE)
        subtitle = bili.find_subtitle("BV1xx411c7mD", 0, 111)
        assert subtitle is not None
        assert subtitle.language == "ai-zh"
        assert subtitle.url == "https://example.com/ai-zh.json"
        assert calls.seen == [bili.PLAYER_API]

    def test_none_when_empty(self, patch_get_json):
        patch_get_json(PLAYER_NO_SUBTITLE)
        assert bili.find_subtitle("BV1xx411c7mD", 0, 111) is None

    def test_query_error_returns_none(self, patch_get_json):
        patch_get_json(bili.BiliError("x"))
        assert bili.find_subtitle("BV1xx411c7mD", 0, 111) is None


class TestSubtitleSegments:
    def test_parse_body(self):
        segments = bili._segments_from_payload(SUBTITLE_JSON)
        assert [(seg.start, seg.end, seg.text) for seg in segments] == [
            (1.0, 3.5, "第一句"),
            (4.0, 6.0, "第二句"),
        ]

    def test_bad_numbers_skipped(self):
        payload = json.dumps(
            {
                "body": [
                    {"from": "x", "to": 1, "content": "a"},
                    {"from": 1, "to": 2, "content": "b"},
                    {"from": 3, "to": 4, "content": "   "},
                ]
            }
        )
        assert [seg.text for seg in bili._segments_from_payload(payload)] == ["b"]

    @pytest.mark.parametrize("payload", ["", "not json", "{}", '{"body": 1}'])
    def test_degenerate_payload(self, payload):
        assert bili._segments_from_payload(payload) == []

    def test_end_clamped_to_start(self):
        payload = json.dumps({"body": [{"from": 5.0, "to": 1.0, "content": "x"}]})
        segments = bili._segments_from_payload(payload)
        assert (segments[0].start, segments[0].end) == (5.0, 5.0)


class TestWbiSign:
    def test_adds_wts_and_w_rid(self):
        params = bili._wbi_sign({"foo": "bar"})
        assert "wts" in params
        assert len(params["w_rid"]) == 32

    def test_signature_is_stable_for_same_input(self):
        once = bili._wbi_sign({"foo": "bar"})
        again = bili._wbi_sign({"foo": "bar", "wts": once["wts"]})
        assert again["w_rid"] == once["w_rid"]


class TestHeaders:
    def test_has_referer_and_origin(self):
        headers = bili.build_api_headers()
        assert headers["Referer"] == "https://www.bilibili.com/"
        assert headers["Origin"] == "https://www.bilibili.com"
        assert "Cookie" not in headers

    def test_includes_cookies_when_given(self):
        headers = bili.build_api_headers({"SESSDATA": "abc", "bili_jct": ""})
        assert headers["Cookie"] == "SESSDATA=abc"


class TestGetJson:
    def test_network_error_becomes_bili_error(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise OSError("no network")

        monkeypatch.setattr(bili.urllib.request, "urlopen", boom)
        with pytest.raises(bili.BiliError):
            bili._get_json(bili.VIEW_API, {}, {})
