"""models_registry.py：缓存布局探测、状态文案、手动导入校验。"""

from __future__ import annotations

from pathlib import Path

import models_registry as registry


def _multiset(directory: Path, names, make, sizes=None) -> None:
    sizes = sizes or {}
    for name in names:
        make(directory / name, sizes.get(name, registry.MIN_FILE_BYTES.get(name, 1024)))


class TestRepoNaming:
    def test_hf_repo_dir_name(self) -> None:
        assert (
            registry.hf_repo_dir_name("Systran/faster-whisper-tiny")
            == "models--Systran--faster-whisper-tiny"
        )

    def test_whisper_tiers_are_complete(self) -> None:
        ids = list(registry.WHISPER_MODELS)
        assert ids == [
            "tiny",
            "base",
            "small",
            "medium",
            "large-v3",
            "turbo",
            "belle-turbo-zh",
        ]
        for model in registry.WHISPER_MODELS.values():
            assert model.repo
            assert model.files
            assert model.approx_mb > 0

    def test_community_ct2_repos_require_preprocessor(self) -> None:
        for model_id in ("turbo", "belle-turbo-zh"):
            files = registry.WHISPER_MODELS[model_id].files
            assert "preprocessor_config.json" in files
            assert "vocabulary.json" in files


class TestFilePlausibility:
    def test_too_small_is_implausible(self, tmp_path: Path, sparse_file) -> None:
        path = sparse_file(tmp_path / "model.bin", 1024)
        assert registry.file_is_plausible(path) is False

    def test_minimum_size_is_enough(self, tmp_path: Path, sparse_file) -> None:
        path = sparse_file(tmp_path / "model.bin", registry.MIN_FILE_BYTES["model.bin"])
        assert registry.file_is_plausible(path) is True

    def test_unknown_name_only_needs_one_byte(self, tmp_path: Path, sparse_file) -> None:
        assert registry.file_is_plausible(sparse_file(tmp_path / "x.txt", 1)) is True
        assert registry.file_is_plausible(tmp_path / "missing.txt") is False


class TestWhisperCacheState:
    def test_not_downloaded(self, tmp_path: Path) -> None:
        state = registry.whisper_cache_state("base", tmp_path)
        assert state.cached is False
        assert state.reason == "尚未下载"

    def test_complete_snapshot_is_cached(self, tmp_path: Path, sparse_file) -> None:
        model = registry.WHISPER_MODELS["base"]
        snap = (
            tmp_path
            / registry.hf_repo_dir_name(model.repo)
            / "snapshots"
            / "main"
        )
        _multiset(snap, model.files, sparse_file)
        state = registry.whisper_cache_state("base", tmp_path)
        assert state.cached is True
        assert state.location == snap
        assert state.missing == []

    def test_incomplete_snapshot_reports_missing(self, tmp_path: Path, sparse_file) -> None:
        model = registry.WHISPER_MODELS["base"]
        snap = tmp_path / registry.hf_repo_dir_name(model.repo) / "snapshots" / "main"
        _multiset(snap, model.files[:2], sparse_file)
        state = registry.whisper_cache_state("base", tmp_path)
        assert state.cached is False
        assert state.reason == "缓存不完整"
        assert set(state.missing) == set(model.files[2:])

    def test_falls_back_to_older_complete_snapshot(
        self, tmp_path: Path, sparse_file
    ) -> None:
        model = registry.WHISPER_MODELS["tiny"]
        repo_root = tmp_path / registry.hf_repo_dir_name(model.repo) / "snapshots"
        _multiset(repo_root / "aaa", model.files, sparse_file)
        _multiset(repo_root / "zzz", model.files[:1], sparse_file)
        state = registry.whisper_cache_state("tiny", tmp_path)
        assert state.cached is True
        assert state.location is not None
        assert state.location.name == "aaa"

    def test_unknown_model(self, tmp_path: Path) -> None:
        state = registry.whisper_cache_state("nope", tmp_path)
        assert state.cached is False
        assert "未知模型" in state.reason


