"""用途ごとの model / effort を決める。

provider の選択は agent ごとに保持する。ここは選択された provider に対して、
用途ごとの固定 recipe を返すだけであり、別 provider や上位 model への fallback はしない。
コアの用途は下の表、モジュールの用途は module.toml の [use_cases] から引く。使ってよいモデルの一覧は、ここにだけ置く。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from kei_agent import modules
from kei_agent.config import AGENT_PLUGINS, ConfigError, model_actors

PROVIDERS = frozenset({"codex", "claude"})

ALLOWED_MODELS = {
    "codex": frozenset({"gpt-6-luna", "gpt-6-sol", "gpt-6-astra"}),
    "claude": frozenset({"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5", "claude-fable-5"}),
}


class ModelPolicyError(ValueError):
    """選択された provider で安全に実行できない。"""


class UseCase(StrEnum):
    ROUTING = "routing"
    RESEARCH_EXTRACT = "research_extract"
    RESEARCH_SCREEN = "research_screen"
    RESEARCH_COMPARE = "research_compare"
    RESEARCH_EXECUTE = "research_execute"
    RESEARCH_DESIGN = "research_design"
    COURSE_EXPLAIN = "course_explain"
    COURSE_REQUIREMENTS = "course_requirements"
    COURSE_COMPARE = "course_compare"
    COURSE_DEGREE_PLAN = "course_degree_plan"
    WORK_SINGLE_SOURCE = "work_single_source"
    WORK_CROSS_SOURCE = "work_cross_source"
    WORK_DECIDE = "work_decide"
    OVERVIEW_DAILY = "overview_daily"
    OVERVIEW_PLAN = "overview_plan"
    SELF_FIX_DESIGN = "self_fix_design"
    SELF_FIX_IMPLEMENTATION = "self_fix_implementation"
    SELF_FIX_REVIEW = "self_fix_review"
    MANUAL_ASTRA = "manual_astra"
    MANUAL_FABLE = "manual_fable"


@dataclass(frozen=True)
class ResolvedModel:
    actor: str
    # コアの用途は UseCase、モジュールの用途は名前の文字列（UseCase も文字列として比べられる）
    use_case: UseCase | str
    provider: str
    model: str
    reasoning_effort: str
    manual_only: bool = False


# `(model, effort)`: empty Claude effort deliberately means non-thinking.
_RECIPES: dict[tuple[str, UseCase], tuple[str, str]] = {
    ("codex", UseCase.ROUTING): ("gpt-6-luna", "low"),
    ("claude", UseCase.ROUTING): ("claude-haiku-4-5", ""),
    ("codex", UseCase.RESEARCH_EXTRACT): ("gpt-6-luna", "low"),
    ("claude", UseCase.RESEARCH_EXTRACT): ("claude-haiku-4-5", ""),
    ("codex", UseCase.RESEARCH_SCREEN): ("gpt-6-luna", "medium"),
    ("claude", UseCase.RESEARCH_SCREEN): ("claude-haiku-4-5", ""),
    ("codex", UseCase.RESEARCH_COMPARE): ("gpt-6-sol", "medium"),
    ("claude", UseCase.RESEARCH_COMPARE): ("claude-sonnet-5", "medium"),
    ("codex", UseCase.RESEARCH_EXECUTE): ("gpt-6-sol", "high"),
    ("claude", UseCase.RESEARCH_EXECUTE): ("claude-sonnet-5", "high"),
    ("codex", UseCase.RESEARCH_DESIGN): ("gpt-6-sol", "xhigh"),
    ("claude", UseCase.RESEARCH_DESIGN): ("claude-opus-5", "high"),
    ("codex", UseCase.COURSE_EXPLAIN): ("gpt-6-luna", "medium"),
    ("claude", UseCase.COURSE_EXPLAIN): ("claude-sonnet-5", "medium"),
    ("codex", UseCase.COURSE_REQUIREMENTS): ("gpt-6-luna", "high"),
    ("claude", UseCase.COURSE_REQUIREMENTS): ("claude-sonnet-5", "high"),
    ("codex", UseCase.COURSE_COMPARE): ("gpt-6-sol", "medium"),
    ("claude", UseCase.COURSE_COMPARE): ("claude-sonnet-5", "high"),
    ("codex", UseCase.COURSE_DEGREE_PLAN): ("gpt-6-sol", "xhigh"),
    ("claude", UseCase.COURSE_DEGREE_PLAN): ("claude-opus-5", "high"),
    ("codex", UseCase.WORK_SINGLE_SOURCE): ("gpt-6-luna", "medium"),
    ("claude", UseCase.WORK_SINGLE_SOURCE): ("claude-sonnet-5", "medium"),
    ("codex", UseCase.WORK_CROSS_SOURCE): ("gpt-6-sol", "medium"),
    ("claude", UseCase.WORK_CROSS_SOURCE): ("claude-sonnet-5", "high"),
    ("codex", UseCase.WORK_DECIDE): ("gpt-6-sol", "high"),
    ("claude", UseCase.WORK_DECIDE): ("claude-opus-5", "high"),
    ("codex", UseCase.OVERVIEW_DAILY): ("gpt-6-luna", "medium"),
    ("claude", UseCase.OVERVIEW_DAILY): ("claude-sonnet-5", "medium"),
    ("codex", UseCase.OVERVIEW_PLAN): ("gpt-6-sol", "high"),
    ("claude", UseCase.OVERVIEW_PLAN): ("claude-opus-5", "high"),
    ("codex", UseCase.SELF_FIX_DESIGN): ("gpt-6-sol", "xhigh"),
    ("claude", UseCase.SELF_FIX_DESIGN): ("claude-opus-5", "high"),
    ("codex", UseCase.SELF_FIX_IMPLEMENTATION): ("gpt-6-sol", "high"),
    ("claude", UseCase.SELF_FIX_IMPLEMENTATION): ("claude-sonnet-5", "high"),
    ("codex", UseCase.SELF_FIX_REVIEW): ("gpt-6-sol", "medium"),
    ("claude", UseCase.SELF_FIX_REVIEW): ("claude-sonnet-5", "high"),
}

_MANUAL = {
    ("codex", UseCase.MANUAL_ASTRA): ("gpt-6-astra", "xhigh"),
    ("claude", UseCase.MANUAL_FABLE): ("claude-fable-5", "high"),
}

_ACTOR_USE_CASES = {
    "research": frozenset({
        UseCase.RESEARCH_EXTRACT, UseCase.RESEARCH_SCREEN, UseCase.RESEARCH_COMPARE,
        UseCase.RESEARCH_EXECUTE, UseCase.RESEARCH_DESIGN, UseCase.MANUAL_ASTRA,
        UseCase.MANUAL_FABLE,
    }),
    "course": frozenset({
        UseCase.COURSE_EXPLAIN, UseCase.COURSE_REQUIREMENTS, UseCase.COURSE_COMPARE,
        UseCase.COURSE_DEGREE_PLAN,
    }),
    "work": frozenset({
        UseCase.WORK_SINGLE_SOURCE, UseCase.WORK_CROSS_SOURCE, UseCase.WORK_DECIDE,
    }),
    "router": frozenset({UseCase.ROUTING, UseCase.OVERVIEW_DAILY, UseCase.OVERVIEW_PLAN}),
    "self_fix": frozenset({
        UseCase.SELF_FIX_DESIGN, UseCase.SELF_FIX_IMPLEMENTATION, UseCase.SELF_FIX_REVIEW,
    }),
}


def allowed_use_cases(actor: str) -> frozenset[UseCase | str]:
    """actor が通常経路または手動例外で使える use case。モジュールの実行役は module.toml の [use_cases]。"""
    if actor in _ACTOR_USE_CASES:
        return _ACTOR_USE_CASES[actor]
    spec = modules.known().get(actor)
    if spec is None or spec.actor is None:
        raise ModelPolicyError(f"未知の actor です: {actor}")
    return frozenset(u.name for u in spec.actor.use_cases)


def use_case_of(value: UseCase | str) -> UseCase | str:
    """用途の名前を、コアの用途（UseCase）か、モジュールの用途（名前の文字列）にする。知らなければ ModelPolicyError。"""
    try:
        return UseCase(value)
    except ValueError:
        if modules.use_case_owner(str(value)) is not None:
            return str(value)
        raise ModelPolicyError(f"未知の use case です: {value}") from None


def _recipe(provider: str, use_case: UseCase | str) -> tuple[str, str] | None:
    """(model, effort)。コアの表か、その用途を持つモジュールの module.toml から。"""
    if (provider, use_case) in _RECIPES:
        return _RECIPES[(provider, use_case)]
    owner = modules.use_case_owner(str(use_case))
    if owner is None or owner.actor is None:
        return None
    spec = next(u for u in owner.actor.use_cases if u.name == use_case)
    return spec.recipes.get(provider)


def check_module_recipes(spec: modules.ModuleSpec) -> None:
    """モジュールの用途は、コアの用途と名前がぶつからず、モデルはコアの一覧の中にあること（設定を読むときに確かめる）。"""
    for use_case in spec.actor.use_cases if spec.actor else ():
        if use_case.name in UseCase.__members__.values():
            raise ConfigError(f"モジュール「{spec.name}」の用途 {use_case.name} は、コアの用途と同じ名前です")
        for provider, (model, _effort) in use_case.recipes.items():
            if not is_allowed_model(provider, model):
                raise ConfigError(f"モジュール「{spec.name}」の用途 {use_case.name} の {provider} のモデル {model} は使えません"
                                  f"（使えるのは {', '.join(sorted(ALLOWED_MODELS[provider]))}）")


def is_allowed_model(provider: str, model: str) -> bool:
    return model in ALLOWED_MODELS.get(provider, ())


def resolve(actor: str, provider: str, use_case: UseCase | str, *, manual: bool = False) -> ResolvedModel:
    """選択済み provider の recipe だけを返す。"""
    if actor not in model_actors():
        raise ModelPolicyError(f"未知の actor です: {actor}")
    if provider not in PROVIDERS:
        raise ModelPolicyError(f"provider を選んでください: {actor}")
    case = use_case_of(use_case)
    if case not in allowed_use_cases(actor):
        raise ModelPolicyError(f"{actor} では {case} を使えません")
    if (provider, case) in _MANUAL:
        if not manual:
            raise ModelPolicyError(f"{case} は依頼者による手動指定だけで使えます")
        model, effort = _MANUAL[(provider, case)]
        if not is_allowed_model(provider, model):
            raise ModelPolicyError(f"許可されていない model です: {model}")
        return ResolvedModel(actor, case, provider, model, effort, manual_only=True)
    found = _recipe(provider, case)
    if found is None:
        raise ModelPolicyError(f"{actor} では {case} を {provider} で使えません")
    model, effort = found
    if not is_allowed_model(provider, model):
        raise ModelPolicyError(f"許可されていない model です: {model}")
    return ResolvedModel(actor, case, provider, model, effort)


def resolve_selected(config, store, actor: str, use_case: UseCase | str, *, manual: bool = False) -> ResolvedModel:
    """App Home で選択された provider から recipe を解決する。"""
    # settings は Config を import するため、循環 import を避けて遅延 import にする。
    from kei_agent.settings import selected_provider

    return resolve(actor, selected_provider(config, store, actor), use_case, manual=manual)


def resolve_classifier(config, store, actor: str, *, provider: str | None = None) -> ResolvedModel:
    """plugin actor の軽量分類だけに使う routing recipe。

    通常の ``resolve`` は actor 固有の仕事だけを許可する。分類は例外的に routing
    recipe を使うが、実行 actor は依頼の担当のままにして runner の権限境界を保つ。
    """
    if actor not in AGENT_PLUGINS:
        raise ModelPolicyError(f"{actor} は軽量分類を使えません")
    from kei_agent.settings import selected_provider

    provider = provider or selected_provider(config, store, actor)
    if provider not in PROVIDERS:
        raise ModelPolicyError(f"provider を選んでください: {actor}")
    try:
        model, effort = _RECIPES[(provider, UseCase.ROUTING)]
    except KeyError as e:
        raise ModelPolicyError(f"{provider} の分類 recipe がありません") from e
    if not is_allowed_model(provider, model):
        raise ModelPolicyError(f"許可されていない model です: {model}")
    return ResolvedModel(actor, UseCase.ROUTING, provider, model, effort)


def validate_resolved(recipe: ResolvedModel) -> None:
    """runner に渡る recipe が policy から作られた値と完全一致するか確認する。

    ``ResolvedModel`` は dataclass なので、呼び出し元が直接作ること自体は Python では防げない。
    CLI 起動直前に再解決して比べることで、allowlist 内の手動例外を通常用途へ偽装する経路を閉じる。
    plugin actor の ``routing`` だけは分類器専用の軽量例外として同じ固定値を検証する。
    """
    if recipe.use_case is UseCase.ROUTING and recipe.actor in AGENT_PLUGINS:
        expected_values = _RECIPES.get((recipe.provider, UseCase.ROUTING))
        expected = (ResolvedModel(recipe.actor, UseCase.ROUTING, recipe.provider, *expected_values)
                    if expected_values is not None else None)
    else:
        try:
            expected = resolve(recipe.actor, recipe.provider, recipe.use_case, manual=recipe.manual_only)
        except ModelPolicyError as e:
            raise ModelPolicyError(f"許可されない recipe です: {e}") from e
    if expected != recipe:
        raise ModelPolicyError(f"許可されない recipe です: {recipe.actor}/{recipe.use_case}")
