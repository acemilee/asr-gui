"""本地长音频转写引擎。

两个引擎，都直接读本机已缓存的模型文件，全程离线：

* ``paraformer-zn``  —— sherpa-onnx + FunASR paraformer-zh int8 ONNX
  （就是 VideoToNo 界面里"paraformer-zn（中文推荐·最快最准，只支持中文）"那套）
  模型目录要求::

      <sherpa_dir>/asr/model.int8.onnx
      <sherpa_dir>/asr/tokens.txt
      <sherpa_dir>/punc/model.onnx        (可选，缺了就没标点)
      <sherpa_dir>/vad/silero_vad.onnx

* ``whisper-base`` / ``whisper-large-v3-turbo`` —— faster-whisper(CT2)
  模型目录要求是 CT2 snapshot 目录（含 model.bin / config.json / tokenizer.json）。

设计要点（针对 9~10 小时长音频）：

* **流式分块解码**：用 PyAV 逐块读音频，块内 16k/mono/float32，绝不把整段解码进内存
  （10 小时整段 float32 约 2.07 GB）。
* **增量落盘**：每识别出一段就立刻追加写 JSONL，崩了/断了只丢最后一块。
* **断点续跑**：启动时读已有 JSONL，跳过已完成的时间区间。
* **可取消**：每块之间检查 cancel 事件。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

SAMPLE_RATE = 16000
VAD_WINDOW = 512
MAX_PIECE_SECONDS = 25


@dataclass
class Segment:
    """一段识别结果，时间为相对媒体起点的秒数。"""

    start: float
    end: float
    text: str


class CancelledError(Exception):
    """用户主动取消。"""


# progress_cb(done_seconds, total_seconds, elapsed_seconds)
ProgressCb = Callable[[float, float, float], None]
# segment_cb(segment)
SegmentCb = Callable[[Segment], None]


class Transcriber:
    ENGINE_PARAFORMER = "paraformer-zn"
    ENGINE_BASE = "whisper-base"
    ENGINE_TURBO = "whisper-large-v3-turbo"
    ENGINE_CHOICES = (ENGINE_PARAFORMER, ENGINE_BASE, ENGINE_TURBO)

    def __init__(
        self,
        engine: str,
        sherpa_dir: str,
        whisper_dir: str = "",
        device: str = "cpu",
        num_threads: int = 8,
    ) -> None:
        self.engine = engine
        self.sherpa_dir = Path(sherpa_dir)
        self.whisper_dir = Path(whisper_dir) if whisper_dir else Path()
        self.device = device
        self.num_threads = max(1, int(num_threads))
        self._recognizer = None
        self._vad_config = None
        self._punct = None
        self._whisper = None

    # ------------------------------------------------------------------ 加载

    def load(self) -> None:
        """加载模型。失败直接抛异常，由调用方展示给用户。"""
        if self.engine == self.ENGINE_PARAFORMER:
            self._load_paraformer()
        else:
            self._load_whisper()

    def _load_paraformer(self) -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise RuntimeError(
                "未安装 sherpa-onnx，无法使用 paraformer-zh 档位。请先安装依赖：\n"
                "  python -m pip install -r requirements.txt\n"
                "（Windows 下也可直接双击仓库根目录的 setup.bat）"
            ) from exc

        asr_dir = self.sherpa_dir / "asr"
        vad_dir = self.sherpa_dir / "vad"
        asr_model = asr_dir / "model.int8.onnx"
        asr_tokens = asr_dir / "tokens.txt"
        vad_model = vad_dir / "silero_vad.onnx"
        missing = [str(p) for p in (asr_model, asr_tokens, vad_model) if not p.is_file()]
        if missing:
            raise RuntimeError("模型文件缺失：\n" + "\n".join(missing))

        self._recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(asr_model),
            tokens=str(asr_tokens),
            num_threads=self.num_threads,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
        )

        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = str(vad_model)
        vad_config.silero_vad.threshold = 0.5
        vad_config.silero_vad.min_silence_duration = 0.5
        vad_config.silero_vad.min_speech_duration = 0.25
        vad_config.silero_vad.max_speech_duration = 10.0
        vad_config.silero_vad.window_size = VAD_WINDOW
        vad_config.sample_rate = SAMPLE_RATE
        self._vad_config = vad_config

        # 标点模型可选：缺失只降级为"无标点"，不致命。
        punc_model = self.sherpa_dir / "punc" / "model.onnx"
        if punc_model.is_file():
            self._punct = sherpa_onnx.OfflinePunctuation(
                sherpa_onnx.OfflinePunctuationConfig(
                    model=sherpa_onnx.OfflinePunctuationModelConfig(
                        ct_transformer=str(punc_model),
                        num_threads=2,
                        provider="cpu",
                        debug=False,
                    )
                )
            )
        else:
            self._punct = None

    def _load_whisper(self) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise RuntimeError("未安装 faster-whisper，请用便携版 venv 的 python 运行。") from exc

        model_dir = self.whisper_dir
        if not model_dir.is_dir():
            raise RuntimeError(f"模型目录不存在：{model_dir}")
        if not (model_dir / "model.bin").is_file():
            raise RuntimeError(f"模型目录里没有 model.bin：{model_dir}")

        compute_type = "float16" if self.device == "cuda" else "int8"
        self._whisper = WhisperModel(
            str(model_dir), device=self.device, compute_type=compute_type
        )

    # ------------------------------------------------------------------ 解码

    @staticmethod
    def probe_duration(media_path: str) -> float:
        """媒体时长（秒）；失败返回 0.0。"""
        try:
            import av

            with av.open(media_path) as container:
                if container.duration:
                    return float(container.duration) / 1_000_000.0
                stream = container.streams.audio[0]
                if stream.duration and stream.time_base:
                    return float(stream.duration * stream.time_base)
        except Exception:
            pass
        return 0.0

    def _iter_chunks(
        self, media_path: str, chunk_seconds: int, start_sec: float
    ) -> tuple[np.ndarray, float]:
        """产出 (chunk_samples_float32, chunk_start_seconds)。

        chunk_start 是该块首样本对应的媒体时间轴秒数（考虑 resume 时的 seek 偏移）。
        """
        import av

        chunk_samples = int(chunk_seconds * SAMPLE_RATE)
        container = av.open(media_path)
        try:
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

            base_time = start_sec
            first_frame = True
            if start_sec > 0:
                # 注意：PyAV 指定 stream 时 offset 的单位是该流的 time_base，
                # 不是 AV_TIME_BASE（用错会直接 seek 到文件末尾）。
                tb = stream.time_base
                if tb:
                    offset = int(start_sec / float(tb))
                else:
                    offset = int(start_sec * 1_000_000)
                container.seek(offset, stream=stream, backward=True)

            # 预分配块缓冲：逐帧写入而不是 np.concatenate，
            # 否则 300 秒一块会退化成 O(n^2) 拷贝（实测解码 RTF 0.22）。
            out = np.empty(chunk_samples, dtype=np.float32)
            filled = 0
            consumed = 0  # 相对 base_time 的采样数

            def emit(frame):
                nonlocal out, filled, consumed
                array = frame.to_ndarray()
                if array.ndim > 1:
                    array = array[0]
                samples = array.astype(np.float32) / 32768.0
                size = samples.size
                pos = 0
                while pos < size:
                    space = chunk_samples - filled
                    take = min(space, size - pos)
                    out[filled : filled + take] = samples[pos : pos + take]
                    filled += take
                    pos += take
                    if filled >= chunk_samples:
                        yield out, base_time + consumed / SAMPLE_RATE
                        consumed += chunk_samples
                        out = np.empty(chunk_samples, dtype=np.float32)
                        filled = 0

            for frame in container.decode(stream):
                if first_frame:
                    first_frame = False
                    if start_sec > 0 and frame.time is not None:
                        base_time = float(frame.time)
                frames = resampler.resample(frame)
                if not isinstance(frames, list):
                    frames = [frames]
                for resampled in frames:
                    for piece, chunk_start in emit(resampled):
                        yield piece, chunk_start

            # 冲洗重采样器里剩下的样本
            tail = resampler.resample(None)
            if tail:
                if not isinstance(tail, list):
                    tail = [tail]
                for resampled in tail:
                    for piece, chunk_start in emit(resampled):
                        yield piece, chunk_start
            if filled:
                yield out[:filled], base_time + consumed / SAMPLE_RATE
        finally:
            container.close()

    # ------------------------------------------------------------------ 转写

    def run(
        self,
        media_path: str,
        output_jsonl: str = "",
        chunk_seconds: int = 300,
        progress_cb: Optional[ProgressCb] = None,
        segment_cb: Optional[SegmentCb] = None,
        cancel: Optional[threading.Event] = None,
    ) -> list[Segment]:
        """流式转写整个媒体文件。

        * ``output_jsonl`` 非空时，边转写边追加写 JSONL（每行一个 segment），
          并在启动时读取该文件跳过已完成区间（断点续跑）。
        * ``progress_cb(done_sec, total_sec, elapsed_sec)``
        * ``segment_cb(segment)``
        """
        if self._recognizer is None and self._whisper is None:
            self.load()

        total_sec = self.probe_duration(media_path)
        resume_sec = 0.0
        segments: list[Segment] = []
        sink = None
        if output_jsonl:
            resume_sec = self._read_progress(output_jsonl)
            sink = open(output_jsonl, "a", encoding="utf-8")
        started = time.monotonic()

        try:
            for chunk, chunk_start in self._iter_chunks(
                media_path, chunk_seconds, resume_sec
            ):
                if cancel is not None and cancel.is_set():
                    raise CancelledError()
                if self.engine == self.ENGINE_PARAFORMER:
                    produced = self._transcribe_paraformer(chunk, chunk_start, cancel)
                else:
                    produced = self._transcribe_whisper(chunk, chunk_start)
                for seg in produced:
                    segments.append(seg)
                    if sink is not None:
                        sink.write(
                            json.dumps(asdict(seg), ensure_ascii=False) + "\n"
                        )
                        sink.flush()
                    if segment_cb is not None:
                        segment_cb(seg)
                done = chunk_start + len(chunk) / SAMPLE_RATE
                if progress_cb is not None:
                    progress_cb(done, total_sec, time.monotonic() - started)
        finally:
            if sink is not None:
                sink.close()
        if progress_cb is not None:
            progress_cb(total_sec, total_sec, time.monotonic() - started)
        return segments

    @staticmethod
    def _read_progress(path: str) -> float:
        """从已有 JSONL 读出已完成的最后时间点（秒），用于续跑。"""
        last_end = 0.0
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    end = float(item.get("end", 0.0))
                    if end > last_end:
                        last_end = end
        except OSError:
            return 0.0
        return last_end

    # ---- paraformer ----

    def _transcribe_paraformer(
        self, chunk: np.ndarray, chunk_start: float, cancel: Optional[threading.Event]
    ) -> list[Segment]:
        import sherpa_onnx

        detector = sherpa_onnx.VoiceActivityDetector(
            self._vad_config, buffer_size_in_seconds=120
        )
        pieces: list[tuple[int, np.ndarray]] = []
        for offset in range(0, len(chunk), VAD_WINDOW):
            if cancel is not None and cancel.is_set():
                raise CancelledError()
            detector.accept_waveform(chunk[offset : offset + VAD_WINDOW])
            while not detector.empty():
                segment = detector.front
                pieces.append((int(segment.start), np.asarray(segment.samples)))
                detector.pop()
        if detector.is_speech_detected():
            silence = np.zeros(VAD_WINDOW, dtype=np.float32)
            for _ in range(64):
                detector.accept_waveform(silence)
                if not detector.is_speech_detected():
                    break
            while not detector.empty():
                segment = detector.front
                pieces.append((int(segment.start), np.asarray(segment.samples)))
                detector.pop()

        max_samples = MAX_PIECE_SECONDS * SAMPLE_RATE
        results: list[Segment] = []
        for start, samples in pieces:
            if cancel is not None and cancel.is_set():
                raise CancelledError()
            if len(samples) <= max_samples:
                sub_pieces = [(start, samples)]
            else:
                sub_pieces = [
                    (start + offset, samples[offset : offset + max_samples])
                    for offset in range(0, len(samples), max_samples)
                    if len(samples[offset : offset + max_samples]) >= SAMPLE_RATE // 2
                ]
            for piece_start, piece in sub_pieces:
                stream = self._recognizer.create_stream()
                stream.accept_waveform(SAMPLE_RATE, piece)
                self._recognizer.decode_stream(stream)
                text = stream.result.text.strip()
                if not text:
                    continue
                if self._punct is not None:
                    text = self._punct.add_punctuation(text).strip()
                if not text:
                    continue
                results.append(
                    Segment(
                        start=round(chunk_start + piece_start / SAMPLE_RATE, 2),
                        end=round(chunk_start + (piece_start + len(piece)) / SAMPLE_RATE, 2),
                        text=text,
                    )
                )
        return results

    # ---- faster-whisper ----

    def _transcribe_whisper(self, chunk: np.ndarray, chunk_start: float) -> list[Segment]:
        produced, _info = self._whisper.transcribe(
            chunk,
            language="zh",
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        results: list[Segment] = []
        for seg in produced:
            text = (seg.text or "").strip()
            if not text:
                continue
            results.append(
                Segment(
                    start=round(chunk_start + float(seg.start), 2),
                    end=round(chunk_start + float(seg.end), 2),
                    text=text,
                )
            )
        return results


def format_timestamp(seconds: float) -> str:
    """秒 -> HH:MM:SS。"""
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
