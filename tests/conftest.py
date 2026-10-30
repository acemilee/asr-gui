"""pytest 共享装置：把 ``asr_gui`` 加进 sys.path，并提供合成音频。

测试只依赖标准库 + numpy + PyAV（解码用），**不下载任何模型**、不联网、
不需要 GPU；模型注册表相关的用例用临时目录 + 稀疏文件伪造缓存布局。
"""

from __future__ import annotations

import math
import struct
import sys
import wave
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ASR_GUI_DIR = REPO_ROOT / "asr_gui"
if str(ASR_GUI_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_GUI_DIR))


def _write_tone(path: Path, seconds: float, rate: int = 16000) -> Path:
    """写一个 16 kHz 单声道 16-bit 正弦波 WAV。"""
    frames = int(seconds * rate)
    payload = bytearray()
    for index in range(frames):
        value = int(8000 * math.sin(2 * math.pi * 440 * index / rate))
        payload += struct.pack("<h", value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(payload))
    return path


@pytest.fixture(scope="session")
def tone_wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """3 秒 16 kHz 单声道音频，用于解码 / 时长 / 分块测试。"""
    target = tmp_path_factory.mktemp("media") / "tone_3s.wav"
    return _write_tone(target, 3.0)


@pytest.fixture
def sparse_file():
    """生成一个指定大小的稀疏文件（用于伪造模型缓存）。"""

    def _make(path: Path, size: int) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.truncate(size)
        return path

    return _make
