"""engine.py 的纯逻辑部分：时间戳、时长探测、分块边界、续跑点解析。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from engine import SAMPLE_RATE, Segment, Transcriber, format_timestamp


def _transcriber(tmp_path: Path) -> Transcriber:
    """只需要解码能力，不加载任何模型。"""
    return Transcriber(
        Transcriber.ENGINE_PARAFORMER,
        sherpa_dir=str(tmp_path / "sherpa"),
        device="cpu",
        num_threads=2,
    )


class TestFormatTimestamp:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "00:00:00"),
            (0.9, "00:00:00"),
            (59.9, "00:00:59"),
            (60, "00:01:00"),
            (3661, "01:01:01"),
            (35987.4, "09:59:47"),
            (360000, "100:00:00"),
        ],
    )
    def test_values(self, seconds: float, expected: str) -> None:
        assert format_timestamp(seconds) == expected

    def test_negative_is_clamped(self) -> None:
        assert format_timestamp(-5.0) == "00:00:00"


class TestSegment:
    def test_fields(self) -> None:
        seg = Segment(start=1.5, end=2.5, text="你好")
        assert (seg.start, seg.end, seg.text) == (1.5, 2.5, "你好")


class TestProbeDuration:
    def test_reads_wav_duration(self, tone_wav: Path) -> None:
        assert Transcriber.probe_duration(str(tone_wav)) == pytest.approx(3.0, abs=0.1)

    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        assert Transcriber.probe_duration(str(tmp_path / "nope.wav")) == 0.0


class TestIterChunks:
    def test_splits_into_fixed_size_chunks(self, tone_wav: Path, tmp_path: Path) -> None:
        chunks = list(_transcriber(tmp_path)._iter_chunks(str(tone_wav), 1, 0.0))
        assert len(chunks) == 3
        for samples, _start in chunks:
            assert samples.dtype == np.float32
            assert samples.size == SAMPLE_RATE
        starts = [round(start, 3) for _samples, start in chunks]
        assert starts == [0.0, 1.0, 2.0]

    def test_tail_chunk_is_not_padded(self, tone_wav: Path, tmp_path: Path) -> None:
        chunks = list(_transcriber(tmp_path)._iter_chunks(str(tone_wav), 2, 0.0))
        assert len(chunks) == 2
        assert chunks[0][0].size == 2 * SAMPLE_RATE
        assert chunks[1][0].size == SAMPLE_RATE  # 3 s 文件，最后一块只有 1 s

    def test_start_sec_seeks_to_requested_position(
        self, tone_wav: Path, tmp_path: Path
    ) -> None:
        chunks = list(_transcriber(tmp_path)._iter_chunks(str(tone_wav), 10, 1.0))
        assert chunks, "seek 到 1.0 s 后仍应有数据"
        _samples, start = chunks[0]
        assert start == pytest.approx(1.0, abs=0.15)


class TestReadProgress:
    def test_returns_max_end(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        rows = [
            {"start": 0.0, "end": 12.5, "text": "a"},
            {"start": 12.5, "end": 30.0, "text": "b"},
            {"start": 30.0, "end": 25.0, "text": "c"},  # 乱序/异常数据不应回退
        ]
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        assert Transcriber._read_progress(str(path)) == pytest.approx(30.0)

    def test_ignores_blank_and_broken_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        path.write_text(
            '{"start": 0, "end": 5}\n\n不是 JSON\n{"start": 5, "end": 9.5}\n',
            encoding="utf-8",
        )
        assert Transcriber._read_progress(str(path)) == pytest.approx(9.5)

    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        assert Transcriber._read_progress(str(tmp_path / "absent.jsonl")) == 0.0
