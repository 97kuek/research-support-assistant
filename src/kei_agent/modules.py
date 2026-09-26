"""モジュール（機能のまとまり）の定義 `module.toml` を読む（docs/extensibility.md）。

組み込みのモジュールはリポジトリ直下の `modules/<名前>/`、利用者のモジュールは `~/.config/kei-agent/modules/<名前>/`。
どちらも同じ形で読む。ここでは動き（module.py）は読み込まず、変わらない事実だけを読んで確かめる。

コアのほかの部品（設定・モデル・制限の表）がここを読むので、ここからは kei_agent のどこも読み込まない。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

# この Kei Agent が読める枠の版。枠（module.toml の形と core の窓口）を変えるときに上げる
API_VERSION = 1
SPEC_FILE = "module.toml"
BUILTIN_DIR = Path(__file__).resolve().parents[2] / "modules"
PROVIDERS = ("claude", "codex")
ACCESS = ("none", "read", "write")
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_USE_CASE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_TOP_KEYS = {"api", "name", "label", "description", "depends", "actor", "use_cases", "process", "channels",
             "schedules"}
_DEPENDS_KEYS = {"requires", "optional"}
_ACTOR_KEYS = {"prompt", "plugin", "files", "shell", "web", "notion", "timeout_minutes", "default_use_case"}
_USE_CASE_KEYS = {"offline", *PROVIDERS}
_RECIPE_KEYS = {"model", "effort"}
_PROCESS_KEYS = {"port"}
_SCHEDULE_KEYS = {"label", "short", "default"}


class ModuleError(ValueError):
    """module.toml が読めない、形が違う、枠の版が合わない、ほかのモジュールとぶつかる。"""


@dataclass(frozen=True)
class UseCaseSpec:
    name: str
    # Web を使わない回（外の文を材料として渡す回。docs/architecture.md の「知識」）
    offline: bool
    # provider → (model, effort)。model が使ってよいものかは、コアのモデルの一覧（model_policy）で確かめる
    recipes: dict[str, tuple[str, str]]


@dataclass(frozen=True)
class ActorSpec:
    """AI の実行役。provider は App Home で選び、どこまで触れるかはここに書いたものが制限の表の行になる。"""
    prompt: str
    plugin: bool
    files: str
    shell: bool
    web: bool
    notion: str
    timeout_minutes: int | None
    # 自由な質問の用途（分類器を動かさない）
    default_use_case: str
    use_cases: tuple[UseCaseSpec, ...]


@dataclass(frozen=True)
class ScheduleSpec:
    name: str
    label: str
    short: str
    default: str


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    label: str
    description: str
    path: Path
    builtin: bool
    requires: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    actor: ActorSpec | None = None
    # 担当プロセス（A2A のサーバー）の番地。持たないモジュールは None
    port: int | None = None
    # チャンネルの種類 → 既定の名前（番号を外した名前。設定の [channels] で変えられる）
    channels: dict[str, tuple[str, ...]] = field(default_factory=dict)
    schedules: tuple[ScheduleSpec, ...] = ()


def _check_keys(data: dict, known: set[str], where: str) -> None:
    unknown = sorted(set(data) - known)
    if unknown:
        raise ModuleError(f"{where} に知らないキーがあります: {', '.join(unknown)}（使えるキー: {', '.join(sorted(known))}）")


def _table(data: dict, key: str, where: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ModuleError(f"{where} の {key} はテーブルにしてください")
    return value


def _names(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ModuleError(f"{where} は文字列の配列にしてください")
    return tuple(value)


def _use_cases(data: dict, where: str) -> tuple[UseCaseSpec, ...]:
    found = []
    for name, spec in data.items():
        at = f"{where} の [use_cases.{name}]"
        if not _USE_CASE.match(name) or not isinstance(spec, dict):
            raise ModuleError(f"{at}: 用途の名前は英小文字と _ で、中身はテーブルにしてください")
        _check_keys(spec, _USE_CASE_KEYS, at)
        recipes = {}
        for provider in PROVIDERS:
            recipe = spec.get(provider)
            if recipe is None:
                continue
            if not isinstance(recipe, dict) or not isinstance(recipe.get("model"), str):
                raise ModuleError(f"{at} の {provider} は {{ model = \"…\", effort = \"…\" }} の形にしてください")
            _check_keys(recipe, _RECIPE_KEYS, f"{at} の {provider}")
            recipes[provider] = (recipe["model"], str(recipe.get("effort", "")))
        if not recipes:
            raise ModuleError(f"{at}: claude か codex の、少なくとも片方のモデルを書いてください")
        found.append(UseCaseSpec(name, bool(spec.get("offline", False)), recipes))
    return tuple(found)


def _actor(data: dict, use_cases: tuple[UseCaseSpec, ...], where: str) -> ActorSpec:
    at = f"{where} の [actor]"
    _check_keys(data, _ACTOR_KEYS, at)
    for key in ("files", "notion"):
        if data.get(key, "none") not in ACCESS:
            raise ModuleError(f"{at} の {key} は {' / '.join(ACCESS)} のどれかにしてください")
    if not use_cases:
        raise ModuleError(f"{at}: 実行役には、少なくとも1つの [use_cases.<名前>] が要ります")
    default = str(data.get("default_use_case") or use_cases[0].name)
    if default not in {u.name for u in use_cases}:
        raise ModuleError(f"{at} の default_use_case（{default}）が [use_cases] にありません")
    timeout = data.get("timeout_minutes")
    if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
        raise ModuleError(f"{at} の timeout_minutes は正の整数にしてください")
    prompt = str(data.get("prompt") or "")
    if not prompt.endswith(".md"):
        raise ModuleError(f"{at} の prompt は指示書のファイル名（例: knowledge.md）にしてください")
    return ActorSpec(prompt=prompt, plugin=bool(data.get("plugin", False)), files=data.get("files", "none"),
                     shell=bool(data.get("shell", False)), web=bool(data.get("web", False)),
                     notion=data.get("notion", "none"), timeout_minutes=timeout, default_use_case=default,
                     use_cases=use_cases)


def _schedules(data: dict, where: str) -> tuple[ScheduleSpec, ...]:
    found = []
    for name, spec in data.items():
        at = f"{where} の [schedules.{name}]"
        if not _USE_CASE.match(name) or not isinstance(spec, dict):
            raise ModuleError(f"{at}: 定期処理の名前は英小文字と _ で、中身はテーブルにしてください")
        _check_keys(spec, _SCHEDULE_KEYS, at)
        default = str(spec.get("default", ""))
        if default and not _HHMM.match(default):
            raise ModuleError(f"{at} の default は HH:MM か、空文字（既定では動かさない）にしてください")
        label = str(spec.get("label") or name)
        found.append(ScheduleSpec(name, label, str(spec.get("short") or label), default))
    return tuple(found)


def load_spec(directory: Path, builtin: bool = False) -> ModuleSpec:
    """1つのモジュールの定義を読んで確かめる。"""
    path = directory / SPEC_FILE
    where = str(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ModuleError(f"{where} を読めません: {e}") from None
    _check_keys(data, _TOP_KEYS, where)
    if data.get("api") != API_VERSION:
        raise ModuleError(f"{where}: 枠の版が合いません（このモジュールは api = {data.get('api')!r}、"
                          f"この Kei Agent は api = {API_VERSION}）。CHANGELOG の直し方を見てください")
    name = str(data.get("name") or "")
    if not _NAME.match(name) or name != directory.name:
        raise ModuleError(f"{where}: name は英小文字・数字・- で、フォルダの名前（{directory.name}）と同じにしてください")
    depends = _table(data, "depends", where)
    _check_keys(depends, _DEPENDS_KEYS, f"{where} の [depends]")
    use_cases = _use_cases(_table(data, "use_cases", where), where)
    actor = _actor(data["actor"], use_cases, where) if "actor" in data else None
    if use_cases and actor is None:
        raise ModuleError(f"{where}: [use_cases] は [actor]（AI の実行役）といっしょに書いてください")
    process = _table(data, "process", where)
    _check_keys(process, _PROCESS_KEYS, f"{where} の [process]")
    port = process.get("port")
    if process and (not isinstance(port, int) or not 1024 <= port <= 65535):
        raise ModuleError(f"{where} の [process] port は 1024〜65535 の整数にしてください")
    channels = {kind: _names(names, f"{where} の [channels] {kind}")
                for kind, names in _table(data, "channels", where).items()}
    return ModuleSpec(
        name=name, label=str(data.get("label") or name), description=str(data.get("description") or ""),
        path=directory, builtin=builtin,
        requires=_names(depends.get("requires", []), f"{where} の requires"),
        optional=_names(depends.get("optional", []), f"{where} の optional"),
        actor=actor, port=port, channels=channels,
        schedules=_schedules(_table(data, "schedules", where), where))


def discover(directory: Path, builtin: bool = False) -> dict[str, ModuleSpec]:
    """そのフォルダの下の、module.toml を持つフォルダを全部読む。"""
    if not directory.is_dir():
        return {}
    return {child.name: load_spec(child, builtin) for child in sorted(directory.iterdir())
            if (child / SPEC_FILE).is_file()}


@cache
def builtin() -> dict[str, ModuleSpec]:
    """組み込みのモジュール（リポジトリ直下の modules/）。"""
    return discover(BUILTIN_DIR, builtin=True)


_user: dict[str, ModuleSpec] = {}


def register_user_modules(directory: Path) -> dict[str, ModuleSpec]:
    """利用者のモジュール（~/.config/kei-agent/modules/）を読んで、知っているモジュールに足す。設定を読むときに呼ぶ。

    設定がリポジトリ直下にあるとき（例の設定を読むときなど）は、そこの modules/ は組み込みなので読まない。
    """
    found = {} if directory.resolve() == BUILTIN_DIR.resolve() else discover(directory)
    for name, spec in found.items():
        if name in builtin():
            raise ModuleError(f"{spec.path}: 組み込みのモジュール「{name}」と同じ名前です。別の名前にしてください")
    _user.clear()
    _user.update(found)
    _check_collisions(known())
    return found


def known() -> dict[str, ModuleSpec]:
    """知っているモジュール（組み込みと、読んだ利用者のもの）。オンになっているかは設定の modules で決まる。"""
    return {**builtin(), **_user}


def _check_collisions(specs: dict[str, ModuleSpec]) -> None:
    """用途・定期処理・チャンネルの種類・番地は、モジュールどうしでぶつからないこと。"""
    seen: dict[str, str] = {}
    for spec in specs.values():
        keys = [f"用途「{u.name}」" for u in (spec.actor.use_cases if spec.actor else ())]
        keys += [f"定期処理「{s.name}」" for s in spec.schedules]
        keys += [f"チャンネルの種類「{kind}」" for kind in spec.channels]
        keys += [f"番地「{spec.port}」"] if spec.port else []
        for key in keys:
            if key in seen and seen[key] != spec.name:
                raise ModuleError(f"モジュール「{seen[key]}」と「{spec.name}」の{key}がぶつかっています")
            seen[key] = spec.name


def enabled(names) -> list[ModuleSpec]:
    """設定の modules の順に、知っているモジュールの定義を並べる（知らない名前は、設定を読むときに断ってある）。"""
    specs = known()
    return [specs[name] for name in names if name in specs]


def use_case_owner(use_case: str) -> ModuleSpec | None:
    """その用途を持つモジュール。コアの用途なら None。"""
    for spec in known().values():
        if spec.actor and any(u.name == use_case for u in spec.actor.use_cases):
            return spec
    return None
