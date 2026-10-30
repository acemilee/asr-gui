"""Model registry: which models we support, where they come from, how to validate them.

Facts in this file were cross-checked against the upstream project
``like-attract/video-to-note`` (MIT).  Notes that cost real debugging time:

* ``Systran/faster-whisper-turbo`` does not exist on the Hub (404), so the
  ``turbo`` slot maps to the community CT2 conversion instead.
* ``large-v3`` in the official Systran repo ships ``vocabulary.json`` while
  tiny/base/small/medium ship ``vocabulary.txt`` -- asking for the wrong name
  returns 404.
* The community CT2 conversions (turbo, belle-turbo-zh) *require*
  ``preprocessor_config.json``: faster-whisper reads ``feature_size`` from it
  and defaults to 80 mel, while large-v3 family models need 128 mel.  Without
  it loading fails with a shape mismatch.
* sherpa-onnx conversions embed the vocabulary in the ONNX metadata, so the
  punctuation model only needs ``model.onnx`` and the ASR model needs
  ``tokens.txt``.

Only the standard library is used here; downloading lives in ``downloader.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# generic helpers
# --------------------------------------------------------------------------

# Lower bounds that catch truncated / HTML-error-page "model" files.  They are
# deliberately a bit under the real size (tiny model.bin is ~75 MB).
MIN_FILE_BYTES = {
    "model.bin": 30 * 1024 * 1024,
    "model.int8.onnx": 100 * 1024 * 1024,
    "model.onnx": 100 * 1024 * 1024,
    "silero_vad.onnx": 512 * 1024,
    "tokenizer.json": 100 * 1024,
    "vocabulary.txt": 100 * 1024,
    "vocabulary.json": 100 * 1024,
    "tokens.txt": 10 * 1024,
    "preprocessor_config.json": 256,
    "config.json": 256,
}

# Strings CTranslate2 emits when a cached model is corrupt/partial.
CORRUPT_MARKERS = ("is incomplete", "failed to read a buffer")

# Name of the directory layout used by huggingface_hub: models--Org--Name
HF_CACHE_PREFIX = "models--"


def hf_repo_dir_name(repo: str) -> str:
    return HF_CACHE_PREFIX + repo.replace("/", "--")


def file_is_plausible(path: Path) -> bool:
    """Cheap sanity check: exists and is at least the recorded minimum size."""
    if not path.is_file():
        return False
    minimum = MIN_FILE_BYTES.get(path.name, 1)
    try:
        return path.stat().st_size >= minimum
    except OSError:
        return False


def _missing_files(directory: Path, filenames: Iterable[str]) -> list[str]:
    """Return the names that are missing or too small inside *directory*."""
    missing = []
    for name in filenames:
        if not file_is_plausible(directory / name):
            missing.append(name)
    return missing


# --------------------------------------------------------------------------
# faster-whisper (CTranslate2) models
# --------------------------------------------------------------------------

# Required files for official Systran conversions and for community CT2 repos.
SYSTRAN_FILES = ("config.json", "model.bin", "tokenizer.json")
COMMUNITY_CT2_EXTRA = ("vocabulary.json", "preprocessor_config.json")


@dataclass(frozen=True)
class WhisperModel:
    model_id: str
    label: str
    repo: str
    approx_mb: int
    files: tuple[str, ...] = field(default=SYSTRAN_FILES + ("vocabulary.txt",))

    @property
    def display(self) -> str:
        return f"{self.label}（约 {self.approx_mb} MB）"


WHISPER_MODELS: dict[str, WhisperModel] = {
    m.model_id: m
    for m in (
        WhisperModel(
            "tiny",
            "tiny · 最快",
            "Systran/faster-whisper-tiny",
            75,
        ),
        WhisperModel(
            "base",
            "base · 默认",
            "Systran/faster-whisper-base",
            142,
        ),
        WhisperModel(
            "small",
            "small · 中文更准",
            "Systran/faster-whisper-small",
            466,
        ),
        WhisperModel(
            "medium",
            "medium · 中文不如 turbo 档",
            "Systran/faster-whisper-medium",
            1500,
        ),
        # large-v3 ships vocabulary.json, not vocabulary.txt
        WhisperModel(
            "large-v3",
            "large-v3 · 多语言最强",
            "Systran/faster-whisper-large-v3",
            3100,
            files=SYSTRAN_FILES + ("vocabulary.json",),
        ),
        WhisperModel(
            "turbo",
            "turbo · 多语言加速",
            "deepdml/faster-whisper-large-v3-turbo-ct2",
            1600,
            files=SYSTRAN_FILES + COMMUNITY_CT2_EXTRA,
        ),
        WhisperModel(
            "belle-turbo-zh",
            "belle-turbo-zh · 中文精度最好",
            "wolfofbackstreet/faster-whisper-belle-whisper-large-v3-turbo-zh-ct2-int8",
            800,
            files=SYSTRAN_FILES + COMMUNITY_CT2_EXTRA,
        ),
    )
}


def whisper_download_names(model: WhisperModel) -> list[tuple[str, ...]]:
    """File names to fetch, each entry being an ordered list of aliases.

    ``large-v3`` may publish either vocabulary name depending on the mirror
    snapshot, so callers try the aliases in order and keep the first hit.
    """
    names: list[tuple[str, ...]] = []
    for name in model.files:
        if name == "vocabulary.json" and model.model_id != "large-v3":
            names.append(("vocabulary.json", "vocabulary.txt"))
        elif name == "vocabulary.txt":
            names.append(("vocabulary.txt", "vocabulary.json"))
        else:
            names.append((name,))
    return names


# --------------------------------------------------------------------------
# sherpa-onnx (paraformer-zh) components
# --------------------------------------------------------------------------

SHERPA_COMPONENTS: dict[str, dict[str, object]] = {
    "asr": {
        "repo": "csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14",
        "files": ("model.int8.onnx", "tokens.txt"),
        "approx_mb": 240,
    },
    "punc": {
        "repo": "csukuangfj/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12",
        "files": ("model.onnx",),
        "approx_mb": 290,
    },
    "vad": {
        "repo": "csukuangfj/vad",
        "files": ("silero_vad.onnx",),
        "approx_mb": 2,
    },
}

PARAFORMER_ID = "paraformer-zh"
PARAFORMER_LABEL = "paraformer-zh · 中文推荐·最快最准（仅 CPU）"
PARAFORMER_APPROX_MB = 520


# --------------------------------------------------------------------------
# cache roots
# --------------------------------------------------------------------------

MODEL_DIR_ENV = "ASR_GUI_MODEL_DIR"


def default_cache_root() -> Path:
    """Where downloaded models live unless the user overrides it."""
    env = os.environ.get(MODEL_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) / "asr-gui" if appdata else Path.home() / ".asr-gui"
    return base / "models"


# --------------------------------------------------------------------------
# cache inspection
# --------------------------------------------------------------------------

@dataclass
class CacheState:
    cached: bool
    location: Path | None
    missing: list[str]
    reason: str = ""


def _snapshot_candidates(root: Path, repo: str) -> list[Path]:
    """All snapshot dirs that could hold a model, newest-looking first."""
    repo_root = root / hf_repo_dir_name(repo)
    snaps = repo_root / "snapshots"
    if not snaps.is_dir():
        return []
    found = [d for d in snaps.iterdir() if d.is_dir()]
    # ``main`` first (our own layout), then the rest
    found.sort(key=lambda p: (p.name != "main", p.name))
    return found


def whisper_cache_state(model_id: str, root: Path | None = None) -> CacheState:
    model = WHISPER_MODELS.get(model_id)
    if model is None:
        return CacheState(False, None, [], f"未知模型：{model_id}")
    root = Path(root) if root else default_cache_root()
    candidates = _snapshot_candidates(root, model.repo)
    if not candidates:
        return CacheState(False, None, list(model.files), "尚未下载")
    missing = _missing_files(candidates[0], model.files)
    if not missing:
        return CacheState(True, candidates[0], [], "")
    # Maybe an older snapshot is complete while the newest one is not.
    for cand in candidates[1:]:
        if not _missing_files(cand, model.files):
            return CacheState(True, cand, [], "")
    return CacheState(False, candidates[0], missing, "缓存不完整")


SHERPA_FILES_TOTAL = sum(len(spec['files']) for spec in SHERPA_COMPONENTS.values())  # type: ignore[arg-type]


def sherpa_component_dir(root: Path, component: str) -> Path:
    return Path(root) / "sherpa" / component


def sherpa_cache_state(root: Path | None = None) -> CacheState:
    root = Path(root) if root else default_cache_root()
    missing: list[str] = []
    for component, spec in SHERPA_COMPONENTS.items():
        directory = sherpa_component_dir(root, component)
        for name in spec["files"]:  # type: ignore[union-attr]
            if not file_is_plausible(directory / str(name)):
                missing.append(f"{component}/{name}")
    if not missing:
        return CacheState(True, root / "sherpa", [], "")
    reason = "尚未下载" if len(missing) == SHERPA_FILES_TOTAL else "缓存不完整"
    return CacheState(False, None, missing, reason)





# --------------------------------------------------------------------------
# unified choice list for the GUI
# --------------------------------------------------------------------------

@dataclass
class ModelChoice:
    model_id: str
    label: str
    kind: str  # "whisper" | "sherpa"
    cached: bool
    location: Path | None
    approx_mb: int
    missing: list[str]

    @property
    def status_text(self) -> str:
        if self.cached:
            return "已缓存"
        if self.missing and len(self.missing) < 3 and self.location is not None:
            return "缓存不完整"
        return f"未缓存 · 需下载约 {self.approx_mb} MB"


def list_choices(root: Path | None = None) -> list[ModelChoice]:
    root = Path(root) if root else default_cache_root()
    choices: list[ModelChoice] = []
    for model in WHISPER_MODELS.values():
        state = whisper_cache_state(model.model_id, root)
        choices.append(
            ModelChoice(
                model_id=model.model_id,
                label=model.label,
                kind="whisper",
                cached=state.cached,
                location=state.location,
                approx_mb=model.approx_mb,
                missing=state.missing,
            )
        )
    para = sherpa_cache_state(root)
    choices.append(
        ModelChoice(
            model_id=PARAFORMER_ID,
            label=PARAFORMER_LABEL,
            kind="sherpa",
            cached=para.cached,
            location=para.location,
            approx_mb=PARAFORMER_APPROX_MB,
            missing=para.missing,
        )
    )
    return choices


def choice_by_id(model_id: str, root: Path | None = None) -> ModelChoice | None:
    for choice in list_choices(root):
        if choice.model_id == model_id:
            return choice
    return None


# --------------------------------------------------------------------------
# manual import
# --------------------------------------------------------------------------

def validate_manual_whisper(directory: Path, model_id: str) -> tuple[bool, list[str]]:
    """Check a user-picked folder holds a usable CT2 model."""
    directory = Path(directory)
    model = WHISPER_MODELS.get(model_id)
    if model is None:
        return False, [f"未知模型：{model_id}"]
    if not directory.is_dir():
        return False, [f"目录不存在：{directory}"]
    missing = _missing_files(directory, model.files)
    return (not missing), missing


def validate_manual_sherpa(directory: Path) -> tuple[bool, list[str]]:
    """Check a user-picked ``sherpa`` folder has asr/ punc/ vad/ sub-dirs."""
    directory = Path(directory)
    if not directory.is_dir():
        return False, [f"目录不存在：{directory}"]
    missing: list[str] = []
    for component, spec in SHERPA_COMPONENTS.items():
        for name in spec["files"]:  # type: ignore[union-attr]
            if not file_is_plausible(directory / component / str(name)):
                missing.append(f"{component}/{name}")
    return (not missing), missing


def corrupt_message(exc: BaseException) -> str | None:
    """Return a hint when an exception looks like a corrupt cached model."""
    text = str(exc).lower()
    for marker in CORRUPT_MARKERS:
        if marker in text:
            return (
                "模型文件疑似损坏或不完整（CTranslate2 报「is incomplete / failed to "
                "read a buffer」）。请删除对应缓存目录后重新下载。"
            )
    return None
