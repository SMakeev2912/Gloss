import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import sysinfo  # noqa: E402
import updater  # noqa: E402


def test_set_model_switches_backend(tmp_path):
    path = tmp_path / "config.json"
    config.set_model(path, "qwen2.5:1.5b-instruct", "ollama")
    cfg = config.load(path)
    assert (cfg.backend, cfg.model.name) == ("ollama", "qwen2.5:1.5b-instruct")


def test_versions():
    assert updater.parse_version("v1.2") == (1, 2, 0)
    assert updater.is_newer("1.0.1", "1.0.0")
    assert updater.is_newer("v2.0.0", "1.9.9")
    assert not updater.is_newer("1.0.0", "1.0.0")
    assert not updater.is_newer("0.9.9", "1.0.0")


def test_manifest_urls():
    base = updater.OLLAMA_REGISTRY
    assert updater._manifest_url("qwen2.5:3b-instruct") == f"{base}/v2/library/qwen2.5/manifests/3b-instruct"
    assert updater._manifest_url("qwen2.5") == f"{base}/v2/library/qwen2.5/manifests/latest"
    assert updater._manifest_url("user/model:tag") == f"{base}/v2/user/model/manifests/tag"
    assert updater._manifest_url("hf.co/user/repo:Q4") is None


def test_pull_progress_aggregates_layers():
    progress = updater.PullProgress(expected_bytes=1000)
    assert progress.feed({"status": "pulling manifest"})[0] is None
    fraction, _ = progress.feed({"status": "pulling a", "digest": "a", "total": 800, "completed": 400})
    assert fraction == 0.4  # ожидаемый размер больше суммы известных слоёв
    progress.feed({"status": "pulling b", "digest": "b", "total": 400, "completed": 200})
    fraction, text = progress.feed({"status": "pulling a", "digest": "a", "total": 800, "completed": 800})
    assert fraction == 1000 / 1200 and "из" in text


def _info(ram, gpu=None, free=100.0, has=()):
    state = sysinfo.OllamaState(installed=True, running=True, models=set(has))
    return sysinfo.SystemInfo(windows="Windows 11", build=26100, arch="x64", cpu="cpu", threads=8, avx2=True,
                              ram_gb=ram, gpu=gpu, free_gb={"C:\\": free}, ollama=state, engine_installed=True,
                              builtin_models=set(has), internet=True)


def test_model_fit():
    model = sysinfo.DEFAULT_MODEL
    assert sysinfo.model_fit(_info(8), model).status == "fail"
    assert sysinfo.model_fit(_info(12), model).status == "warn"
    assert sysinfo.model_fit(_info(16), model).status == "ok"  # и без видеокарты
    assert sysinfo.model_fit(_info(32, sysinfo.Gpu("NVIDIA RTX", 8, "nvidia")), model).verdict.startswith("Быстро")
    assert sysinfo.model_fit(_info(32, free=1.0), model).status == "fail"
    assert sysinfo.model_fit(_info(32, free=1.0, has={model}), model).status != "fail"


def test_notes_from_all_skipped_versions():
    releases = [
        {"tag_name": "v1.3.0", "body": "- Третье\n- **Важное** с [ссылкой](https://x)"},
        {"tag_name": "v1.4.0-beta", "prerelease": True, "body": "- Черновое"},
        {"tag_name": "v1.1.0", "body": "- Первое"},
        {"tag_name": "v1.2.0", "body": ""},
        {"tag_name": "v1.0.0", "body": "- Уже установлено"},
        {"tag_name": "v9.0.0", "draft": True, "body": "- Не опубликовано"},
    ]
    notes = updater.collect_notes(releases, current="1.0.0")
    assert notes == [("1.3.0", ["Третье", "Важное с ссылкой"]), ("1.2.0", []), ("1.1.0", ["Первое"])]


def test_changelog_release_moves_unreleased_points():
    import changelog
    text = "# Что нового\n\n## Готовится\n\n- Новое\n\n## 1.0.0 · 23.10.2026\n\n- Старое\n"
    assert changelog.unreleased(text) == ["Новое"]
    released = changelog.release(text, "1.1.0", "24.10.2026", ["Новое"])
    assert changelog.unreleased(released) == []
    assert changelog.version_points("1.1.0", released) == ["Новое"]
    assert changelog.version_points("v1.0.0", released) == ["Старое"]
    assert released.index("## Готовится") < released.index("## 1.1.0") < released.index("## 1.0.0")
