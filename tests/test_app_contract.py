"""app.py 的结构性冒烟测试：语法、导入、关键方法、常量映射。

不实例化 Tk 窗口（CI 上没有桌面），只验证"界面代码还能被加载且对外契约没变"。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# app.py 只需要标准库 tkinter；某些 CI 镜像的 Python 没编进 tkinter，
# 那种环境整个文件跳过（其余用例仍覆盖 engine / config / registry / downloader）。
pytest.importorskip("tkinter", reason="该环境没有 tkinter，跳过 GUI 结构测试")

import app  # noqa: E402
import sources  # noqa: E402
from engine import Transcriber  # noqa: E402

APP_PATH = Path(app.__file__)


class TestSyntax:
    def test_parses(self) -> None:
        source = APP_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert tree is not None

    def test_entry_point_guarded(self) -> None:
        source = APP_PATH.read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in source


class TestPublicContract:
    @pytest.mark.parametrize(
        "method",
        [
            "_build",
            "_poll",
            "_refresh_models",
            "_on_model_change",
            "_on_locate_model",
            "_on_download_model",
            "_download_worker",
            "_handle_model_progress",
            "_handle_model_done",
            "_resolve_model_paths",
            "_apply_model_state",
            "_save_cfg_from_ui",
            "on_start",
            "on_stop",
            "on_close",
            "_worker",
            "_prepare_engine",
            "_url_worker",
            "_subtitle_worker",
            "_on_source_change",
            "_update_download_progress",
            "_apply_url_widgets",
            "_ensure_transcriber",
            "_transcribe_media",
            "_emit_segments",
            "_cookies_key",
            "_rebuild_txt",
        ],
    )
    def test_methods_exist(self, method: str) -> None:
        assert callable(getattr(app.AsrGui, method, None))

    def test_source_choices(self) -> None:
        """三种输入来源：视频链接 / 本地文件 / 字幕文件。"""
        assert app.SOURCE_CHOICES == [app.SOURCE_URL, app.SOURCE_FILE, app.SOURCE_SUB]
        assert app.SOURCE_CHOICES == ["视频链接", "本地文件", "字幕文件"]
        assert set(app.SOURCE_HINTS) == set(app.SOURCE_CHOICES)
        assert app.SUBTITLE_FILE_TYPES[0][1] == "*.srt *.vtt"

    def test_subtitle_source_bypasses_model(self) -> None:
        """字幕来源不需要模型：_prepare_engine 直接返回空引擎。"""
        gui = app.AsrGui.__new__(app.AsrGui)  # 不建窗口，只调用纯分支
        engine_id, sherpa_dir, whisper_dir, device, threads = gui._prepare_engine(
            app.SOURCE_SUB
        )
        assert (engine_id, sherpa_dir, whisper_dir, device) == ("", "", "", "cpu")
        assert 1 <= threads <= 32

    def test_engine_constants_match(self) -> None:
        assert Transcriber.ENGINE_PARAFORMER == "paraformer-zn"
        assert Transcriber.ENGINE_BASE == "whisper-base"
        assert Transcriber.ENGINE_CHOICES[0] == Transcriber.ENGINE_PARAFORMER

    def test_endpoint_label_maps_are_symmetric(self) -> None:
        assert set(app.ENDPOINT_LABELS) == set(app.ENDPOINT_KEY_TO_LABEL)
        for key, label in app.ENDPOINT_LABELS.items():
            assert app.ENDPOINT_LABEL_TO_KEY[label] == key
            assert app.ENDPOINT_KEY_TO_LABEL[key] == label

    def test_chunk_seconds(self) -> None:
        assert app.CHUNK_SECONDS == 300

    def test_cookie_choices(self) -> None:
        """登录态下拉：空 = 不使用，其余是 yt-dlp 认识的浏览器代号。"""
        keys = [key for key, _label in sources.COOKIE_BROWSERS]
        assert keys[0] == ""
        assert "chrome" in keys
        assert set(keys) == set(sources.COOKIE_BROWSER_LABELS)
        labels = [label for _key, label in sources.COOKIE_BROWSERS]
        assert labels[0] == "不使用"

    def test_cli_flag_parsing(self) -> None:
        parsed = app._parse_transcribe_flags(
            ["--pages", "1,3-5", "--cookies-from-browser", "chrome", "--out", "x.txt"],
            {"output_dir": "out"},
            "https://www.bilibili.com/video/BV1xx411c7mD",
        )
        assert parsed["pages"] == "1,3-5"
        assert parsed["cookies_browser"] == "chrome"
        assert str(parsed["out_txt"]).endswith("x.txt")
        assert parsed["subtitle_first"] is True

    def test_cli_subtitle_first_can_be_disabled(self) -> None:
        parsed = app._parse_transcribe_flags(
            ["--no-subtitle-first"],
            {"output_dir": "out"},
            "https://example.com/a.mp4",
        )
        assert parsed["subtitle_first"] is False
        assert parsed["pages"] == ""

    def test_default_output_name(self) -> None:
        url = app._parse_transcribe_flags(
            [], {"output_dir": "out"}, "https://example.com/a.mp4"
        )
        assert str(url["out_txt"]).replace("\\", "/").endswith("out/transcript.txt")
        local = app._parse_transcribe_flags([], {"output_dir": "out"}, "D:/media/clip.mp4")
        assert str(local["out_txt"]).replace("\\", "/").endswith("out/clip.txt")

    def test_jsonl_triples_reader(self, tmp_path) -> None:
        path = tmp_path / "a.jsonl"
        path.write_text(
            '{"start": 2.0, "end": 3.0, "text": "b"}\n'
            "not json\n"
            "\n"
            '{"start": 1.0, "end": 1.5, "text": "a"}\n',
            encoding="utf-8",
        )
        rows = app._read_jsonl_triples(path)
        assert [row[2] for row in rows] == ["a", "b"]


class TestNoHardcodedMachinePaths:
    def test_sources_do_not_contain_local_absolute_paths(self) -> None:
        """回归：源码里不允许出现本机绝对路径（开源第一原则）。"""
        offenders = []
        for path in sorted(APP_PATH.parent.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for marker in ("E:\\\\AI_dev", "C:\\\\Users\\\\", "F:\\\\python"):
                if marker in text:
                    offenders.append(f"{path.name}: {marker}")
        assert offenders == []

    def test_bat_files_are_ascii_crlf(self) -> None:
        """回归：.bat 必须是纯 ASCII + CRLF，否则 cmd 解析会崩（见 lessons 第 7 节）。"""
        repo = APP_PATH.parent.parent
        bats = sorted(repo.glob("*.bat")) + sorted(APP_PATH.parent.glob("*.bat"))
        bats += sorted((repo / "packaging").glob("*.bat"))
        assert bats, "没有找到任何 .bat 文件"
        for bat in bats:
            raw = bat.read_bytes()
            assert b"\r\n" in raw, f"{bat.name} 不是 CRLF 换行"
            assert raw.count(b"\r") == raw.count(b"\n"), f"{bat.name} 混用换行符"
            non_ascii = [b for b in raw if b > 0x7F]
            assert non_ascii == [], f"{bat.name} 含非 ASCII 字节"

    def test_all_python_sources_avoid_machine_paths(self) -> None:
        """同一个约束也要覆盖 packaging/ 等新加的脚本目录。"""
        repo = APP_PATH.parent.parent
        offenders = []
        for folder in (APP_PATH.parent, repo / "packaging", repo / "tests"):
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.py")):
                text = path.read_text(encoding="utf-8")
                for marker in ("E:\\\\AI_dev", "C:\\\\Users\\\\", "F:\\\\python"):
                    if marker in text:
                        offenders.append(f"{path.name}: {marker}")
        assert offenders == []
