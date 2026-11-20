"""Generation profiles keep settings, queue routing and resume identity together."""
import hashlib
import json
from dataclasses import asdict, fields, replace

import pytest

import _generation_config as gc
import _run_progress as rp
import _webrunner_shared as ws
from test_webrunner_shared import FakeBrowserPort, _RunBatchHarness, _ServeHarness, _fake_clock


def _write_config(tmp_path, profile=None, *, active="test"):
    path = tmp_path / "generation_config.json"
    path.write_text(json.dumps({"active_profile": active,
                               "profiles": {"test": profile or {}}}), encoding="utf-8")
    return path


def test_missing_config_retains_legacy_defaults(tmp_path):
    profile = gc.load_generation_profile(tmp_path / "absent.json")
    assert profile.settings == gc.GenerationSettings()
    assert profile.model_candidates is None
    assert profile.todo.character2 == tmp_path / "todo_character2.md"


def test_selected_profile_resolves_its_own_files_independent_of_cwd(tmp_path, monkeypatch):
    path = _write_config(tmp_path, {"model_candidates": [" Custom Model "],
                                  "todo": {"prompt": "prompts/v2.md",
                                           "character1": str(tmp_path.parent / "outside.md")},
                                  "settings": {"steps": 31, "transparent_background": False}})
    monkeypatch.chdir(tmp_path.parent)
    profile = gc.load_generation_profile(path)
    assert profile.model_candidates == ("Custom Model",)
    assert profile.todo.prompt == tmp_path / "prompts/v2.md"
    assert profile.todo.character1 == tmp_path.parent / "outside.md"
    assert profile.settings.steps == 31
    assert profile.settings.transparent_background is False


@pytest.mark.parametrize("profile", [
    {"settings": {"step": 20}}, {"settings": {"steps": True}},
    {"settings": {"steps": 0}}, {"settings": {"guidance": float("nan")}},
    {"settings": {"guidance": 10 ** 400}}, {"settings": {"guidance": -(10 ** 400)}},
    {"settings": {"transparent_background": "false"}},
    {"model_candidates": []}, {"model_candidates": "model"},
    {"settings": {"effort": "Medium"}},
    {"settings": {"noise_schedule": ""}}, {"settings": {"noise_schedule": "  "}},
    {"settings": {"noise_schedule": 1}}, {"settings": {"noise_schedule": True}},
    {"settings": {"effort": "Medium", "steps": None, "sampler": None,
                  "guidance_rescale": None, "noise_schedule": "karras"}},
    {"todo": {"prompt": "same.md", "character1": "same.md"}},
    {"todo": {"prompt": "prompt.md"}},
])
def test_explicit_invalid_profile_fails_before_consuming(tmp_path, profile):
    with pytest.raises(ValueError):
        gc.load_generation_profile(_write_config(tmp_path, profile))


def test_noise_schedule_defaults_to_untouched_and_accepts_text(tmp_path):
    assert gc.GenerationSettings().noise_schedule is None
    assert gc.load_generation_profile(_write_config(tmp_path)).settings.noise_schedule is None
    profile = gc.load_generation_profile(_write_config(tmp_path, {"settings": {
        "noise_schedule": " karras "}}))
    assert profile.settings.noise_schedule == "karras"
    explicit_null = gc.load_generation_profile(_write_config(tmp_path, {"settings": {
        "noise_schedule": None}}))
    assert explicit_null.settings == gc.GenerationSettings()


# 這兩個雜湊值是**加入 `noise_schedule` 之前**的程式算出來的。識別值進
# 續跑檢查點、單張請求與 `webrunner_generation.json`；新欄位讓它們變動的話，正在跑的
# 批次會把自己的檢查點判成別的設定、開新資料夾重產，還沒重啟的 bot 也會跟新批次互相
# 拒絕。路徑刻意用相對字串，雜湊才不隨機器與暫存目錄變。
_LEGACY_IDENTITIES = {
    "v5": "fb71eb40bcf474822fc04758c2683996e29d0ee25381ecfbbee30211b404ec43",
    "v4_5": "8a39357bbaf947a3a9b565174037627facb8ca95147d9c74d528ec9f88dde691",
}
_LEGACY_SETTING_KEYS = ("resolution", "orientation", "effort", "sampler", "steps",
                        "guidance", "guidance_rescale", "transparent_background",
                        "quality_tags", "variety_plus")


