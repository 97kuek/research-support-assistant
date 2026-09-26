from __future__ import annotations

import pytest

from kei_agent.model_policy import ModelPolicyError, UseCase, resolve


def test_research_execution_uses_provider_specific_recipes():
    codex = resolve("research", "codex", UseCase.RESEARCH_EXECUTE)
    claude = resolve("research", "claude", UseCase.RESEARCH_EXECUTE)

    assert (codex.model, codex.reasoning_effort) == ("gpt-6-sol", "high")
    assert (claude.model, claude.reasoning_effort) == ("claude-sonnet-5", "high")


def test_router_uses_lightweight_recipe_without_claude_thinking():
    claude = resolve("router", "claude", UseCase.ROUTING)

    assert (claude.model, claude.reasoning_effort) == ("claude-haiku-4-5", "")


def test_normal_use_cases_cannot_select_manual_top_model():
    with pytest.raises(ModelPolicyError, match="手動指定"):
        resolve("research", "codex", UseCase.MANUAL_ASTRA)


def test_owner_explicit_manual_label_can_select_the_exception(config, store):
    from kei_agent import research, settings
    from kei_agent.model_policy import resolve_selected

    settings.set_agent_provider(store, "research", "codex")
    use_case, prompt = research.use_case_for_prompt("[[manual-astra]] 厳密な反証レビューをして")

    recipe = resolve_selected(config, store, "research", use_case, manual=research.is_manual_use_case(use_case))
    assert prompt == "厳密な反証レビューをして"
    assert (recipe.model, recipe.reasoning_effort, recipe.manual_only) == ("gpt-6-astra", "xhigh", True)


def test_unknown_actor_provider_or_use_case_is_rejected():
    with pytest.raises(ModelPolicyError, match="provider"):
        resolve("research", "", UseCase.RESEARCH_EXECUTE)
    with pytest.raises(ModelPolicyError, match="actor"):
        resolve("unknown", "codex", UseCase.RESEARCH_EXECUTE)


@pytest.mark.parametrize(("actor", "case"), [
    ("router", UseCase.RESEARCH_EXECUTE),
    ("work", UseCase.COURSE_REQUIREMENTS),
    ("course", UseCase.MANUAL_ASTRA),
])
def test_resolve_rejects_use_case_outside_actor_policy(actor, case):
    with pytest.raises(ModelPolicyError):
        resolve(actor, "codex", case, manual=True)


def test_manual_fable_requires_research_and_claude():
    with pytest.raises(ModelPolicyError):
        resolve("research", "codex", UseCase.MANUAL_FABLE, manual=True)


@pytest.mark.parametrize("provider, model", [
    ("codex", "gpt-6-luna"),
    ("codex", "gpt-6-sol"),
    ("codex", "gpt-6-astra"),
    ("claude", "claude-haiku-4-5"),
    ("claude", "claude-sonnet-5"),
    ("claude", "claude-opus-5"),
    ("claude", "claude-fable-5"),
])
def test_only_approved_models_are_allowlisted(provider, model):
    from kei_agent.model_policy import is_allowed_model

    assert is_allowed_model(provider, model)
    assert not is_allowed_model(provider, "gpt-5.6-terra")


def test_config_rejects_old_model_override_and_defaults_to_unselected_provider(tmp_path):
    from kei_agent.config import ConfigError, load_config

    old = tmp_path / "old.toml"
    old.write_text('[agents.research]\nprovider = "codex"\nmodel = "gpt-5.6-terra"\n')
    with pytest.raises(ConfigError, match="知らないキー"):
        load_config(old, env={})

    (tmp_path / "empty.toml").write_text("")
    config = load_config(tmp_path / "empty.toml", env={})
    assert config.agent_profiles["research"].provider == ""
    assert config.agent_profiles["router"].provider == ""


def test_selected_provider_resolves_its_fixed_recipe(config, store):
    from kei_agent import settings
    from kei_agent.model_policy import resolve_selected

    settings.set_agent_provider(store, "research", "codex")
    recipe = resolve_selected(config, store, "research", UseCase.RESEARCH_EXECUTE)

    assert (recipe.provider, recipe.model, recipe.reasoning_effort) == ("codex", "gpt-6-sol", "high")


def test_lightweight_classifier_requires_valid_high_confidence_json():
    from kei_agent.model_classifier import parse

    assert parse('{"use_case":"research_extract","confidence":0.9}') is UseCase.RESEARCH_EXTRACT
    assert parse('{"use_case":"research_design","confidence":0.7}') is None
    assert parse('{"use_case":"unknown","confidence":1}') is None


def test_lightweight_classifier_restricts_each_actor_to_its_own_cases():
    from kei_agent.model_classifier import _COURSE_CASES, _WORK_CASES, parse

    assert parse('{"use_case":"course_requirements","confidence":0.9}', _COURSE_CASES) \
        is UseCase.COURSE_REQUIREMENTS
    assert parse('{"use_case":"work_decide","confidence":0.9}', _COURSE_CASES) is None
    assert parse('{"use_case":"work_decide","confidence":0.9}', _WORK_CASES) is UseCase.WORK_DECIDE


async def test_classifier_stops_on_a_provider_usage_limit(config, store, monkeypatch):
    from kei_agent import model_classifier, runner, settings

    settings.set_agent_provider(store, "research", "claude")

    async def limited(*_args, **_kwargs):
        return runner.RunResult(is_error=True, limit_reset_at=123.0, errors=["usage limit reached"])

    monkeypatch.setattr(runner, "run_model", limited)
    with pytest.raises(model_classifier.UsageLimited) as raised:
        await model_classifier._classify(
            config, store, "research", "実験ログを見て", model_classifier._RESEARCH_CASES,
            UseCase.RESEARCH_EXECUTE, "research_extract", "抽出は extract。")
    assert raised.value.reset_at == 123.0


async def test_each_classifier_runs_in_its_own_directory(config, store, monkeypatch):
    """research と course の分類が同時に走っても、skill の置き場を取り合わない。"""
    from kei_agent import model_classifier, router, runner, settings

    seen = []

    async def record(_config, request, _prompt, *_args, **_kwargs):
        seen.append(request.workspace.cwd)
        return runner.RunResult(text='{"use_case":"research_extract","confidence":0.9}')

    monkeypatch.setattr(runner, "run_model", record)
    for actor in ("research", "course"):
        settings.set_agent_provider(store, actor, "codex")
    # conftest が差し替えた classify_research ではなく、本物の _classify を通す
    await model_classifier._classify(config, store, "research", "ログを見て", model_classifier._RESEARCH_CASES,
                                     UseCase.RESEARCH_EXECUTE, "", "")
    await model_classifier._classify(config, store, "course", "課題の要件", model_classifier._COURSE_CASES,
                                     UseCase.COURSE_EXPLAIN, "", "")

    assert len(set(seen + [router.workspace(config).cwd])) == 3


def test_every_actor_use_case_has_a_recipe_on_both_providers():
    """Claude でも Codex でも同じ担当が動く（知識の担当を足したときに、片方だけ忘れないように）。"""
    from kei_agent.config import model_actors
    from kei_agent.model_policy import allowed_use_cases
    from kei_agent.research import is_manual_use_case

    for actor in model_actors():
        for use_case in allowed_use_cases(actor):
            if is_manual_use_case(use_case):
                continue
            for provider in ("claude", "codex"):
                assert resolve(actor, provider, use_case).provider == provider
