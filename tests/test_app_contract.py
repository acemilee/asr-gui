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
        ],
    )
    def test_methods_exist(self, method: str) -> None:
        assert callable(getattr(app.AsrGui, method, None))

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
