"""字幕解析 / 链接判定 / 下载参数 的单元测试（不联网、不加载模型）。"""

import json
import sys

import pytest
import sources

SRT = """1
00:00:01,000 --> 00:00:04,500
第一句字幕 &amp; 测试

2
00:00:05,000 --> 00:00:08,250
<i>第二句字幕</i>
"""

VTT = """WEBVTT
Kind: captions

NOTE 这是注释块，应该被跳过

00:01.000 --> 00:04.500
<c.yellow>第一句字幕</c> & 测试

00:05.000 --> 00:08.250
第二句字幕
"""


class TestTimestamp:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("00:00:01,000", 1.0),
            ("00:01:02.500", 62.5),
            ("01:02.500", 62.5),
            ("1:02:03,250", 3723.25),
            ("00:00:00,000", 0.0),
        ],
    )
    def test_parse_timestamp(self, text, expected):
        assert sources._parse_timestamp(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "abc", "1,2", "::"])
    def test_bad_timestamp_returns_none(self, text):
        assert sources._parse_timestamp(text) is None


class TestCleanText:
    def test_strips_markup_and_entities(self):
        assert sources.clean_text("<i>你好</i> &amp; <c.loud>世界</c>") == "你好 & 世界"

    def test_strips_inline_timestamps(self):
        assert sources.clean_text("<00:00:01.000>甲<00:00:02.000>乙") == "甲乙"

    def test_collapses_whitespace(self):
        assert sources.clean_text("  多   个\t空白  \n") == "多 个 空白"


class TestParseSrt:
    def test_two_cues(self):
        segments = sources.parse_srt(SRT)
        assert [seg.text for seg in segments] == ["第一句字幕 & 测试", "第二句字幕"]
        assert segments[0].start == pytest.approx(1.0)
        assert segments[0].end == pytest.approx(4.5)

    def test_skips_garbage_blocks(self):
        text = "不是字幕\n\n" + SRT
        assert len(sources.parse_srt(text)) == 2

    def test_empty(self):
        assert sources.parse_srt("") == []


class TestParseVtt:
    def test_ignores_header_note_and_strips_tags(self):
        segments = sources.parse_vtt(VTT)
        assert [seg.text for seg in segments] == ["第一句字幕 & 测试", "第二句字幕"]
        assert segments[0].start == pytest.approx(1.0)

    def test_style_and_region_blocks_skipped(self):
        body = VTT.split("\n\n", 2)[2]
        text = "WEBVTT\n\nSTYLE\n::cue { color: red }\n\nREGION\nid:r1\n\n" + body
        assert len(sources.parse_vtt(text)) == 2


class TestParseSubtitleFile:
    def test_srt_file(self, tmp_path):
        path = tmp_path / "a.srt"
        path.write_text(SRT, encoding="utf-8")
        assert len(sources.parse_subtitle_file(path)) == 2

    def test_vtt_file_with_bom(self, tmp_path):
        path = tmp_path / "b.vtt"
        path.write_bytes(b"\xef\xbb\xbf" + VTT.encode("utf-8"))
        assert len(sources.parse_subtitle_file(path)) == 2

    def test_gbk_file(self, tmp_path):
        path = tmp_path / "c.srt"
        path.write_bytes(SRT.replace("\r\n", "\n").encode("gb18030"))
        assert sources.parse_subtitle_file(path)[0].text.startswith("第一句字幕")

    def test_unknown_extension_sniffs_arrow(self, tmp_path):
        path = tmp_path / "d.txt"
        path.write_text(SRT, encoding="utf-8")
        assert len(sources.parse_subtitle_file(path)) == 2

    def test_unknown_extension_without_arrow_raises(self, tmp_path):
        path = tmp_path / "e.txt"
        path.write_text("普通文本，不是字幕", encoding="utf-8")
        with pytest.raises(ValueError):
            sources.parse_subtitle_file(path)


