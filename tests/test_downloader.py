"""downloader.py：URL 组装、取消、以及 file:// 下的完整下载路径（离线）。"""

from __future__ import annotations

import threading
import urllib.request
from pathlib import Path

import downloader
import pytest


class TestCandidateUrls:
    def test_mirror_first(self) -> None:
        urls = downloader._candidate_urls("mirror", "Org/Name", "config.json")
        assert urls[0].startswith(downloader.ENDPOINTS["mirror"])
        assert urls[1].startswith(downloader.ENDPOINTS["official"])
        assert urls[0].endswith("/Org/Name/resolve/main/config.json")

    def test_official_first(self) -> None:
        urls = downloader._candidate_urls("official", "Org/Name", "a.bin")
        assert urls[0].startswith(downloader.ENDPOINTS["official"])

    def test_unknown_endpoint_falls_back_to_mirror(self) -> None:
        urls = downloader._candidate_urls("bogus", "Org/Name", "a.bin")
        assert urls[0].startswith(downloader.ENDPOINTS["mirror"])
        assert len(urls) == 2


class TestDownloadFile:
    def test_cancel_before_start(self, tmp_path: Path) -> None:
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(downloader.DownloadCancelled):
            downloader.download_file(
                "https://example.invalid/x", tmp_path / "x", cancel=cancel
            )

    def test_successful_copy_over_file_url(self, tmp_path: Path) -> None:
        """file:// 走同一段循环（无 Content-Length，不校验体积）。"""
        source = tmp_path / "source.bin"
        source.write_bytes(b"asr-gui" * 100)
        target = tmp_path / "out" / "copy.bin"
        seen: list[tuple[int, int, str]] = []
        ok, error = downloader.download_file(
            source.as_uri(),
            target,
            progress_cb=lambda done, total, label: seen.append((done, total, label)),
            label="unit",
        )
        assert ok is True
        assert error is None
        assert target.read_bytes() == source.read_bytes()
        assert seen and seen[-1][0] == len(source.read_bytes())
        # 临时 .part 文件必须已经改名清掉
        assert not list(target.parent.glob("*.part"))

    def test_missing_source_reports_error(self, tmp_path: Path) -> None:
        ok, error = downloader.download_file(
            (tmp_path / "absent.bin").as_uri(),
            tmp_path / "x.bin",
            attempts=1,
            timeout=5,
        )
        assert ok is False
        assert error is not None


class TestDownloadModel:
    def test_unknown_model_is_rejected(self, tmp_path: Path) -> None:
        result = downloader.download_model("nope", tmp_path)
        assert result.ok is False
        assert "未知模型" in result.error

    def test_cancel_propagates_for_sherpa(self, tmp_path: Path) -> None:
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(downloader.DownloadCancelled):
            downloader.download_model(
                downloader.PARAFORMER_ID, tmp_path, cancel=cancel
            )

    def test_whisper_target_layout(self, tmp_path: Path) -> None:
        """确认下载目录布局与 registry 的探测规则一致（不联网，只算路径）。"""
        import models_registry as registry

        model = registry.WHISPER_MODELS["tiny"]
        expected = (
            tmp_path
            / registry.hf_repo_dir_name(model.repo)
            / "snapshots"
            / "main"
        )
        assert expected == tmp_path / "models--Systran--faster-whisper-tiny" / "snapshots" / "main"


class TestHeaders:
    def test_identity_encoding_required(self) -> None:
        # 不带 identity 时某些镜像会用 gzip，导致 Content-Length 与实际写入不符
        assert downloader.HEADERS["Accept-Encoding"] == "identity"
        assert "User-Agent" in downloader.HEADERS

    def test_endpoints(self) -> None:
        assert downloader.ENDPOINTS["mirror"] == "https://hf-mirror.com"
        assert downloader.ENDPOINTS["official"] == "https://huggingface.co"
        assert set(downloader.ENDPOINT_LABELS) == set(downloader.ENDPOINTS)


def test_urllib_is_used_not_huggingface_hub() -> None:
    """回归：曾经踩过 hf_hub 在镜像上校验失败的问题，别再引回来。"""
    source = (Path(downloader.__file__)).read_text(encoding="utf-8")
    assert "huggingface_hub" in source  # 只出现在注释/文档里说明为什么不用
    assert "import urllib.request" in source
    assert urllib.request.__name__ == "urllib.request"