def _fixed_profiles(**settings_overrides):
    todo = gc.QueueFiles("t/p.md", "t/c1.md", "t/c2.md", "t/u.md")
    fallback = gc.QueueFiles("f/p.md", "f/c1.md", "f/c2.md", "f/u.md")
    v5 = gc.GenerationProfile("v5", None, gc.GenerationSettings(**settings_overrides),
                              todo, fallback)
    v4_5 = gc.GenerationProfile(
        "v4_5", ("NAI Diffusion V4.5 Full",),
        gc.GenerationSettings(effort=None, steps=28, guidance=7, guidance_rescale=0.6,
                              transparent_background=None, quality_tags=None,
                              **settings_overrides),
        todo, fallback)
    return {"v5": (v5, ["Model A"]), "v4_5": (v4_5, ["NAI Diffusion V4.5 Full"])}


def test_an_unset_noise_schedule_keeps_every_existing_identity():
    for name, (profile, models) in _fixed_profiles().items():
        assert profile.settings.noise_schedule is None
        assert profile.identity(models) == _LEGACY_IDENTITIES[name], name
        # 同一件事換個角度：照改動前的 payload 形狀（只有十個舊鍵）重算也要相等。
        payload = {"name": profile.name, "model_candidates": list(models),
                   "settings": {k: getattr(profile.settings, k) for k in _LEGACY_SETTING_KEYS},
                   "todo": asdict(profile.todo), "fallback": asdict(profile.fallback)}
        legacy = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str)
                                   .encode("utf-8")).hexdigest()
        assert profile.identity(models) == legacy, name


def test_a_set_noise_schedule_is_part_of_the_identity():
    karras = _fixed_profiles(noise_schedule="karras")
    native = _fixed_profiles(noise_schedule="native")
    for name, (profile, models) in karras.items():
        assert profile.identity(models) != _LEGACY_IDENTITIES[name]
        assert profile.identity(models) != native[name][0].identity(models)
    # 只有 None 被省略；其他新欄位（若哪天有）沒列進省略清單就會改到全部識別值。
    assert gc._IDENTITY_OMIT_WHEN_NONE == ("noise_schedule",)
    assert set(gc._IDENTITY_OMIT_WHEN_NONE) | set(_LEGACY_SETTING_KEYS) == {
        field.name for field in fields(gc.GenerationSettings)}, (
        "GenerationSettings 多了新欄位：預設必須是 None，並列進 _IDENTITY_OMIT_WHEN_NONE，"
        "否則每一個既有 profile 的識別值都會變")


def test_unknown_selected_profile_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="active_profile"):
        gc.load_generation_profile(_write_config(tmp_path, active="absent"))


def test_fingerprint_blocks_cross_profile_resume(tmp_path, monkeypatch):
    profile = gc.load_generation_profile(_write_config(tmp_path))
    identity = profile.identity(["Model"])
    monkeypatch.setattr(rp, "PROGRESS_FILE", tmp_path / "progress.json")
    monkeypatch.setattr(rp, "_PROGRESS_TMP", tmp_path / "progress.tmp")
    rp.write_progress("P", "A", "", "", "folder", 10, generation=identity)
    progress = rp.read_progress()
    assert rp.matches(progress, "P", "A", "", "", generation=identity)
    changed = replace(profile, settings=replace(profile.settings, steps=30))
    assert not rp.matches(progress, "P", "A", "", "", generation=changed.identity(["Model"]))
    assert profile.identity(["Other Model"]) != identity
    assert replace(profile, todo=replace(profile.todo, prompt=tmp_path / "other.md")).identity(["Model"]) != identity
    assert not rp.matches({"prompt": "P", "char1": "A", "char2": "", "undesired": ""},
                          "P", "A", "", "", generation=identity)


