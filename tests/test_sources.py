"""字幕解析 / 链接判定 / 下载参数 的单元测试（不联网、不加载模型）。"""

import json

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
        assert options["retries"] == 3
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
