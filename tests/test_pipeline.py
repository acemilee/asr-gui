"""pipeline.py 的离线测试：用假 transcribe / emit_segments 验证编排逻辑。"""

from __future__ import annotations

import threading
from pathlib import Path

import pipeline
import pytest
from bili import BiliError, BiliPage, BiliSubtitle, BiliVideo
from sources import DownloadResult, SubtitleSegment

URL = "https://www.bilibili.com/video/BV1xx411c7mD"

VIDEO = BiliVideo(
    bvid="BV1xx411c7mD",
    aid=123,
    title="测试视频",
    pages=(
        BiliPage(page=1, cid=111, part="第一集", duration=60.0),
        BiliPage(page=2, cid=222, part="第二集", duration=90.0),
    ),
)


class Recorder:
    """收集 pipeline 的回调，替身转发给 GUI/CLI。"""

    def __init__(self):
        self.logs: list[str] = []
        self.transcribed: list[tuple[str, str, float]] = []
        self.emitted: list[tuple[float, str]] = []
        self.progress: list[tuple[float, float, str]] = []
        self.downloads: list[tuple[int, int]] = []

    def log(self, message: str) -> None:
        self.logs.append(message)

    def transcribe(self, media: Path, jsonl_path: Path, offset: float) -> bool:
        self.transcribed.append((str(media), str(jsonl_path), offset))
        return True

    def emit_segments(self, segments, offset: float) -> None:
        for segment in segments:
            self.emitted.append((offset + segment.start, segment.text))

    def progress(self, done: float, total: float, label: str) -> None:
        self.progress.append((done, total, label))

    def download_progress(self, done: int, total: int, label: str) -> None:
        self.downloads.append((done, total))


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def stub_bili(monkeypatch):
    """把 bili 的四个网络入口替换成可编排的假实现。"""

    class Stub:
        video = VIDEO
        subtitle = None
        subtitle_segments: list[SubtitleSegment] = []
        subtitle_error: Exception | None = None
        video_error: Exception | None = None

    def fetch_video(url, cookies=None, timeout=20.0):
        if Stub.video_error:
            raise Stub.video_error
        return Stub.video

    def find_subtitle(bvid, aid, cid, cookies=None, timeout=20.0):
        return Stub.subtitle

    def fetch_subtitle_segments(url, cookies=None, timeout=30.0):
        if Stub.subtitle_error:
            raise Stub.subtitle_error
        return list(Stub.subtitle_segments)

    monkeypatch.setattr(pipeline.bili, "fetch_video", fetch_video)
    monkeypatch.setattr(pipeline.bili, "find_subtitle", find_subtitle)
    monkeypatch.setattr(pipeline.bili, "fetch_subtitle_segments", fetch_subtitle_segments)
    return Stub


@pytest.fixture
def stub_download(monkeypatch):
    class Stub:
        result = DownloadResult(ok=True, path=Path("audio.m4a"), title="音频")
        calls: list[str] = []

    def download_audio(url, target_dir, progress_cb=None, cancel=None, log=None, **_kwargs):
        Stub.calls.append(url)
        return Stub.result

    monkeypatch.setattr(pipeline.sources, "download_audio", download_audio)
    return Stub


SUBTITLE = BiliSubtitle(language="ai-zh", url="https://example.com/ai-zh.json")


class TestSingleSite:
    def test_download_then_transcribe(self, rec, stub_download, tmp_path):
        result = pipeline.run_url_pipeline(
            "https://example.com/video",
            transcript_dir=tmp_path,
            stem="clip",
            downloads_dir=tmp_path,
            transcribe=rec.transcribe,
            emit_segments=rec.emit_segments,
            log=rec.log,
        )
        assert result.ok is True
        assert result.pages_done == 1
        assert result.subtitle_pages == 0
        assert stub_download.calls == ["https://example.com/video"]
        assert rec.transcribed == [
            ("audio.m4a", str(tmp_path / "clip.jsonl"), 0.0)
        ]

    def test_download_failure(self, rec, stub_download, tmp_path):
        stub_download.result = DownloadResult(ok=False, path=None, error="网络断了")
        result = pipeline.run_url_pipeline(
            "https://example.com/video",
            transcript_dir=tmp_path,
            stem="clip",
            downloads_dir=tmp_path,
            transcribe=rec.transcribe,
            emit_segments=rec.emit_segments,
            log=rec.log,
        )
        assert result.ok is False
        assert "网络断了" in result.error
        assert rec.transcribed == []

    def test_rejects_non_http(self, rec, tmp_path):
        result = pipeline.run_url_pipeline(
            "ftp://example.com/video",
            transcript_dir=tmp_path,
            stem="clip",
            downloads_dir=tmp_path,
            transcribe=rec.transcribe,
            emit_segments=rec.emit_segments,
            log=rec.log,
        )
        assert result.ok is False
        assert "http" in result.error