class TestSherpaCacheState:
    def _make_full(self, root: Path, sparse_file) -> Path:
        for component, spec in registry.SHERPA_COMPONENTS.items():
            directory = registry.sherpa_component_dir(root, component)
            for name in spec["files"]:
                sparse_file(directory / str(name), registry.MIN_FILE_BYTES[str(name)])
        return root / "sherpa"

    def test_complete(self, tmp_path: Path, sparse_file) -> None:
        expected = self._make_full(tmp_path, sparse_file)
        state = registry.sherpa_cache_state(tmp_path)
        assert state.cached is True
        assert state.location == expected

    def test_missing_component(self, tmp_path: Path, sparse_file) -> None:
        self._make_full(tmp_path, sparse_file)
        (registry.sherpa_component_dir(tmp_path, "punc") / "model.onnx").unlink()
        state = registry.sherpa_cache_state(tmp_path)
        assert state.cached is False
        assert state.missing == ["punc/model.onnx"]
        assert state.reason == "缓存不完整"

    def test_nothing_downloaded(self, tmp_path: Path) -> None:
        state = registry.sherpa_cache_state(tmp_path)
        assert state.cached is False
        assert state.reason == "尚未下载"
        assert len(state.missing) == registry.SHERPA_FILES_TOTAL


class TestListChoices:
    def test_eight_tiers_in_order(self, tmp_path: Path) -> None:
        choices = registry.list_choices(tmp_path)
        assert len(choices) == 8
        assert [c.model_id for c in choices][-1] == registry.PARAFORMER_ID
        assert {c.kind for c in choices} == {"whisper", "sherpa"}

    def test_status_text_when_missing(self, tmp_path: Path) -> None:
        for choice in registry.list_choices(tmp_path):
            assert choice.cached is False
            assert choice.status_text.startswith("未缓存 · 需下载约 ")
            assert f"{choice.approx_mb} MB" in choice.status_text

    def test_paraformer_size_matches_registry(self, tmp_path: Path) -> None:
        choice = registry.choice_by_id(registry.PARAFORMER_ID, tmp_path)
        assert choice is not None
        assert choice.approx_mb == registry.PARAFORMER_APPROX_MB == 520

    def test_cached_status_text(self, tmp_path: Path, sparse_file) -> None:
        model = registry.WHISPER_MODELS["tiny"]
        snap = tmp_path / registry.hf_repo_dir_name(model.repo) / "snapshots" / "main"
        _multiset(snap, model.files, sparse_file)
        choice = registry.choice_by_id("tiny", tmp_path)
        assert choice is not None
        assert choice.cached is True
        assert choice.status_text == "已缓存"


class TestValidateManual:
    def test_whisper_accepts_complete_directory(self, tmp_path: Path, sparse_file) -> None:
        model = registry.WHISPER_MODELS["small"]
        _multiset(tmp_path, model.files, sparse_file)
        ok, missing = registry.validate_manual_whisper(tmp_path, "small")
        assert ok is True
        assert missing == []

    def test_whisper_rejects_partial_directory(self, tmp_path: Path, sparse_file) -> None:
        model = registry.WHISPER_MODELS["small"]
        _multiset(tmp_path, model.files[:1], sparse_file)
        ok, missing = registry.validate_manual_whisper(tmp_path, "small")
        assert ok is False
        assert set(missing) == set(model.files[1:])

    def test_whisper_rejects_missing_directory(self, tmp_path: Path) -> None:
        ok, missing = registry.validate_manual_whisper(tmp_path / "absent", "base")
        assert ok is False
        assert missing and "目录不存在" in missing[0]

    def test_whisper_unknown_model(self, tmp_path: Path) -> None:
        ok, missing = registry.validate_manual_whisper(tmp_path, "nope")
        assert ok is False
        assert "未知模型" in missing[0]

    def test_sherpa_accepts_asr_punc_vad(self, tmp_path: Path, sparse_file) -> None:
        # 用户挑选的目录是 ``sherpa`` 本身（其下含 asr/ punc/ vad/）
        sherpa_dir = TestSherpaCacheState()._make_full(tmp_path, sparse_file)
        ok, missing = registry.validate_manual_sherpa(sherpa_dir)
        assert ok is True
        assert missing == []

    def test_sherpa_rejects_flat_directory(self, tmp_path: Path) -> None:
        ok, missing = registry.validate_manual_sherpa(tmp_path)
        assert ok is False
        assert len(missing) == registry.SHERPA_FILES_TOTAL


class TestCorruptMessage:
    def test_matches_ctranslate2_markers(self) -> None:
        assert registry.corrupt_message(RuntimeError("model.bin is incomplete")) is not None
        assert (
            registry.corrupt_message(RuntimeError("failed to read a buffer of 4096"))
            is not None
        )

    def test_ignores_unrelated_errors(self) -> None:
        assert registry.corrupt_message(RuntimeError("out of memory")) is None