class TestWriteJsonl:
    def test_format(self, tmp_path):
        segments = [
            sources.SubtitleSegment(start=1.0, end=4.5, text="甲"),
            sources.SubtitleSegment(start=5.0, end=8.25, text="乙"),
        ]
        target = tmp_path / "out.jsonl"
        sources.write_jsonl(target, segments)
        lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        assert lines == [
            {"start": 1.0, "end": 4.5, "text": "甲"},
            {"start": 5.0, "end": 8.25, "text": "乙"},
        ]

    def test_creates_parent_directory(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "out.jsonl"
        sources.write_jsonl(target, [sources.SubtitleSegment(0.0, 1.0, "甲")])
        assert target.is_file()


class TestUrlDetection:
    @pytest.mark.parametrize(
        "value",
        [
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "http://127.0.0.1:8000/clip.wav",
            "  https://youtu.be/abc  ",
        ],
    )
    def test_supported(self, value):
        assert sources.is_supported_url(value) is True

    @pytest.mark.parametrize(
        "value",
        ["", "   ", "bilibili.com/video/BV1", "ftp://a/b", "C:\\video.mp4", "https://"],
    )
    def test_not_supported(self, value):
        assert sources.is_supported_url(value) is False


class TestDownloadOptions:
    def test_key_options(self, tmp_path):
        options = sources.build_download_options(tmp_path)
        assert options["format"] == "bestaudio/best"
        assert options["noplaylist"] is True
        assert options["socket_timeout"] == 30
        # 网络抖一下不该让整页失败：重试次数给足，并带指数退避
        assert options["retries"] >= 10
        assert options["fragment_retries"] >= 10
        sleep_func = options["retry_sleep_functions"]["http"]
        # 回归：yt-dlp 是 sleep_func(n=count - 1) 关键字调用，用位置参数测试会漏掉
        assert sleep_func(n=0) == 1.0
        assert sleep_func(n=1) == 2.0
        assert sleep_func(n=4) == 16.0
        assert sleep_func(n=0, extra="ignored") == 1.0
        assert sleep_func(6) == 20.0
        assert all(
            options["retry_sleep_functions"][key] is sleep_func
            for key in ("http", "fragment", "extractor")
        )
        assert "%(id)s" in options["outtmpl"]
        assert options["paths"]["home"] == str(tmp_path)
        assert options["quiet"] is True
        # 不传 progress_hook 时不该凭空生成 hooks
        assert "progress_hooks" not in options

    def test_progress_hook_is_wired(self, tmp_path):
        seen = []
        options = sources.build_download_options(tmp_path, progress_hook=seen.append)
        options["progress_hooks"][0]({"status": "downloading"})
        assert seen == [{"status": "downloading"}]


class TestDownloadErrors:
    def test_missing_ytdlp_reports_install_hint(self, tmp_path, monkeypatch):
        """没有 yt-dlp 时给出安装提示，而不是抛异常。"""
        import sys

        monkeypatch.setitem(sys.modules, "yt_dlp", None)
        result = sources.download_audio("https://example.com/a", tmp_path)
        assert result.ok is False
        assert "yt-dlp" in result.error

    def test_unsupported_url_rejected(self, tmp_path):
        result = sources.download_audio("not a url", tmp_path)
        assert result.ok is False
        assert "链接" in result.error

    def test_cancel_before_start(self, tmp_path):
        import threading

        cancel = threading.Event()
        cancel.set()
        result = sources.download_audio("https://example.com/a.mp3", tmp_path, cancel=cancel)
        assert result.cancelled is True or result.ok is False

class TestFriendlyErrors:
    def test_network_errors_are_explained(self):
        for exc in (
            Exception("ERROR: [download] Got error: [WinError 10061] refused"),
            Exception("The read operation timed out"),
            Exception("Remote end closed connection without response"),
        ):
            text = sources.describe_download_error(exc)
            assert "网络侧问题" in text
            assert "原始错误" in text

    def test_unknown_error_keeps_type_and_message(self):
        assert sources.describe_download_error(ValueError("boom")) == "ValueError: boom"


class TestCookieSources:
    def test_browser_choices_shape(self) -> None:
        assert sources.COOKIE_BROWSERS[0][0] == ""
        assert sources.COOKIE_BROWSER_LABELS[""] == "不使用"
        for key, label in sources.COOKIE_BROWSERS:
            assert sources.COOKIE_BROWSER_LABELS[key] == label

    def test_build_download_options_accepts_browser_cookie(self, tmp_path) -> None:
        options = sources.build_download_options(
            tmp_path, cookies_from_browser="chrome"
        )
        assert options["cookiesfrombrowser"] == ("chrome",)
        assert "cookiefile" not in options

    def test_build_download_options_accepts_cookiefile(self, tmp_path) -> None:
        cookie_file = tmp_path / "cookies.txt"
        options = sources.build_download_options(tmp_path, cookiefile=cookie_file)
        assert options["cookiefile"] == str(cookie_file)
        assert "cookiesfrombrowser" not in options

    def test_no_cookie_options_by_default(self, tmp_path) -> None:
        options = sources.build_download_options(tmp_path)
        assert "cookiesfrombrowser" not in options
        assert "cookiefile" not in options

    def test_browser_cookies_returns_empty_when_unavailable(self, monkeypatch) -> None:
        """浏览器库缺失 / 没登录时不能抛异常，只能返回空并记日志。"""
        monkeypatch.setitem(sys.modules, "yt_dlp", None)
        logs: list[str] = []
        assert sources.browser_cookies("chrome", log=logs.append) == {}
        assert any("登录态" in line for line in logs)

    def test_load_cookie_file_netscape(self, tmp_path) -> None:
        path = tmp_path / "cookies.txt"
        path.write_text(
            "# Netscape HTTP Cookie File\n"
            "# comment line\n"
            ".bilibili.com\tTRUE\t/\tFALSE\t1999999999\tSESSDATA\tabc\n"
            "#HttpOnly_.bilibili.com\tTRUE\t/\tFALSE\t1999999999\tbili_jct\txyz\n"
            "example.com\tFALSE\t/\tFALSE\t1999999999\tfoo\tbar\n",
            encoding="utf-8",
        )
        cookies = sources.load_cookie_file(path)
        assert cookies["SESSDATA"] == "abc"
        assert cookies["bili_jct"] == "xyz"
        assert cookies["foo"] == "bar"

    def test_load_cookie_file_skips_broken_lines(self, tmp_path) -> None:
        path = tmp_path / "cookies.txt"
        path.write_text("too\tfew\tcolumns\n", encoding="utf-8")
        assert sources.load_cookie_file(path) == {}

    def test_load_cookie_file_missing(self, tmp_path) -> None:
        assert sources.load_cookie_file(tmp_path / "nope.txt") == {}


class TestExtractUrl:
    """B 站「复制分享」会把标题和话题一起带过来。"""

    def test_plain_url(self):
        assert sources.extract_url("https://www.bilibili.com/video/BV1xx411c7mD") == (
            "https://www.bilibili.com/video/BV1xx411c7mD"
        )

    def test_share_text_with_title(self):
        text = (
            "【全军配置最豪横的集团军，第82集团军，究竟有多强？ #军事科普】\n"
            "https://www.bilibili.com/video/BV1M6pc65Eox?vd_source=deadbeef"
        )
        assert sources.extract_url(text) == (
            "https://www.bilibili.com/video/BV1M6pc65Eox?vd_source=deadbeef"
        )

    def test_trailing_chinese_punctuation_is_trimmed(self):
        assert sources.extract_url("看看这个 https://b23.tv/abc123。") == "https://b23.tv/abc123"
        assert sources.extract_url("链接：https://b23.tv/abc123，挺好") == "https://b23.tv/abc123"

    def test_first_url_wins(self):
        text = "https://b23.tv/one 和 https://b23.tv/two"
        assert sources.extract_url(text) == "https://b23.tv/one"

    def test_no_url_returns_empty(self):
        assert sources.extract_url("这里没有链接") == ""
        assert sources.extract_url("") == ""
        assert sources.extract_url(None) == ""

    def test_is_supported_url_accepts_share_text(self):
        assert sources.is_supported_url("【标题】 https://www.bilibili.com/video/BV1M6pc65Eox")

    def test_is_supported_url_rejects_schemeless(self):
        assert not sources.is_supported_url("www.bilibili.com/video/BV1M6pc65Eox")
        assert not sources.is_supported_url("BV1M6pc65Eox")
