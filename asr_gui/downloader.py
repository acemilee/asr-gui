"""Model downloader: streaming HTTPS + ``.part`` resume + size validation.

Design notes (all learned the hard way, see docs/lessons.md):

* We deliberately do **not** use ``huggingface_hub.snapshot_download``.  The
  mirror (hf-mirror.com) answers with a 302 whose headers lack
  ``X-Repo-Commit``; recent hf_hub releases validate that header and abort.
  A plain ``GET {endpoint}/{repo}/resolve/main/{file}`` works with every
  mirror and keeps the resume logic in our hands.
* A connection can end early *without* raising, so a truncated file would
  silently look complete.  We therefore keep a ``.part`` file, compare the
  received size against ``Content-Length`` and only rename to the final name
  once the byte count matches.
* The 404 case is special-cased: retrying a missing file just wastes time, and
  the caller wants to try the alternative file name (``vocabulary.txt`` vs
  ``vocabulary.json``) instead.

Only the standard library is used.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from models_registry import (
    PARAFORMER_ID,
    SHERPA_COMPONENTS,
    WHISPER_MODELS,
    hf_repo_dir_name,
    sherpa_component_dir,
    whisper_download_names,
)

ENDPOINTS: dict[str, str] = {
    "mirror": "https://hf-mirror.com",
    "official": "https://huggingface.co",
}
ENDPOINT_LABELS = {"mirror": "hf-mirror 镜像", "official": "Hugging Face 官方"}

HEADERS = {"User-Agent": "asr-gui/0.1", "Accept-Encoding": "identity"}

# progress_cb(done_bytes, total_bytes, label) -- total may be 0 (unknown)
ProgressCb = Callable[[int, int, str], None]


class DownloadCancelled(Exception):
    """Raised when the user presses Stop during a download."""


@dataclass
class DownloadResult:
    ok: bool
    location: Path | None = None
    error: str = ""


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled("下载已取消")


def download_file(
    url: str,
    target: Path,
    progress_cb: ProgressCb | None = None,
    cancel: threading.Event | None = None,
    label: str = "",
    attempts: int = 3,
    timeout: int = 60,
) -> tuple[bool, Exception | None]:
    """Download *url* to *target* with resume. Returns (ok, last_error)."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    last_error: Exception | None = None

    for attempt in range(attempts):
        _check_cancel(cancel)
        try:
            resume = part.stat().st_size if part.is_file() else 0
            request_headers = dict(HEADERS)
            if resume:
                request_headers["Range"] = f"bytes={resume}-"
            request = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status == 200 and resume:
                    # Server ignored Range: start over instead of appending.
                    part.unlink(missing_ok=True)
                    resume = 0
                total = 0
                length = response.headers.get("Content-Length")
                if length and length.isdigit():
                    total = int(length) + resume
                mode = "ab" if resume else "wb"
                with open(part, mode) as out:
                    while True:
                        _check_cancel(cancel)
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        resume += len(chunk)
                        if progress_cb is not None:
                            progress_cb(resume, total, label)
            actual = part.stat().st_size
            if actual == 0:
                part.unlink(missing_ok=True)
                return False, RuntimeError("下载内容为空")
            if total and actual != total:
                last_error = RuntimeError(
                    f"下载不完整：收到 {actual} 字节，应为 {total} 字节"
                )
                time.sleep(1.5 * (attempt + 1))
                continue
            if target.exists():
                target.unlink()
            part.rename(target)
            return True, None
        except DownloadCancelled:
            raise
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 404:
                # Missing file: no point retrying the same URL.
                return False, exc
            time.sleep(1.5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001 - network errors are broad
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    return False, last_error


def _candidate_urls(endpoint_key: str, repo: str, filename: str) -> list[str]:
    """Primary endpoint first, then the other one as a fallback."""
    order = [endpoint_key] if endpoint_key in ENDPOINTS else ["mirror"]
    for key in ("mirror", "official"):
        if key != order[0]:
            order.append(key)
    return [
        f"{ENDPOINTS[key]}/{repo}/resolve/main/{filename}" for key in order if key in ENDPOINTS
    ]


def _download_one(
    endpoint_key: str,
    repo: str,
    filename: str,
    target_dir: Path,
    progress_cb: ProgressCb | None,
    cancel: threading.Event | None,
    label_prefix: str = "",
) -> tuple[bool, str]:
    """Try the preferred endpoint, then the fallback one. Returns (ok, message)."""
    last_error = ""
    for url in _candidate_urls(endpoint_key, repo, filename):
        host = url.split("/")[2]
        label = f"{label_prefix}{filename} @ {host}"
        ok, error = download_file(
            url,
            Path(target_dir) / filename,
            progress_cb=progress_cb,
            cancel=cancel,
            label=label,
        )
        if ok:
            return True, ""
        if isinstance(error, urllib.error.HTTPError) and error.code == 404:
            last_error = f"{filename} 在 {host} 不存在（404）"
            continue  # maybe the alias name exists, or the other mirror has it
        last_error = f"{filename}: {error}"
    return False, last_error


def download_whisper_model(
    model_id: str,
    root: Path,
    endpoint: str = "mirror",
    progress_cb: ProgressCb | None = None,
    cancel: threading.Event | None = None,
) -> DownloadResult:
    model = WHISPER_MODELS.get(model_id)
    if model is None:
        return DownloadResult(False, None, f"未知模型：{model_id}")
    target_dir = Path(root) / hf_repo_dir_name(model.repo) / "snapshots" / "main"
    for aliases in whisper_download_names(model):
        ok = False
        message = ""
        for name in aliases:
            ok, message = _download_one(
                endpoint, model.repo, name, target_dir, progress_cb, cancel
            )
            if ok:
                break
        if not ok:
            return DownloadResult(False, None, f"{model.label} 下载失败：{message}")
    return DownloadResult(True, target_dir, "")


def download_sherpa_model(
    root: Path,
    endpoint: str = "mirror",
    progress_cb: ProgressCb | None = None,
    cancel: threading.Event | None = None,
) -> DownloadResult:
    root = Path(root)
    for component, spec in SHERPA_COMPONENTS.items():
        repo = str(spec["repo"])
        target_dir = sherpa_component_dir(root, component)
        for filename in spec["files"]:  # type: ignore[union-attr]
            ok, message = _download_one(
                endpoint,
                repo,
                str(filename),
                target_dir,
                progress_cb,
                cancel,
                label_prefix=f"[{component}] ",
            )
            if not ok:
                return DownloadResult(
                    False, None, f"paraformer-zh 组件 {component} 下载失败：{message}"
                )
    return DownloadResult(True, root / "sherpa", "")


def download_model(
    model_id: str,
    root: Path,
    endpoint: str = "mirror",
    progress_cb: ProgressCb | None = None,
    cancel: threading.Event | None = None,
) -> DownloadResult:
    if model_id == PARAFORMER_ID:
        return download_sherpa_model(root, endpoint, progress_cb, cancel)
    return download_whisper_model(model_id, root, endpoint, progress_cb, cancel)
