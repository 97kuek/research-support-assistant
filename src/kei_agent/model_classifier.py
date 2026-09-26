"""自由文を、選択済み provider の軽量 recipe で用途分類する。"""

from __future__ import annotations

from kei_agent import modules, runner
from kei_agent.config import Config
from kei_agent.model_json import json_object
from kei_agent.model_policy import ModelPolicyError, UseCase, resolve_classifier
from kei_agent.router import workspace

_RESEARCH_CASES = frozenset({
    UseCase.RESEARCH_EXTRACT, UseCase.RESEARCH_SCREEN, UseCase.RESEARCH_COMPARE,
    UseCase.RESEARCH_EXECUTE, UseCase.RESEARCH_DESIGN,
})
_COURSE_CASES = frozenset({UseCase.COURSE_EXPLAIN, UseCase.COURSE_REQUIREMENTS,
                           UseCase.COURSE_COMPARE, UseCase.COURSE_DEGREE_PLAN})
_WORK_CASES = frozenset({UseCase.WORK_SINGLE_SOURCE, UseCase.WORK_CROSS_SOURCE, UseCase.WORK_DECIDE})


class UsageLimited(RuntimeError):
    """分類に使った provider の quota が尽きた。別 recipe で再試行してはいけない。"""

    def __init__(self, reset_at: float):
        self.reset_at = reset_at
        super().__init__("軽量分類器の利用上限に達しました")


def parse(text: str, allowed: frozenset[UseCase] = _RESEARCH_CASES) -> UseCase | None:
    """形式不正・低信頼は None。呼び出し側が通常 recipe へ安全に戻す。"""
    try:
        data = json_object(text, "use_case")
    except ValueError:
        return None
    try:
        confidence = float(data.get("confidence"))
        use_case = UseCase(str(data.get("use_case") or ""))
    except (TypeError, ValueError, AttributeError):
        return None
    return use_case if confidence >= 0.8 and use_case in allowed else None


async def classify_research(config: Config, store, prompt: str, *, provider: str | None = None) -> UseCase:
    """研究 provider の Luna/Haiku recipe で一度だけ分類する。

    分類用の workspace は connector なし・read-only。失敗時に別 provider/上位 model では再試行しない。
    """
    return await _classify(config, store, "research", prompt, _RESEARCH_CASES, UseCase.RESEARCH_EXECUTE,
                           "research_extract, research_screen, research_compare, research_execute, research_design",
                           "書誌・固定項目・ログの抽出は extract、明確な基準の候補仕分けは screen、比較・結果分析は compare、"
                           "実験コード・データ処理・通常調査は execute、仮説・実験計画・手法選択・厳密レビューは design。", provider=provider)


async def classify_course(config: Config, store, prompt: str, *, provider: str | None = None) -> UseCase:
    return await _classify(config, store, "course", prompt, _COURSE_CASES, UseCase.COURSE_EXPLAIN,
                           "course_explain, course_requirements, course_compare, course_degree_plan",
                           "1資料の説明は explain、課題要件・評価基準の整理は requirements、複数資料や試験範囲の比較は compare、"
                           "履修・卒業計画の選択肢提案は degree_plan。", provider=provider)


async def classify_work(config: Config, store, prompt: str, *, provider: str | None = None) -> UseCase:
    return await _classify(config, store, "work", prompt, _WORK_CASES, UseCase.WORK_SINGLE_SOURCE,
                           "work_single_source, work_cross_source, work_decide",
                           "1件のメール・資料の要点は single_source、複数メール・予定・資料の状況要約は cross_source、"
                           "優先順位・会議準備・論点整理は decide。", provider=provider)


CLASSIFIERS = {"research": classify_research, "course": classify_course, "work": classify_work}


async def classify(config: Config, store, actor: str, prompt: str, *,
                   provider: str | None = None) -> UseCase | str:
    """担当の用途を分類する（研究・大学・仕事で同じ呼び方）。モジュールの実行役は、分類器を動かさずに
    module.toml の default_use_case にする。"""
    if actor in CLASSIFIERS:
        return await CLASSIFIERS[actor](config, store, prompt, provider=provider)
    spec = modules.known().get(actor)
    if spec is None or spec.actor is None:
        raise KeyError(actor)
    return spec.actor.default_use_case


async def _classify(config: Config, store, actor: str, prompt: str, allowed: frozenset[UseCase],
                    fallback: UseCase, candidates: str, guidance: str, *, provider: str | None = None) -> UseCase:
    try:
        recipe = resolve_classifier(config, store, actor, provider=provider)
    except ModelPolicyError:
        return fallback
    classifier_prompt = ("次の依頼をユースケースに分類してください。JSON 1行だけで答えてください。"
                         "形: {\"use_case\":\"候補名\",\"confidence\":0.0から1.0}。\n"
                         f"候補は {candidates}。\n{guidance}\n"
                         f"迷うときは {fallback.value} と confidence を 0.7 未満にしてください。\n\n依頼:\n{prompt[:1200]}")
    try:
        result = await runner.run_model(
            config, runner.ExecutionRequest(workspace(config, actor), recipe, None, "", "", read_only=True),
            classifier_prompt,
        )
    except Exception:
        # 分類器の障害で利用者の依頼自体を落とさない。再試行・昇格はしない。
        return fallback
    if result.limit_reset_at is not None:
        raise UsageLimited(result.limit_reset_at)
    return parse(result.text, allowed) or fallback
