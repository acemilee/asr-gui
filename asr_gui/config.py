"""User configuration: no absolute paths baked into the source.

Everything machine-specific (model cache, output folder, last used media,
preferred engine) is stored in ``%APPDATA%\\asr-gui\\config.json`` on Windows
and ``~/.asr-gui/config.json`` elsewhere.  The file is created on first save
and is safe to delete: missing keys fall back to defaults.

A deliberately small module - it exists so the GUI can be shipped publicly
without any hard-coded ``E:\\...`` paths.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONFIG_DIR_ENV = "ASR_GUI_CONFIG_DIR"
CONFIG_FILENAME = "config.json"

# Keep in sync with pyproject.toml / the git tag. Shown in the window title and
# in the packaged exe name, so it must be a plain string literal here.
APP_VERSION = "0.1.3"

DEFAULT_MODEL_ID = "paraformer-zh"
DEFAULT_DEVICE = "cpu"
DEFAULT_THREADS = 8
DEFAULT_ENDPOINT = "mirror"


def config_dir() -> Path:
    env = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / "asr-gui"
    return Path.home() / ".asr-gui"


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def repo_root() -> Path:
    """Directory that contains this ``asr_gui`` folder."""
    return Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    """True when running from the PyInstaller build (``ASR-GUI.exe``)."""
    return bool(getattr(sys, "frozen", False))


def app_home() -> Path:
    """Folder that "owns" the app: the checkout, or the exe's folder when frozen.

    In a frozen build ``__file__`` points inside the temporary ``_MEIxxxx``
    extraction directory, so model folders placed next to the exe would be
    missed - hence this helper.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return repo_root()


def default_output_dir() -> Path:
    """Where transcripts go by default.

    A source checkout writes to ``<repo>/out``. The packaged exe cannot assume
    its own folder is writable (users drop it in Downloads, Program Files, a USB
    stick...), so it uses a plain folder in the home directory instead.
    """
    if is_frozen():
        return Path.home() / "asr-gui-output"
    return repo_root() / "out"


def downloads_dir() -> Path:
    """Where audio pulled from a video URL lands before transcription."""
    if is_frozen():
        return config_dir() / "downloads"
    return app_home() / "downloads"


def logs_dir() -> Path:
    """Crash logs: ``<repo>/logs`` in a checkout, ``%APPDATA%\\asr-gui\\logs`` when frozen."""
    if is_frozen():
        return config_dir() / "logs"
    return repo_root() / "logs"


# Candidate locations that already hold a model cache, in priority order.
def _cache_candidates() -> list[Path]:
    home = app_home()
    return [
        home / "models",
        home / "_model_cache",
        Path.cwd() / "models",
    ]


def effective_cache_dir(config: dict | None = None) -> Path:
    """Resolve the model cache directory: env var > saved config > discovery.

    环境变量优先，是因为它最明确地表态「这次就用这个目录」；配置文件里的值
    可能是上一次在别台机器/别个盘上保存下来的。
    """
    env_value = os.environ.get("ASR_GUI_MODEL_DIR", "").strip()
    if env_value:
        return Path(env_value).expanduser()
    if config:
        value = str(config.get("model_cache_dir") or "").strip()
        if value:
            return Path(value).expanduser()
    return discover_cache_dir()


def discover_cache_dir() -> Path:
    """Pick the first existing folder that already looks like a model cache.

    Falls back to the per-user location (which may not exist yet - that is
    fine, the GUI creates it before downloading).
    """
    from models_registry import default_cache_root, list_choices

    env = os.environ.get("ASR_GUI_MODEL_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    for candidate in _cache_candidates():
        if not candidate.is_dir():
            continue
        try:
            if any(choice.cached for choice in list_choices(candidate)):
                return candidate
        except OSError:
            continue
    return default_cache_root()


def defaults() -> dict:
    return {
        "model_id": DEFAULT_MODEL_ID,
        "device": DEFAULT_DEVICE,
        "num_threads": DEFAULT_THREADS,
        "endpoint": DEFAULT_ENDPOINT,
        "model_cache_dir": str(discover_cache_dir()),
        "output_dir": str(default_output_dir()),
        "last_media": "",
        "output_name": "transcript",
        # 输入来源（本地文件 / 视频链接 / 字幕文件）与链接模式的两个选项
        "source": "本地文件",
        "pages": "",
        "cookies_from_browser": "",
        # model_id -> explicit directory chosen via "import / locate"
        "manual_models": {},
    }


def load() -> dict:
    data = defaults()
    path = config_path()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return data
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in data:
                    data[key] = value
            manual = data.get("manual_models")
            if not isinstance(manual, dict):
                data["manual_models"] = {}
    return data


def save(config: dict) -> tuple[bool, str]:
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True, ""
    except OSError as exc:
        return False, str(exc)


def clear_manual_entry(config: dict, model_id: str) -> None:
    manual = config.get("manual_models")
    if isinstance(manual, dict):
        manual.pop(model_id, None)