def test_per_image_checks_use_custom_values_and_skip_null(monkeypatch):
    settings = gc.GenerationSettings(effort=None, sampler="Custom sampler",
                                     transparent_background=False, quality_tags=None)
    calls = []
    monkeypatch.setattr(ws, "set_transparent_background",
                        lambda _p, enabled: calls.append(("transparent", enabled)) or True)
    monkeypatch.setattr(ws, "select_sampler",
                        lambda _p, target: calls.append(("sampler", target)) or True)
    monkeypatch.setattr(ws, "select_model_effort", lambda *_a: pytest.fail("null effort"))
    monkeypatch.setattr(ws, "select_quality_tags", lambda *_a: pytest.fail("null quality"))
    monkeypatch.setattr(ws, "GENERATION_PROFILE", replace(gc.GENERATION_PROFILE, settings=settings))
    monkeypatch.setattr(ws, "find_generate_button", lambda _p: None)
    monkeypatch.setattr(ws, "human_pause", lambda *_a: None)
    assert ws.ensure_generation_options(None)
    assert calls == [("transparent", False), ("sampler", "Custom sampler")]


def test_medium_profile_only_sets_supported_numeric_controls(tmp_path, monkeypatch):
    profile = gc.load_generation_profile(_write_config(tmp_path, {"settings": {
        "effort": "Medium", "steps": None, "sampler": None,
        "guidance_rescale": None, "guidance": 4.5,
        "transparent_background": None, "quality_tags": None, "variety_plus": None}}))
    wrote = []
    monkeypatch.setattr(ws, "select_model_effort", lambda *_a: True)
    monkeypatch.setattr(ws, "ensure_rescale_visible", lambda *_a: pytest.fail("hidden rescale"))
    monkeypatch.setattr(ws, "dump_advanced_labels", lambda *_a: None)
    monkeypatch.setattr(ws, "human_pause", lambda *_a: None)
    monkeypatch.setattr(ws, "first_present_numeric_label", lambda _p, labels: labels[0])
    monkeypatch.setattr(ws, "set_numeric_setting_verified",
                        lambda _p, label, value, **_k: wrote.append((label, value)) or True)
    assert ws.configure_sampler_settings(None, profile.settings)
    assert wrote == [("Prompt Guidance:", 4.5)]


def test_profile_drift_refuses_queue_write_and_child_start(tmp_path, monkeypatch):
    path = _write_config(tmp_path)
    profile = gc.load_generation_profile(path)
    monkeypatch.setattr(gc, "load_generation_profile", lambda **_k: replace(profile, name="new"))
    monkeypatch.setattr(ws, "GENERATION_PROFILE", profile)
    queue = tmp_path / "todo_prompt.md"
    queue.write_text("unchanged\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed"):
        ws.write_todo_characters(queue, [])
    assert queue.read_text(encoding="utf-8") == "unchanged\n"
    monkeypatch.setenv("WEBRUNNER_GENERATION_IDENTITY", "different")
    with pytest.raises(RuntimeError, match="launching"):
        gc.require_expected_profile("current")


@pytest.mark.parametrize("prompt", ["P", "end"])
def test_selected_queue_pop_never_touches_other_version(tmp_path, monkeypatch, prompt):
    with _RunBatchHarness() as harness:
        selected = harness.dir / "todo_prompt_v2.md"
        other = harness.dir / "todo_prompt.md"
        selected.write_text(prompt + "\n", encoding="utf-8")
        other.write_text("other version\n", encoding="utf-8")
        harness.write_queue("todo_character1.md", ["A"])
        with monkeypatch.context() as patch:
            patch.setattr(ws, "TODO_PROMPT_FILE", selected)
            rc = ws.run_batch(FakeBrowserPort(), "e", "p", setup_fn=lambda *_a: True,
                              minimize_fn=lambda: None)
        assert rc == 0
        assert selected.read_text(encoding="utf-8") == ""
        assert other.read_text(encoding="utf-8") == "other version\n"


def test_bot_and_runner_initialize_same_profile_paths():
    import discord_bot as bot
    assert bot.GENERATION_IDENTITY == ws.GENERATION_IDENTITY
    for name in ("TODO_PROMPT_FILE", "TODO_FILE_1", "TODO_FILE_2", "TODO_UNDESIRED_FILE"):
        assert getattr(bot, name) == getattr(ws, name)
    assert bot.PROMPT_FILE == ws.PROMPT_FILE
    assert bot.CHARACTER1_FILE == ws.CHARACTER1_FALLBACK_FILE
    assert bot.CHARACTER2_FILE == ws.CHARACTER2_FALLBACK_FILE