class TestBilibili:
    def test_subtitle_pages_skip_download(self, rec, stub_bili, stub_download, tmp_path):
        stub_bili.video = BiliVideo(
            bvid="BV1", aid=1, title="只有一集",
            pages=(BiliPage(page=1, cid=111, part="第一集", duration=60.0),),
        )
        stub_bili.subtitle = SUBTITLE
        stub_bili.subtitle_segments = [
            SubtitleSegment(start=1.0, end=2.0, text="字幕一"),
            SubtitleSegment(start=3.0, end=4.0, text="字幕二"),
        ]
        result = pipeline.run_url_pipeline(
            URL,
            transcript_dir=tmp_path,
            stem="clip",
            downloads_dir=tmp_path,
            transcribe=rec.transcribe,
            emit_segments=rec.emit_segments,
            log=rec.log,
        )
        assert result.ok is True
        assert result.subtitle_pages == 1
        assert result.pages_done == 1
        assert stub_download.calls == []
        assert rec.emitted == [(1.0, "字幕一"), (3.0, "字幕二")]
        assert rec.transcribed == []
        assert any("使用 B 站字幕" in line for line in rec.logs)

    def test_no_subtitle_falls_back_to_speech(self, rec, stub_bili, stub_download, tmp_path):
        stub_bili.video = VIDEO
        stub_bili.subtitle = None
        result = pipeline.run_url_pipeline(
            URL,
            transcript_dir=tmp_path,
            stem="clip",
            downloads_dir=tmp_path,
            transcribe=rec.transcribe,
            emit_segments=rec.emit_segments,
            log=rec.log,
        )
        assert result.ok is True
        assert result.subtitle_pages == 0
        assert result.pages_done == 2
        # 两个分 P 各下载一次，且第二 P 改写了 ?p=2
        assert stub_download.calls == [f"{URL}?p=1", f"{URL}?p=2"]
        # 第二个分 P 的偏移 = 第一个分 P 的时长
        assert [offset for _media, _jsonl, offset in rec.transcribed] == [0.0, 60.0]
        assert rec.transcribed[1][1] == str(tmp_path / "clip.p2.jsonl")
        assert result.page_offsets == [(1, 0.0), (2, 60.0)]
        assert any("没有可用的 B 站字幕轨" in line for line in rec.logs)

    def test_subtitle_error_falls_back(self, rec, stub_bili, stub_download, tmp_path):
        stub_bili.video = VIDEO
        stub_bili.subtitle = SUBTITLE
        stub_bili.subtitle_error = BiliError("字幕 404")
        result = pipeline.run_url_pipeline(
            URL,
            transcript_dir=tmp_path,
            stem="clip",
            downloads_dir=tmp_path,
            transcribe=rec.transcribe,
            emit_segments=rec.emit_segments,
            log=rec.log,
        )
        assert result.ok is True
        assert result.subtitle_pages == 0
        assert len(rec.transcribed) == 2
        assert any("字幕轨查询失败" in line or "改走语音转写" in line for line in rec.logs)

    def test_pages_spec_limits_scope(self, rec, stub_bili, stub_download, tmp_path):
        stub_bili.video = VIDEO
        stub_bili.subtitle = None
        result = pipeline.run_url_pipeline(
            URL,
            transcript_dir=tmp_path,
            stem="clip",
            downloads_dir=tmp_path,
            transcribe=rec.transcribe,
            emit_segments=rec.emit_segments,
            pages_spec="2",
            log=rec.log,
        )
        assert result.ok is True
        assert result.pages_done == 1
        assert stub_download.calls == [f"{URL}?p=2"]
        assert [offset for _m, _j, offset in rec.transcribed] == [0.0]

    def test_bad_pages_spec_fails(self, rec, stub_bili, stub_download, tmp_path):
        stub_bili.video = VIDEO
        result = pipeline.run_url_pipeline(
            URL,
            transcript_dir=tmp_path,
            stem="clip",
            downloads_dir=tmp_path,
            transcribe=rec.transcribe,
            emit_segments=rec.emit_segments,
            pages_spec="9",
            log=rec.log,
        )
        assert result.ok is False
        assert "分 P" in result.error

    def test_video_error_fails(self, rec, stub_bili, stub_download, tmp_path):
        stub_bili.video_error = BiliError("接口挂了")
        result = pipeline.run_url_pipeline(
            URL,
            transcript_dir=tmp_path,
            stem="clip",
            downloads_dir=tmp_path,
            transcribe=rec.transcribe,
            emit_segments=rec.emit_segments,
            log=rec.log,
        )
        assert result.ok is False
        assert "接口挂了" in result.error

    def test_transcribe_failure_aborts(self, rec, stub_bili, stub_download, tmp_path):
        stub_bili.video = VIDEO
        stub_bili.subtitle = None

        def failing_transcribe(media, jsonl_path, offset):
            return False

        result = pipeline.run_url_pipeline(
            URL,
            transcript_dir=tmp_path,
            stem="clip",
            downloads_dir=tmp_path,
            transcribe=failing_transcribe,
            emit_segments=rec.emit_segments,
            log=rec.log,
        )
        assert result.ok is False
        assert stub_download.calls == [f"{URL}?p=1"]  # 第一个分 P 失败后不再继续

    def test_cancel_stops_early(self, rec, stub_bili, stub_download, tmp_path):
        stub_bili.video = VIDEO
        stub_bili.subtitle = None
        cancel = threading.Event()
        cancel.set()
        result = pipeline.run_url_pipeline(
            URL,
            transcript_dir=tmp_path,
            stem="clip",
            downloads_dir=tmp_path,
            transcribe=rec.transcribe,
            emit_segments=rec.emit_segments,
            log=rec.log,
            cancel=cancel,
        )
        assert result.cancelled is True
        assert result.ok is False
        assert stub_download.calls == []


class TestFormatDuration:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "00:00"),
            (59, "00:59"),
            (60, "01:00"),
            (3599, "59:59"),
            (3600, "1:00:00"),
            (44068, "12:14:28"),
        ],
    )
    def test_formats(self, seconds, expected):
        assert pipeline.format_duration(seconds) == expected

    def test_negative(self):
        assert pipeline.format_duration(-5) == "00:00"
