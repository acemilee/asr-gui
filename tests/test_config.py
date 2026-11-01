"""config.py：配置文件读写、缓存目录探测、手工导入记录。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import config as app_config
import pytest


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把配置目录指到临时目录，避免污染真实用户配置。"""
    target = tmp_path / "cfg"
    monkeypatch.setenv(app_config.CONFIG_DIR_ENV, str(target))
    return target


class TestPaths:
    def test_config_path_honours_env(self, config_home: Path) -> None:
        assert app_config.config_dir() == config_home
        assert app_config.config_path() == config_home / app_config.CONFIG_FILENAME

    def test_default_output_dir_is_repo_out(self) -> None:
        assert app_config.default_output_dir() == app_config.repo_root() / "out"

    def test_not_frozen_in_tests(self) -> None:
        assert app_config.is_frozen() is False
        assert app_config.app_home() == app_config.repo_root()
        assert app_config.logs_dir() == app_config.repo_root() / "logs"

    def test_frozen_uses_exe_folder_and_user_dirs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_home: Path
    ) -> None:
        """打包成 exe 后：模型目录从 exe 旁边找，输出/crash 日志落到用户目录。"""
        exe_dir = tmp_path / "downloaded" / "asr-gui"
        exe_dir.mkdir(parents=True)
        monkeypatch.setattr(app_config.sys, "frozen", True, raising=False)
        monkeypatch.setattr(app_config.sys, "executable", str(exe_dir / "ASR-GUI.exe"))

        assert app_config.is_frozen() is True
        assert app_config.app_home() == exe_dir
        assert app_config.default_output_dir() == Path.home() / "asr-gui-output"
        assert app_config.logs_dir() == config_home / "logs"
        # 缓存候选目录里不能再有 _MEI 临时解包目录
        assert str(app_config.default_output_dir()).find("_MEI") == -1

    def test_version_is_exposed(self) -> None:
        assert app_config.APP_VERSION.count(".") == 2


class TestDiscoverCacheDir:
    def test_env_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "models-cache"
        monkeypatch.setenv("ASR_GUI_MODEL_DIR", str(target))
        assert app_config.discover_cache_dir() == target

    def test_falls_back_to_user_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ASR_GUI_MODEL_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        found = app_config.discover_cache_dir()
        assert found.name == "models"
        assert "asr-gui" in str(found)


class TestDefaultsAndRoundTrip:
    def test_defaults_have_all_keys(self, config_home: Path) -> None:
        data = app_config.defaults()
        assert set(data) == {
            "model_id",
            "device",
            "num_threads",
            "endpoint",
            "model_cache_dir",
            "output_dir",
            "last_media",
            "output_name",
            "source",
            "pages",
            "cookies_from_browser",
            "manual_models",
        }
        assert data["endpoint"] == "mirror"
        assert data["manual_models"] == {}
        assert data["source"] == "本地文件"

    def test_save_then_load(self, config_home: Path) -> None:
        data = app_config.defaults()
        data["model_id"] = "small"
        data["num_threads"] = 12
        data["source"] = "视频链接"
        data["pages"] = "1,3-5"
        data["cookies_from_browser"] = "chrome"
        data["manual_models"] = {"small": str(config_home / "imported")}
        ok, message = app_config.save(data)
        assert ok is True and message == ""
        assert app_config.config_path().is_file()

        loaded = app_config.load()
        assert loaded["model_id"] == "small"
        assert loaded["num_threads"] == 12
        # 回归：这三项曾因不在 defaults() 里被 load() 静默丢弃
        assert loaded["source"] == "视频链接"
        assert loaded["pages"] == "1,3-5"
        assert loaded["cookies_from_browser"] == "chrome"
        assert loaded["manual_models"] == {"small": str(config_home / "imported")}

    def test_unknown_keys_are_dropped(self, config_home: Path) -> None:
        config_home.mkdir(parents=True, exist_ok=True)
        app_config.config_path().write_text(
            json.dumps({"model_id": "tiny", "bogus": 1}), encoding="utf-8"
        )
        loaded = app_config.load()
        assert loaded["model_id"] == "tiny"
        assert "bogus" not in loaded

    def test_corrupt_json_falls_back_to_defaults(self, config_home: Path) -> None:
        config_home.mkdir(parents=True, exist_ok=True)
        app_config.config_path().write_text("{ not json", encoding="utf-8")
        loaded = app_config.load()
        assert loaded["model_id"] == app_config.defaults()["model_id"]

    def test_manual_models_of_wrong_type_is_reset(self, config_home: Path) -> None:
        config_home.mkdir(parents=True, exist_ok=True)
        app_config.config_path().write_text(
            json.dumps({"manual_models": ["oops"]}), encoding="utf-8"
        )
        assert app_config.load()["manual_models"] == {}


class TestClearManualEntry:
    def test_removes_only_target(self) -> None:
        data = {"manual_models": {"base": "d:/a", "small": "d:/b"}}
        app_config.clear_manual_entry(data, "base")
        assert data["manual_models"] == {"small": "d:/b"}

    def test_tolerates_broken_structure(self) -> None:
        data: dict = {"manual_models": None}
        app_config.clear_manual_entry(data, "base")
        assert data["manual_models"] is None


def test_save_creates_parent_directory(config_home: Path) -> None:
    assert not config_home.exists()
    ok, _ = app_config.save(app_config.defaults())
    assert ok is True
    assert config_home.is_dir()
    assert os.path.isfile(app_config.config_path())