def test_numeric_settings_restored_after_sampler_change(monkeypatch):
    actual = {"Steps:": 14, "Prompt Guidance:": 3, "Prompt Guidance Rescale:": 0}
    writes = []
    monkeypatch.setattr(ws, "read_numeric_setting", lambda _p, label: actual.get(label))
    monkeypatch.setattr(ws, "set_numeric_setting_verified",
                        lambda _p, label, value: writes.append((label, value)) or True)
    assert ws.verify_generation_numbers(None)
    assert writes == [("Steps:", 23), ("Prompt Guidance:", 6)]


def test_generation_identity_matches_only_its_live_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "GENERATION_STATE_FILE", tmp_path / "state.json")
    gc.publish_generation_identity("profile-a")
    assert gc.generation_process_matches(gc.os.getpid(), "profile-a")
    assert gc.generation_process_matches(gc.os.getppid(), "profile-a")
    assert not gc.generation_process_matches(-1, "profile-a")
    assert not gc.generation_process_matches(gc.os.getpid(), "profile-b")


def test_one_shot_cannot_run_on_a_different_profile(monkeypatch):
    events = []
    monkeypatch.setattr(ws, "emit_event", lambda *a, **k: events.append((a, k)))
    monkeypatch.setattr(ws, "fill_main_prompt", lambda *_a: pytest.fail("must reject before browser input"))
    ws.serve_single_image_request(None, {"request_id": "request1", "prompt": "P", "generation": "different"})
    assert len(events) == 1
    assert events[0][1]["ok"] is False


def test_main_rejects_launching_profile_before_browser_boot(monkeypatch):
    import webrunner_je_only as runner
    monkeypatch.setenv("WEBRUNNER_GENERATION_IDENTITY", "different")
    monkeypatch.setattr(runner, "start_driver", lambda: pytest.fail("browser must not start"))
    monkeypatch.setattr(ws, "publish_generation_identity", lambda *_a: pytest.fail("must not publish mismatched identity"))
    assert runner.main() == ws.RC_GENERATION_BLOCKED


def test_batch_profile_drift_stops_without_consuming(monkeypatch):
    with _RunBatchHarness() as harness:
        harness.write_queue("todo_prompt.md", ["P"])
        harness.write_queue("todo_character1.md", ["A"])
        def changed(_profile):
            raise gc.GenerationProfileChanged("changed")
        with monkeypatch.context() as patch:
            patch.setattr(ws, "require_current_profile", changed)
            rc = ws.run_batch(FakeBrowserPort(), "e", "p", setup_fn=lambda: True,
                              minimize_fn=lambda: None)
        assert rc == ws.RC_GENERATION_BLOCKED
        assert (harness.dir / "todo_prompt.md").read_text(encoding="utf-8") == "P\n"


def test_adoption_rejects_different_profile(monkeypatch):
    import discord_bot as bot
    from test_batch_recovery import _reattach, _reattach_env
    env = _reattach_env(monkeypatch)
    monkeypatch.setattr(bot, "generation_process_matches", lambda *_a: False)
    assert _reattach() is None
    assert env.watched == []


def test_one_shot_profile_drift_is_not_a_quota_error(monkeypatch):
    events = []
    with _ServeHarness(), _fake_clock():
        def changed(*_a, **_k):
            raise gc.GenerationProfileChanged("changed")
        with monkeypatch.context() as patch:
            patch.setattr(ws, "generate_one_image", changed)
            patch.setattr(ws, "emit_event", lambda event, **k: events.append(k) if event == "single_image_done" else None)
            patch.setattr(ws, "dismiss_blocking_dialog", lambda *_a: pytest.fail("not a quota dialog"))
            with pytest.raises(gc.GenerationProfileChanged):
                ws.serve_single_image_request(FakeBrowserPort(), {"request_id": "request1", "prompt": "P"})
    assert len(events) == 1
    assert "settings changed" in events[0]["error"]
