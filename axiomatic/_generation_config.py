"""Validated, process-lifetime generation profiles shared by bot and runner."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATION_CONFIG_FILE = PROJECT_ROOT / "generation_config.json"
GENERATION_STATE_FILE = PROJECT_ROOT / "webrunner_generation.json"

# 這一個行程（與它的子孫）用哪一套生成設定，蓋過 `active_profile`。
#
# `active_profile` 是**持久的預設值**，只有人手改設定檔時才會變。`wake_autostart.py --profile
# NAME` 不改寫它，而是讓那一次叫醒的每一支監督者（每個平台的 bot 監督者與批次監督者）
# 在啟動時把 NAME 放進這個環境變數；之後的
# 子行程重生、Chrome 週期重啟、bot 自己 spawn 的批次（`{**os.environ, …}`）全部繼承同一個
# 值，所以那一整串行程從頭到尾都是 NAME；下一次沒有指定的啟動又回到預設。
# 監督者那一端的名字是 `_supervisor.GENERATION_PROFILE_ENV`——兩份字面值由
# `test_generation_selection` 釘成同一個，刻意不互相 import：監督者匯入本模組的話，
# 模組層的 `load_generation_profile()` 會在它還沒設好這個變數之前就先跑一次。
PROFILE_OVERRIDE_ENV = "WEBRUNNER_GENERATION_PROFILE"


def profile_override() -> str | None:
    """環境變數指定的 profile 名稱；沒設或只有空白 → None（＝用 `active_profile`）。"""
    value = os.environ.get(PROFILE_OVERRIDE_ENV)
    if value is None or not value.strip():
        return None
    return value.strip()


class GenerationProfileChanged(RuntimeError):
    """A profile change requires stopping and restarting the generating processes."""


@dataclass(frozen=True)
class GenerationSettings:
    resolution: str = "Normal"
    orientation: str = "Landscape"
    effort: str | None = "High"
    sampler: str | None = "Euler Ancestral"
    steps: int | None = 23
    guidance: float | None = 6
    guidance_rescale: float | None = 0
    transparent_background: bool | None = True
    quality_tags: str | None = "Standard"
    variety_plus: bool | None = True


@dataclass(frozen=True)
class QueueFiles:
    prompt: Path
    character1: Path
    character2: Path
    undesired: Path


@dataclass(frozen=True)
class GenerationProfile:
    name: str
    model_candidates: tuple[str, ...] | None
    settings: GenerationSettings
    todo: QueueFiles
    fallback: QueueFiles

    def identity(self, model_candidates: list[str] | tuple[str, ...]) -> str:
        payload = asdict(self)
        payload["model_candidates"] = list(model_candidates)
        return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                         default=str).encode("utf-8")).hexdigest()


def _object(value: object, label: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"generation config: {label} must be an object")
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(f"generation config: unknown {label} keys: {sorted(unknown)}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"generation config: {label} must be nonempty text")
    return value.strip()


def _paths(data: object, root: Path, *, todo: bool) -> QueueFiles:
    names = {field.name for field in fields(QueueFiles)}
    data = _object(data, "todo" if todo else "fallback", names)
    paths = {}
    for name in ("prompt", "character1", "character2", "undesired"):
        default = f"{'todo_' if todo else ''}{name}.md"
        value = _text(data.get(name, default), name)
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        paths[name] = path.resolve()
    if len(set(paths.values())) != len(paths):
        raise ValueError("generation config: queue paths must be distinct")
    return QueueFiles(**paths)


def _settings(data: object) -> GenerationSettings:
    defaults = asdict(GenerationSettings())
    data = _object(data, "settings", set(defaults))
    result = defaults | data
    for key, value in result.items():
        if value is None and key not in {"resolution", "orientation"}:
            continue
        if key in {"transparent_background", "variety_plus"}:
            if not isinstance(value, bool):
                raise ValueError(f"generation config: {key} must be boolean or null")
        elif key in {"steps", "guidance", "guidance_rescale"}:
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or value < 0 or value > 1.7976931348623157e308 or not math.isfinite(value)
                    or (key == "steps" and (not isinstance(value, int) or value < 1))):
                raise ValueError(f"generation config: invalid {key}")
        else:
            result[key] = _text(value, key)
    if result["effort"] == "Medium" and any(
            result[key] is not None for key in ("steps", "sampler", "guidance_rescale")):
        raise ValueError("generation config: Medium effort requires steps, sampler "
                         "and guidance_rescale to be null")
    return GenerationSettings(**result)


def load_generation_profile(path: Path = GENERATION_CONFIG_FILE, *,
                            profile: str | None = None) -> GenerationProfile:
    """Resolve paths relative to the config, never to the caller's cwd.

    Missing files retain legacy defaults. Explicit invalid config is an error.
    `profile` 見 `parse_generation_profile`。
    """
    path = Path(path).resolve()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        data = {}
    else:
        try:
            data = json.loads(text)
        except (ValueError, UnicodeError) as error:
            raise ValueError("generation config: invalid JSON") from error
    return parse_generation_profile(data, path.parent, profile=profile)


def parse_generation_profile(data: object, root: Path, *,
                             profile: str | None = None) -> GenerationProfile:
    """Validate an in-memory config before publishing a profile selection.

    選哪一套：`profile` 引數 → 環境變數 `PROFILE_OVERRIDE_ENV` → `active_profile`。
    指定的名字不在 `profiles` 裡是錯誤（`ValueError`），不會安靜退回預設。
    `active_profile` 本身**照樣驗證**（必須是 `profiles` 裡的一個名字）：它是之後每一次
    沒有指定的啟動要用的預設，壞掉的話應該現在就講，不是等到下一次開機。
    """
    data = _object(data, "root", {"active_profile", "profiles"})
    selected = profile if profile is not None else profile_override()
    if not data:
        if selected not in (None, "legacy"):
            raise ValueError("generation config: selected profile is absent from profiles")
        name, entry = "legacy", {}
    else:
        default = _text(data.get("active_profile"), "active_profile")
        profiles = data.get("profiles")
        if not isinstance(profiles, dict) or default not in profiles:
            raise ValueError("generation config: active_profile is absent from profiles")
        name = default if selected is None else _text(selected, "selected profile")
        if name not in profiles:
            raise ValueError("generation config: selected profile is absent from profiles")
        entry = _object(profiles[name], "profile",
                        {"model_candidates", "settings", "todo", "fallback"})
    candidates = entry.get("model_candidates")
    if candidates is not None:
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("generation config: model_candidates must be a nonempty list")
        candidates = tuple(_text(value, "model_candidates") for value in candidates)
    todo = _paths(entry.get("todo", {}), root, todo=True)
    fallback = _paths(entry.get("fallback", {}), root, todo=False)
    if set(asdict(todo).values()) & set(asdict(fallback).values()):
        raise ValueError("generation config: todo paths cannot overwrite fallback files")
    return GenerationProfile(name, candidates, _settings(entry.get("settings", {})),
                             todo, fallback)


# 行程啟動當下的覆寫值。`require_current_profile` 拿**同一個值**重讀，比的是「同一套設定
# 有沒有被改」，不是「預設值是不是剛好也是這套」——否則一個用 `--profile` 叫醒的行程，
# 每次重讀都會把預設那套當成「設定變了」而拒絕動佇列。
STARTUP_PROFILE_OVERRIDE = profile_override()
GENERATION_PROFILE = load_generation_profile(profile=STARTUP_PROFILE_OVERRIDE)


def require_current_profile(profile: GenerationProfile = GENERATION_PROFILE) -> None:
    try:
        current = load_generation_profile(profile=STARTUP_PROFILE_OVERRIDE)
    except (OSError, ValueError) as error:
        raise GenerationProfileChanged("Generation profile is invalid; stop and restart both processes.") from error
    if current != profile:
        raise GenerationProfileChanged("Generation profile changed; stop the batch and restart both processes.")


def require_expected_profile(identity: str) -> None:
    expected = os.environ.get("WEBRUNNER_GENERATION_IDENTITY")
    if expected and expected != identity:
        raise GenerationProfileChanged("Generation profile differs from the launching process.")


def publish_generation_identity(identity: str) -> None:
    payload = json.dumps({"pid": os.getpid(), "parent_pid": os.getppid(),
                          "generation": identity})
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                     dir=GENERATION_STATE_FILE.parent,
                                     prefix=".generation-", suffix=".tmp") as handle:
        temporary = Path(handle.name)
        handle.write(payload)
    try:
        os.replace(temporary, GENERATION_STATE_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def generation_process_matches(pid: int, identity: str) -> bool:
    try:
        state = json.loads(GENERATION_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(state, dict)
            and pid in (state.get("pid"), state.get("parent_pid"))
            and state.get("generation") == identity)
