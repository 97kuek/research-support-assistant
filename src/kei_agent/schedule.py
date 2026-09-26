"""決まった時刻の処理: 先行研究の新着と読みもの（知識の担当）、Daily、Retro & Planning、🌙 の夜間 Task、放置されたスレッドへの声かけ。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import socket
import time
from contextlib import suppress
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import aiohttp

from kei_agent import (
    course,
    dates,
    digest,
    knowledge,
    maintenance,
    modules,
    morning,
    research,
    settings,
    themes,
    timelog,
    version,
    work,
)
from kei_agent.assistant import Assistant
from kei_agent.calendar_sync import (
    JST,
    CalendarItem,
    CalendarSnapshot,
    IncompleteSnapshot,
    outlook_items,
    sync_calendar,
)
from kei_agent.config import Config
from kei_agent.model_policy import UseCase
from kei_agent.notion import NotionError
from kei_agent.notion_store import Note, Task, parse_slack_permalink, summarize
from kei_agent.request import Request
from kei_agent.slack_text import AWAITING_MARKER, clean_text, escape, format_duration
from kei_agent.store import Store

log = logging.getLogger(__name__)

# 実行する順番。夜間の Task の結果を Daily に載せるため、night を先にする
def task_names(config: Config) -> tuple[str, ...]:
    """実行する順。同じ時刻なら、夜間の Task → モジュールの処理（朝の読みものなど）→ Daily → 振り返り → 保守。

    夜間の Task とモジュールの処理の結果を、Daily と朝の一覧に載せるため。
    """
    return ("night", *(s.name for s in settings.module_schedules(config)), "daily", "review", "maintenance")
# 夜間の Task は、朝に Mac が起きたときにも実行する
NIGHT_CATCH_UP_HOURS = 12
# 締切が近いものを知らせるために、カレンダーを見に行く間隔（秒）
DUE_CHECK_SECONDS = 3600
# 取り込んだ新しい版で起動し直したかを見る間隔（秒）。見つけてから、もう一度この時間たっても古ければ知らせる
VERSION_CHECK_SECONDS = 3600
# 「一度だけ知らせた」目印を残す日数（学期の終わりまで持たなくてよい）
NOTICE_RETENTION_DAYS = 60
# 声のレイヤに渡す日数。「明日の予定」「今週の予定」に答えられるように1週間ぶん
VOICE_DAYS = 7
HUB_SYNC_HOUR = 8
HUB_RETRY_SECONDS = 3600
# 予定カレンダーに載せる課題の日数。課題 DB を読める上限で、これからの課題を全部載せる
COURSE_CALENDAR_DAYS = 400
# レトプラのスレッドに並べる締切（明日・明後日まで）
REVIEW_DUE_DAYS = 2
# Daily と Retro & Planning の材料は、ファイルにせずプロンプトのこの間に入れる
MATERIAL_START = "--- 材料ここから ---"
MATERIAL_END = "--- 材料ここまで ---"
# 振り返るときの問い（Codex のアプリなどで振り返る材料）。Slack には出さず、日別記録のレトプラにだけ残す
REVIEW_QUESTIONS = ("### 振り返りの問い\n"
                    "1. 今日分かったことは何か（〜について、など具体的に）\n"
                    "2. 明日やることは何か")


def due_day(now: datetime, hhmm: str, catch_up_hours: float) -> str | None:
    """now が、その日（または前日）の hhmm から catch_up_hours 以内なら、その日付を返す。"""
    if not hhmm:
        return None
    hour, minute = (int(x) for x in hhmm.split(":"))
    for offset in (0, -1):
        day = now.date() + timedelta(days=offset)
        at = datetime.combine(day, dtime(hour, minute))
        if at <= now <= at + timedelta(hours=catch_up_hours):
            return day.isoformat()
    return None


def offline(error: BaseException) -> bool:
    """ネットにつながらない（名前を引けない、つながらない、待ちきれない）ときの例外か。"""
    return isinstance(error, (aiohttp.ClientConnectionError, ConnectionError, TimeoutError, socket.gaierror))


def label(day: str) -> str:
    return dates.day_label(date.fromisoformat(day))


def search_keywords(claude_md: Path) -> list[str]:
    """テーマの CLAUDE.md の「## 検索キーワード」の箇条書きを読む。"""
    if not claude_md.exists():
        return []
    text = re.sub(r"<!--.*?-->", "", claude_md.read_text(encoding="utf-8"), flags=re.DOTALL)
    m = re.search(r"^## 検索キーワード\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    return [line.strip()[2:].strip() for line in m.group(1).splitlines()
            if line.strip().startswith("- ") and line.strip()[2:].strip()]


class Scheduler:
    def __init__(self, config: Config, store: Store, assistant: Assistant):
        self.config = config
        self.store = store
        self.assistant = assistant
        # 締切が近いものを最後に見に行った時刻（起動直後に1回見る）
        self._due_checked = 0.0
        # 取り込んだ新しい版と、それを最初に見つけた時刻
        self._version_checked = 0.0
        self._newer: tuple[str, float] | None = None
        self._hub_calendar_checked = 0.0
        # ネットにつながらなくなった時刻（つながっている間は None）
        self._offline_since: float | None = None

    @property
    def overview_channel_name(self) -> str:
        return self.config.overview_channels[0]

    # ループ

    async def loop(self) -> None:
        while True:
            await self.safe_tick(datetime.now())
            await asyncio.sleep(60)

    async def safe_tick(self, now: datetime) -> None:
        """1分ごとの tick。ネットにつながらない間は、毎分の長いエラーの代わりに、始めと終わりを1行ずつ残す。"""
        try:
            await self.tick(now)
        except Exception as e:
            if not offline(e):
                log.exception("定期処理に失敗しました")
            elif self._offline_since is None:
                self._offline_since = time.time()
                log.warning("ネットにつながらないので、定期処理はつながるまで待ちます: %s: %s", type(e).__name__, e)
            return
        if self._offline_since is not None:
            log.info("ネットにつながったので、定期処理を続けます（%s止まっていました）",
                     format_duration(time.time() - self._offline_since))
            self._offline_since = None

    async def tick(self, now: datetime) -> None:
        sched = self.config.schedule
        if not sched.enabled:
            return
        await self.catch_up_deferred(now.timestamp())
        pending = {(payload.get("name"), payload.get("day"))
                   for _, payload in self.store.pending_deferred("schedule")}
        for name in task_names(self.config):
            catch_up = NIGHT_CATCH_UP_HOURS if name == "night" else sched.catch_up_hours
            # Slack（App Home）で変えた時刻を毎回読み直す。止めている処理は空文字
            hhmm = settings.schedule_time(self.config, self.store, name)
            day = due_day(now, hhmm, catch_up)
            if day is None or self.store.schedule_ran(name, day) or (name, day) in pending:
                continue
            # 実行中に次の tick で二重に動かないよう、先に記録する
            self.store.record_schedule(name, day, {"status": "running"})
            await self.run_or_defer(name, day, now.timestamp())
        hub_day = now.date().isoformat()
        if (not self.store.pending_deferred("schedule")
                and now.hour >= HUB_SYNC_HOUR and not self.store.schedule_ran("hub_calendar", hub_day)
                and now.timestamp() - self._hub_calendar_checked >= HUB_RETRY_SECONDS):
            self._hub_calendar_checked = now.timestamp()
            detail = await self.run_task("hub_calendar", hub_day, record=False)
            if isinstance(detail, dict) and detail.get("course") == "synced":
                self.store.record_schedule("hub_calendar", hub_day, detail)
        await self.notify_due_soon(now)
        await self.nudge_stale_threads()
        await self.notify_unrestarted(now)

    async def run_task(self, name: str, day: str, record: bool = True) -> dict:
        log.info("定期処理を始めます: %s（%s）", name, day)
        try:
            detail = await getattr(self, f"run_{name}")(day)
        except Exception as e:
            log.exception("定期処理 %s が失敗しました", name)
            detail = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        if record:
            self.store.record_schedule(name, day, detail)
        return detail

    def task_provider(self, name: str) -> str | None:
        """定期処理が使う明示 provider。保守はモデルを使わない。"""
        if name == "maintenance":
            return None
        if name in ("daily", "review"):
            return settings.selected_provider(self.config, self.store, "router")
        owner = next((spec for spec in modules.enabled(self.config.modules) if any(s.name == name for s in spec.schedules)),
                     None)
        if owner is not None:
            # モジュールの処理は、そのモジュールの実行役の provider（AI を使わないモジュールなら要らない）
            return settings.selected_provider(self.config, self.store, owner.name) if owner.actor else None
        return settings.selected_provider(self.config, self.store, "research")

    def can_run(self, name: str, now: float, provider: str | None = None) -> bool:
        provider = self.task_provider(name) if provider is None else provider
        return provider is None or bool(provider) and self.store.limit_until(provider) <= now

    async def run_or_defer(self, name: str, day: str, now: float) -> bool:
        """実行する。途中で契約の上限に当たったら、その日の分として残さず、明けてからやり直す。"""
        provider = self.task_provider(name)
        if provider == "":
            self.store.record_schedule(name, day, {"status": "provider_unselected"})
            return False
        if not self.can_run(name, now, provider):
            until = self.store.limit_until(provider)
        else:
            await self.run_task(name, day)
            until = self.store.limit_until(provider) if provider else 0.0
        if until > now:
            log.info("上限に当たったので、%s（%s）は明けてからやり直します", name, day)
            self.store.forget_schedule(name, day)
            self.store.defer_run("schedule", {"name": name, "day": day, "provider": provider}, until)
            return False
        return True

    async def catch_up_deferred(self, now: float) -> None:
        """上限で止まった決まった時刻の処理を、明けてからやり直す（猶予の時間を過ぎていても動かす）。"""
        for deferred_id, payload in self.store.due_deferred("schedule", now):
            self.store.finish_deferred(deferred_id)
            if not self.store.schedule_ran(payload["name"], payload["day"]):
                await self.run_or_defer(payload["name"], payload["day"], now)

    # 夜間の Task

    async def run_night(self, day: str) -> dict:
        notion = self.assistant.notion
        if notion is None:
            return {"status": "no_notion"}
        try:
            tasks = await asyncio.to_thread(notion.tonight_tasks, self.config.schedule.night_max_tasks)
        except NotionError as e:
            await self.assistant.notify_trouble(f"夜間の Task を Notion から読めなかったので、今夜は実行しません: {e}")
            return {"status": "error", "error": str(e)}
        ids = await self.assistant.channel_ids()
        done = []
        for task in tasks:
            try:
                done.append(await self._run_night_task(task, ids))
            except Exception as e:
                # 「実行中」のまま残ると二度と実行されないので、確認待ちに戻して知らせる
                log.exception("夜間の Task「%s」が止まりました", task.title)
                reason = f"途中で止まりました: {type(e).__name__}: {e}"
                await self.assistant.notify_trouble(f"夜間の Task「{task.title}」が{reason}")
                with suppress(NotionError):
                    await asyncio.to_thread(notion.update_task, task.id, "確認待ち", reason)
                done.append({"title": task.title, "status": "error", "reason": reason, "url": task.url})
        try:
            remaining = await asyncio.to_thread(notion.count_tonight_tasks)
        except NotionError:
            remaining = None
        return {"status": "done", "tasks": done, "remaining": remaining}

    async def _run_night_task(self, task: Task, ids: dict[str, str]) -> dict:
        notion = self.assistant.notion
        info = {"title": task.title, "url": task.url, "theme": ", ".join(task.theme_names)}
        theme = task.theme_names[0] if task.theme_names else None
        channel_name = theme
        if theme is None or channel_name not in ids:
            reason = "テーマを設定してください" if theme is None else f"テーマのチャンネル #{theme} に Kei Agent がいません"
            await asyncio.to_thread(notion.update_task, task.id, "確認待ち", reason)
            return {**info, "status": "確認待ち", "reason": reason}

        await asyncio.to_thread(notion.update_task, task.id, "実行中")
        body = await asyncio.to_thread(notion.page_markdown, task.id)
        channel = ids[channel_name]
        source = parse_slack_permalink(task.slack_url)
        message: dict = {}
        # 元のメッセージがテーマのチャンネルにあるときだけ、そのスレッドで続ける
        if source and source[0] == channel:
            message = await self.assistant.fetch_message(*source) or {}
        if message:
            message_ts = source[1]
            thread_ts = message.get("thread_ts") or message_ts
        else:
            message_ts = None
            resp = await self.assistant.slack.chat_postMessage(channel=channel, text=f"🌙 Task: {task.title}")
            thread_ts = resp["ts"]
            await asyncio.to_thread(notion.update_task, task.id, None, None,
                                    await self.assistant.permalink(channel, thread_ts))

        text = (
            "[🌙 夜間の Task] 依頼者は寝ているので、その場で聞き返せません。"
            "判断が必要なところまで進めたら、最後の行を「❓ 確認:」で始めて止めてください。\n\n"
            f"タイトル: {task.title}\n優先度: {task.priority or '-'} / 期日: {task.due or '-'}\n"
            f"Notion: {task.url}\n\n## 本文\n\n{body or '（なし）'}\n"
        )
        if message:
            text += f"\n## 元の Slack のメッセージ\n\n{clean_text(message.get('text', ''))}\n"
        req = Request(channel, channel_name, thread_ts, None, text, trigger="night", files=message.get("files") or [])
        result = await self.assistant.process(req)

        if result is None or result.is_error:
            status = "確認待ち"
            summary = "エラーで止まりました"
        else:
            shown, contract_failed = self.assistant.render_reply(result)
            status = "確認待ち" if contract_failed or AWAITING_MARKER in result.text else "完了"
            summary = "返答を利用者向けの形に整えられませんでした" if contract_failed else summarize(shown)
        await asyncio.to_thread(notion.update_task, task.id, status, summary)
        if status == "完了" and message_ts:
            await self.assistant.react_done(channel, message_ts)
        return {**info, "status": status, "summary": summary}

    # 先行研究の新着

    async def run_literature(self, day: str) -> dict:
        """先行研究の新着。テーマの検索キーワードと前提（CLAUDE.md）を知識の担当に渡し、選ばれた論文を
        研究ホームの先行研究 DB と、各テーマのチャンネルに出す。そのスレッドの質問は知識の担当が答える。"""
        ids = await self.assistant.channel_ids()
        notion = self.assistant.notion
        if notion is None:
            return {"status": "no_notion"}
        try:
            known = set(await asyncio.to_thread(notion.paper_ids))
        except (NotionError, KeyError) as e:
            log.warning("先行研究 DB を読めません: %s", e)
            return {"status": "error", "error": f"先行研究 DB を読めません: {e}"}
        results: dict[str, dict] = {}
        for cwd in themes.theme_dirs(self.config):
            name = cwd.name
            if name not in ids:
                continue  # アーカイブしたテーマや、Kei Agent のいないテーマは見張らない
            claude_md = cwd / "CLAUDE.md"
            keywords = search_keywords(claude_md)
            if not keywords:
                results[name] = {"status": "no_keywords"}
                continue
            premises = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
            reply = await self.assistant.ask_knowledge(knowledge.PAPER_DIGEST, {
                "theme": name, "keywords": keywords, "premises": premises, "known_ids": sorted(known),
                "count": knowledge.PAPERS_PER_THEME})
            if not reply.ok:
                results[name] = {"status": "error"}
                continue
            items = reply.data.get("items") or []
            if not items:
                results[name] = {"status": "no_new"}
                continue
            try:
                await asyncio.to_thread(notion.add_papers, name, items, "毎朝の新着")
            except (NotionError, KeyError) as e:
                log.warning("先行研究 DB に書けません（%s）: %s", name, e)
                results[name] = {"status": "error", "error": f"先行研究 DB に書けません: {e}"}
                continue
            known |= {str(item.get("id")) for item in items}
            posted = await self.assistant.slack.chat_postMessage(
                channel=ids[name], text=knowledge.papers_text(items, label(day)), unfurl_links=False, unfurl_media=False)
            thread_ts = str(posted.get("ts") or "")
            if thread_ts:
                # このスレッドの続きは、知識の担当が答える（ほかのスレッドは研究の担当）
                self.store.upsert_thread(ids[name], thread_ts, name, None)
                self.store.set_agent_session(ids[name], thread_ts, knowledge.AGENT, "")
            results[name] = {"status": "posted", "count": len(items), "thread_ts": thread_ts}
        failed = any(result.get("status") == "error" for result in results.values())
        return {"status": "error" if failed else "done", "themes": results}

    async def run_reading(self, day: str) -> dict:
        """朝の読みもの。共通ホームの「収集」ページの興味と情報源と、最近 👍 した記事を知識の担当に渡し、
        選ばれた記事を1記事 = 1投稿で出す（👍 とスレッドが記事ごとになる）。"""
        channels = self.config.module_channels.get("knowledge", ())
        name = channels[0] if channels else ""
        channel = (await self.assistant.channel_ids()).get(name) if name else None
        if channel is None:
            return {"status": "no_channel"}
        hub = self.assistant.hub
        if hub is None:
            return {"status": "no_hub"}
        try:
            interests, sources = await asyncio.to_thread(hub.collect_settings)
        except NotionError as e:
            log.warning("「収集」ページを読めません: %s", e)
            return {"status": "error", "error": f"「収集」ページを読めません: {e}"}
        if not interests or not sources:
            return {"status": "no_settings"}
        liked = self.store.liked_readings(time.time() - knowledge.LIKED_DAYS * 86400, knowledge.LIKED_EXAMPLES)
        reply = await self.assistant.ask_knowledge(knowledge.READING_DIGEST, {
            "interests": interests, "sources": sources, "count": knowledge.READING_COUNT,
            "liked": [{key: item.get(key) for key in ("title", "source", "interests")} for item in liked]})
        if not reply.ok:
            return {"status": "error"}
        items = reply.data.get("items") or []
        failed = [str(source) for source in reply.data.get("failed_sources") or []]
        if not items:
            return {"status": "no_new", "failed_sources": failed}
        for number, item in enumerate(items, 1):
            posted = await self.assistant.slack.chat_postMessage(
                channel=channel, text=knowledge.reading_post_text(item, number, len(items), hint=number == len(items)),
                unfurl_links=False, unfurl_media=False)
            ts = str(posted.get("ts") or "")
            if ts:
                # スレッドの質問は知識の担当へ（元の投稿も渡る）。👍 はこの控えで記事を知る
                self.store.upsert_thread(channel, ts, name, None)
                self.store.add_reading_post(channel, ts, day, item)
        return {"status": "posted", "count": len(items), "channel": channel, "failed_sources": failed}

    # Daily と振り返り

    async def run_hub_calendar(self, day: str) -> dict:
        return await self.sync_hub_calendar(day)

    async def sync_hub_calendar(self, day: str) -> dict:
        """授業ホームの課題（これからの全部）を、共通ホームの予定カレンダーに写す。

        会議は朝の Daily で読んだものを書く（sync_meetings）。ここでは AI を動かさない。
        """
        hub = self.assistant.hub
        if hub is None:
            return {"course": "no_hub"}
        checked_at = datetime.combine(date.fromisoformat(day), dtime(9, 0), JST)
        try:
            reply = await self.assistant.ask_course(course.LIST_CALENDAR_ASSIGNMENTS, days=COURSE_CALENDAR_DAYS)
            data = reply.data if reply.ok else {}
            if data.get("complete") is not True or not isinstance(data.get("items"), list):
                return {"course": "incomplete"}
            items = tuple(
                CalendarItem(source_id=str(item.get("id") or ""), title=str(item.get("title") or ""),
                             start=str(item.get("due") or ""), end="", url=str(item.get("url") or ""),
                             location="", status=str(item.get("status") or ""))
                for item in data["items"] if isinstance(item, dict)
            )
            if len(items) != len(data["items"]):
                raise IncompleteSnapshot("課題に不正な行があります")
            snapshot = CalendarSnapshot("課題", True, items, data.get("source_count"))
            report = await asyncio.to_thread(sync_calendar, hub, snapshot, checked_at, COURSE_CALENDAR_DAYS)
        except (IncompleteSnapshot, NotionError, ValueError, TypeError) as e:
            log.warning("課題を予定カレンダーに写せません: %s", e)
            return {"course": "error"}
        except Exception:
            log.exception("課題を予定カレンダーに写せません")
            return {"course": "error"}
        return {"course": "synced", "course_counts": report.__dict__}

    async def sync_meetings(self, events: list[dict], now: datetime) -> dict | str:
        """朝に読んだ会議（7日ぶん）を、共通ホームの予定カレンダーに足す。

        AI が読んだ一覧は全部とは言い切れないので、見つからなくなった会議は消さずに「要確認」にする
        （0件のときは読み損ねを疑って、印も付けない）。
        """
        hub = self.assistant.hub
        if hub is None:
            return "no_hub"
        try:
            snapshot = CalendarSnapshot("Outlook", False, outlook_items(events))
            report = await asyncio.to_thread(sync_calendar, hub, snapshot, now.astimezone(JST), VOICE_DAYS)
        except (IncompleteSnapshot, NotionError, ValueError, TypeError) as e:
            log.warning("会議を予定カレンダーに書けません: %s", e)
            return "error"
        except Exception:
            # 朝のまとめは止めない
            log.exception("会議を予定カレンダーに書けません")
            return "error"
        return report.__dict__

    async def sync_assignments(self) -> bool:
        """Moodle の課題を授業ホームに取り込む。増えた課題と締切の変わった課題は #20_course に知らせる。"""
        reply = await self.assistant.ask_course(course.SYNC_ASSIGNMENTS)
        if not reply.ok:
            return False
        changes = ([f"• 新しい: {escape(str(title))}" for title in reply.data.get("added") or []]
                   + [f"• 締切が変わった: {escape(str(title))}" for title in reply.data.get("updated") or []])
        channel = await self.course_channel() if changes else None
        if channel:
            await self.assistant.slack.chat_postMessage(channel=channel, text="\n".join(["📚 Moodle の課題", *changes]))
        return True

    async def _material(self, kind: str, day: str, since: float, ids: dict[str, str]) -> str:
        """プロンプトに入れる材料（上限の字数で切ったもの）。ファイルには残さない。"""
        title = {"daily": f"Daily の材料 {day}", "review": f"Retro & Planning の材料 {day}"}[kind]
        text = await digest.DigestBuilder(self.config, self.store, self.assistant).build(
            since, time.time(), title, set(ids), domains=kind == "review")
        return (f"{MATERIAL_START}\n{text.strip()}\n{MATERIAL_END}\n"
                f"（材料は {digest.MAX_DIGEST_CHARS} 字までで、超えた分は後ろのノートから省いています）")

    async def _save_note(self, channel: str, thread_ts: str, title: str, kind: str, day: str,
                         markdown: str) -> Note | None:
        """共通ホームの日別記録に1日1行で残す。残せなくても Slack には出ているので、知らせるだけにする。"""
        hub = self.assistant.hub
        if not markdown.strip():
            return None
        if hub is None:
            await self.assistant.notify_trouble(
                f"{title} を日別記録に保存できませんでした。共通 Notion ホームが使えません"
                "（Slack には出ています。共有と kei-agent-hub-setup を確認してください）")
            return None
        try:
            link = await self.assistant.permalink(channel, thread_ts)
            return await asyncio.to_thread(hub.upsert_day, kind, day, title, markdown, link)
        except NotionError as e:
            await self.assistant.notify_trouble(f"{title} を日別記録に保存できませんでした: {e}")
            return None

    async def run_daily(self, day: str) -> dict:
        ids = await self.assistant.channel_ids()
        channel = ids.get(self.overview_channel_name)
        if channel is None:
            return {"status": "no_channel"}
        last = self.store.last_schedule("daily", before_day=day)
        since = last["ran_at"] if last else time.time() - 86400
        material = await self._material("daily", day, since, ids)
        ws = themes.resolve(self.config, self.overview_channel_name)
        prompt = (
            f"[Kei Agent の定期処理: Daily {day}]\n"
            "次は前回の Daily からの材料です。\n\n"
            f"{material}\n\n"
            "材料（Notion のノートと Task を含む）と、そこに書かれたスレッドのログを読み、"
            "今日の議論の起点になる Daily を書いてください。\n\n"
            "**次の4つを、この順と見出しで書いてください。**ほかの見出しは足さないでください。\n"
            "スマホでも読めるように、全体を1画面に収めます。\n\n"
            "**今日のタスク**\n"
            "材料の「今日が期日の Task」を1行ずつ。**済みのものは `~取り消し線~` にする**"
            "（やったことも見えるように）。1件も無ければ「なし」の1行。\n\n"
            "**夜間処理の結果**\n"
            "夜間に終わったジョブと Task。無ければ1行で。\n\n"
            "**確認待ち・期日・止まっているテーマ・返事待ち**\n"
            "確認待ちの Task、期日が近い Task とマイルストーン、止まっているテーマ、返事待ちのスレッド、"
            "今週の時間の気になる点。**何も無いものはまとめて1行にする**（「いずれもなし」）。\n\n"
            "**今日考えるとよい問い**\n"
            "2〜3個。番号を振る。前日のスレッドの結果と、振り返り・考察のノートを踏まえる。\n\n"
            "**前日の動きの説明と、先行研究の新着は書かないでください。**前者は長くなって読み飛ばすため、"
            "後者はテーマのチャンネルに別で流れているためです（朝の予定に「どのテーマに新着があったか」だけ出ます）。\n\n"
            "返答は Kei Agent がそのまま共通 Notion ホームの日別記録に保存します。ファイルは作らないでください。\n"
            "Slack に出す本文は、次の marker の間にだけ書いてください。"
            "marker の外には何も書かず、作業手順・tool 名・ファイル名は本文に入れません。\n"
            "<<kei-agent-final>>\n（ここに4 section）\n<<kei-agent-final-end>>"
        )
        result = await self.assistant.run_detached(
            ws, self.overview_channel_name, prompt, "daily", actor="router",
            use_case=UseCase.OVERVIEW_DAILY)
        title = f"Daily {label(day)}"
        # 朝に読むものを1通にまとめる。チャンネルには今日の時系列、スレッドに Daily の中身
        timeline, gathered, notices = await self.morning_text(datetime.now())
        thread_ts = await self.assistant.publish(
            channel, self.overview_channel_name, ws, f"{timeline}\n\n🌅 {title}", result, output_kind="daily")
        for key in notices:
            self.store.record_notice(key)
        note = None
        if not result.is_error:
            note = await self._save_note(channel, thread_ts, title, "Daily", day, result.text)
        return {"status": "error" if result.is_error else "posted", "thread_ts": thread_ts,
                "notion_url": note.url if note else None, "morning": gathered}

    async def run_review(self, day: str) -> dict:
        ids = await self.assistant.channel_ids()
        channel = ids.get(self.overview_channel_name)
        if channel is None:
            return {"status": "no_channel"}
        now = datetime.now()
        has_course = course.AGENT in self.assistant.agents
        # 明日の計画に使うので、振り返りの前に Moodle の課題を取り込む
        synced = await self.sync_assignments() if has_course else False
        since = datetime.combine(date.fromisoformat(day), dtime(0, 0)).timestamp()
        material = await self._material("review", day, since, ids)
        ws = themes.resolve(self.config, self.overview_channel_name)
        prompt = (
            f"[Kei Agent の定期処理: Retro & Planning {day}]\n"
            "次は今日の材料です。\n\n"
            f"{material}\n\n"
            "材料と、そこに書かれたスレッドのログを読み、今日を振り返ってください。\n\n"
            "**Slack への返答は、次の2つの見出しと最後の1行だけ**にしてください。"
            "ほかの見出しや説明を足さないでください。\n\n"
            "**今日の成果**\n"
            "材料の「今日が期日の Task」のうち**済みのもの**を1行ずつ。"
            "Task になっていないが今日片付いたことがあれば、それも1行で足してよい。無ければ「なし」。\n\n"
            "**未完了タスク**\n"
            "同じ Task のうち**終わっていないもの**を1行ずつ。無ければ「なし」。\n\n"
            "最後に、次の1行をそのまま書いてください。\n"
            "夜間に実行したいタスクはありますか？\n\n"
            "返答は Kei Agent がそのまま共通 Notion ホームの日別記録（レトプラ）に保存します。ファイルは作らないでください。"
            "このあと、このスレッドに振り返りの結論が貼られたら、Kei Agent が同じ日別記録に追記します。"
            "ファイルには書かず、受け取ったことだけを短く返してください。\n\n"
            "Slack に出す本文は次の marker の間にだけ書いてください。marker の外には何も書かず、"
            "作業手順・tool 名・ファイル名・provider 名は本文に入れません。\n"
            "<<kei-agent-final>>\n（ここに指定の3 block）\n<<kei-agent-final-end>>"
        )
        result = await self.assistant.run_detached(
            ws, self.overview_channel_name, prompt, "review", actor="router",
            use_case=UseCase.OVERVIEW_PLAN)
        title = f"Retro & Planning {label(day)}"
        thread_ts = await self.assistant.publish(
            channel, self.overview_channel_name, ws, f"🌙 Retro & Planning {label(day)}", result,
            output_kind="review",
        )
        deadlines = ""
        if has_course:
            dues = await self.assistant.course_due(REVIEW_DUE_DAYS + 1, now) or []
            deadlines = morning.soon_deadlines(dues, now, REVIEW_DUE_DAYS)
        if deadlines and thread_ts:
            await self.assistant.slack.chat_postMessage(channel=channel, thread_ts=thread_ts, text=deadlines)
        note = None
        if not result.is_error:
            note = await self._save_note(channel, thread_ts, title, "振り返り", day,
                                         "\n\n".join(part for part in (result.text, deadlines, REVIEW_QUESTIONS) if part))
            if note:
                # このスレッドに貼られた結論を、同じ行のレトプラに足す（assistant.sync_review_conclusion）
                self.store.link_notion(channel, thread_ts, note.id, "review")
        return {"status": "error" if result.is_error else "posted", "thread_ts": thread_ts,
                "notion_url": note.url if note else None, "synced": synced}

    # 保守

    async def run_maintenance(self, day: str) -> dict:
        detail: dict = {"status": "done"}
        # いま直している最中の worktree と一時ディレクトリは残す（SQLite は別スレッドから触れない）
        busy = self.store.improvements_in("working", "review", "restarting")
        keep_worktrees = frozenset(Path(r["worktree"]).name for r in busy if r["worktree"])
        keep_scratch = frozenset(r["thread_ts"] for r in busy)
        detail["removed"] = await asyncio.to_thread(
            maintenance.cleanup, self.config, maintenance.claude_projects_dir(), None,
            keep_worktrees, keep_scratch)
        detail["notices"] = self.store.drop_old_notices(time.time() - NOTICE_RETENTION_DAYS * 86400)
        # 声をかけてもさらに同じ時間が過ぎた返事待ちは閉じる（放っておくと何日も残る）
        detail["awaits"] = self.store.forget_stale_awaits(
            time.time() - self.config.schedule.unanswered_hours * 2 * 3600)
        # エージェントの claude の会話も、セッションの記録と同じ日数で忘れる
        detail["agent_sessions"] = self.store.drop_old_agent_sessions(
            time.time() - self.config.maintenance.session_retention_days * 86400)
        detail["toggl"] = await self.import_toggl()
        if self.config.maintenance.backup:
            try:
                detail["backup"] = await maintenance.backup(self.config, day, self.store)
                await self._warn_unsaved(detail["backup"].get("agent_root") or {})
            except maintenance.BackupError as e:
                await self.assistant.notify_trouble(f"研究データのバックアップに失敗しました: {e}")
                detail = {**detail, "status": "error", "error": str(e)}
        return detail

    async def import_toggl(self) -> dict:
        """Toggl のアプリで直接測った記録を、共通ホームの時間記録に入れる。失敗しても保守は続ける。"""
        hub = self.assistant.hub
        if hub is None or not hub.has_time_db:
            return {"status": "skipped", "reason": "no_hub"}
        toggl = timelog.load_toggl()
        if toggl is None:
            return {"status": "skipped", "reason": "no_toggl"}
        until = date.today()
        since = until - timedelta(days=timelog.IMPORT_DAYS - 1)
        # Slack で測った分（Toggl にも送ってある）。SQLite は別スレッドから触れないので、先に読む
        rows = self.store.finished_time_entries(
            datetime.combine(since, dtime(0, 0)).timestamp() - 86400, time.time() + 86400)
        own = [(r["started_at"], r["ended_at"] - r["started_at"]) for r in rows]
        try:
            return await asyncio.to_thread(timelog.import_toggl, toggl, hub, own, since, until)
        except Exception as e:
            log.exception("Toggl の記録を時間記録に取り込めませんでした")
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    async def _warn_unsaved(self, agent: dict) -> None:
        """Kei Agent 側（状態の書き出しなど）が保存できていないときに知らせる。"""
        why = {
            "not_a_repo": "Git のリポジトリになっていません",
            "no_remote": "push 先（origin）が登録されていません",
        }.get(str(agent.get("status") or ""))
        if why:
            await self.assistant.notify_trouble(
                f"Kei Agent 側のデータ（状態の書き出しなど）が保存できていません: `{agent.get('path')}` が{why}。"
                "非公開のリポジトリを作って `git remote add origin <URL>` してください")

    # 授業（大学エージェント）

    async def course_channel(self) -> str | None:
        """#20_course の ID。Kei Agent がいなければ None。"""
        name = self.config.course_channels[0] if self.config.course_channels else ""
        return (await self.assistant.channel_ids()).get(name) if name else None

    async def morning_text(self, now: datetime) -> tuple[str, dict, list[str]]:
        """朝のまとめ（今日の時系列）。集められなかったものは黙って飛ばす。"""
        detail: dict = {}
        classes: list[dict] = []
        dues: list[dict] = []
        events: list[dict] = []
        if course.AGENT in self.assistant.agents:
            detail["synced"] = await self.sync_assignments()
            classes = (await self.assistant.ask_course(course.LIST_CLASSES)).data.get("items") or []
            dues = await self.assistant.course_due(course.DIGEST_DAYS, now) or []
        if work.AGENT in self.assistant.agents:
            # 声のレイヤが「今週の会議」に答えられるように、1週間ぶん取る
            reply = await self.assistant.ask_work(work.LIST_EVENTS, days=VOICE_DAYS)
            events = reply.data.get("items") or [] if reply.ok else []
            if reply.ok:
                # 読んだ会議は、共通ホームの予定カレンダーにも書く（AI をもう一度動かさない）
                detail["meetings"] = await self.sync_meetings(events, now)
        detail |= {"classes": len(classes), "dues": len(dues), "events": len(events)}
        # 朝に出した締切は、そのあと24時間前の知らせで繰り返さない。ただし記録するのは
        # Slack に出せたあと（出す前に記録すると、投稿に失敗したときに黙って消える）
        notices = [course.notice_key(item) for item in course.soon_items(dues, now)]
        # 声のレイヤは、聞かれてから取りに行かず、朝に決まったものを手元へ渡しておく。
        # 渡すのはデータで、声の言い方は声のレイヤが作る（帯も URL も声では読めない）。
        # **日付も渡す。** 今日ぶんだけ渡していたせいで、明日を聞かれても今日を答えていた
        self.assistant.notify_voice("schedule", items=[
            {"date": f"{e.day:%Y-%m-%d}", "at": e.clock,
             "end": f"{e.end:%H:%M}" if e.end else "", "icon": e.icon, "text": e.text}
            for e in morning.upcoming(classes, events, dues, now, days=VOICE_DAYS)])
        failed_now = [label for label, failed in (("課題の取り込み", detail.get("synced") is False),
                                                  ("会議の書き込み", detail.get("meetings") == "error")) if failed]
        return morning.text(classes, events, dues, now, self.morning_notes(now, failed_now)), detail, notices

    def failure_note(self, now: datetime, failed_now: list[str] | None = None) -> str:
        """前回の Daily から今朝までに、うまくいかなかった定期処理を1行で。無ければ空文字。

        Daily とレトプラが何日も Notion に残っていなかったのに、気づけなかった（2026-09-26）。
        """
        today = now.date().isoformat()
        last = self.store.last_schedule("daily", before_day=today)
        since = last["ran_at"] if last else now.timestamp() - 86400
        failed = []
        for row in self.store.schedule_runs_since(since):
            label = settings.schedule_label(self.config, row["name"]) if row["name"] in settings.schedule_names(self.config) else None
            if label is None or (row["name"] == "daily" and row["day"] == today):
                continue    # 定期処理でないもの、いま作っている Daily
            detail = json.loads(row["detail"] or "{}") or {}
            if detail.get("status") == "error":
                failed.append(label)
            elif row["name"] in ("daily", "review") and detail.get("status") == "posted" and not detail.get("notion_url"):
                failed.append(f"{label}（Notion に残せず）")
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        if (self.assistant.hub is not None and course.AGENT in self.assistant.agents
                and not self.store.schedule_ran("hub_calendar", yesterday)):
            failed.append("予定カレンダーへの課題の書き込み")
        failed += failed_now or []
        return f"⚠️ うまくいかなかったこと: {'、'.join(dict.fromkeys(failed))}" if failed else ""

    def morning_notes(self, now: datetime | None = None, failed_now: list[str] | None = None) -> list[str]:
        """時刻の無いもの（今朝の先行研究の新着と読みもの、うまくいかなかったこと）を、1行ずつ。"""
        now = now or datetime.now()
        today = now.date().isoformat()
        notes = []
        themes_ = self.today_detail("literature", today).get("themes") or {}
        posted = [name for name, got in themes_.items() if got.get("status") == "posted"]
        if posted:
            notes.append("先行研究の新着: " + "、".join(f"#{name}" for name in posted))
        reading = self.today_detail("reading", today)
        if reading.get("status") == "posted" and reading.get("channel"):
            notes.append(f"読みもの: {reading.get('count')}件（<#{reading['channel']}>）")
        failure = self.failure_note(now, failed_now)
        if failure:
            notes.append(failure)
        return notes

    def today_detail(self, name: str, today: str) -> dict:
        """その定期処理の今日の記録（まだなら空）。昨日の分を、今朝のもののように載せないため。"""
        last = self.store.last_schedule(name)
        if last is None or last["day"] != today:
            return {}
        return json.loads(last["detail"] or "{}") or {}

    async def notify_due_soon(self, now: datetime) -> None:
        """締切まで24時間を切った課題を、1件ずつ1回だけ知らせる。"""
        if now.timestamp() - self._due_checked < DUE_CHECK_SECONDS:
            return
        channel = await self.course_channel()
        if channel is None:
            return
        items = await self.assistant.course_due(2, now)
        if items is None:
            # 取れなかったときは時計を進めない（1時間待たずに、次の tick で取り直す）
            return
        self._due_checked = now.timestamp()
        for item in course.soon_items(items, now):
            key = course.notice_key(item)
            if self.store.noticed(key):
                continue
            await self.assistant.slack.chat_postMessage(channel=channel, text=course.soon_text(item, now))
            self.assistant.notify_voice("due", title=item.get("title"), at=item.get("at"))
            self.store.record_notice(key)
        await self.notify_unstarted(channel, now)

    async def notify_unstarted(self, channel: str, now: datetime) -> None:
        """締切まで3日を切っても「未着手」の課題を、1件ずつ1回だけ知らせる（Notion の課題の状態を見る）。"""
        reply = await self.assistant.ask_course(course.LIST_CALENDAR_ASSIGNMENTS, days=course.EARLY_DAYS + 1)
        if not reply.ok:
            return
        for item in course.unstarted_items(reply.data.get("items") or [], now):
            key = course.early_notice_key(item)
            if self.store.noticed(key):
                continue
            await self.assistant.slack.chat_postMessage(channel=channel, text=course.early_text(item, now))
            self.store.record_notice(key)

    async def notify_unrestarted(self, now: datetime) -> None:
        """取り込んだ新しい版で、1時間たっても起動し直していなければ、一度だけ知らせる。"""
        if now.timestamp() - self._version_checked < VERSION_CHECK_SECONDS:
            return
        self._version_checked = now.timestamp()
        disk = await asyncio.to_thread(version.on_disk)
        if not version.differs(disk):
            self._newer = None
            return
        if self._newer is None or self._newer[0] != disk:
            # 自己改善の取り込みは、静かになってから起動し直す。見つけたばかりなら、もう少し待つ
            self._newer = (disk, now.timestamp())
            return
        key = f"version:{disk}"
        if now.timestamp() - self._newer[1] < VERSION_CHECK_SECONDS or self.store.noticed(key):
            return
        await self.assistant.notify_trouble(f"新しい版（{disk}）を取り込みましたが、まだ起動し直していません。"
                                            "deploy/restart-all.sh で起動し直してください")
        self.store.record_notice(key)

    # 声かけ

    async def nudge_stale_threads(self) -> None:
        hours = self.config.schedule.unanswered_hours
        for row in self.store.threads_to_nudge(time.time() - hours * 3600):
            req = Request(row["channel"], row["channel_name"], row["thread_ts"], None, "")
            try:
                await self.assistant.post(
                    req, f"<@{self.config.allowed_user_id}> ⏰ 返事待ちのまま{hours}時間たちました。続けるときは、このスレッドに返信してください。"
                )
            except Exception:
                # アーカイブしたチャンネルなどに投稿できなくても、毎分やり直さない
                log.warning("返事待ちのスレッドに声をかけられません: #%s %s", row["channel_name"], row["thread_ts"], exc_info=True)
            self.store.mark_nudged(row["channel"], row["thread_ts"])


async def _run_once(name: str, record: bool) -> None:
    from slack_sdk.web.async_client import AsyncWebClient

    from kei_agent.config import load_config
    from kei_agent.jobs import JobManager
    from kei_agent.notion_hub import load_hub
    from kei_agent.notion_store import load_notion

    config = load_config()
    store = Store(config.db_path)
    slack = AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"])
    auth = await slack.auth_test()
    pueue = research.pueue(config)
    await pueue.ensure_group()  # 夜間の Task がジョブを投入することがある
    assistant = Assistant(config, store, slack, JobManager(config, store, pueue),
                          os.environ["SLACK_BOT_TOKEN"], auth["user_id"],
                          notion=load_notion(config), team_url=auth.get("url", ""),
                          team_id=auth.get("team_id", ""), hub=load_hub(config))
    scheduler = Scheduler(config, store, assistant)
    day = date.today().isoformat()
    detail = await scheduler.run_task(name, day, record=record)
    print(json.dumps(detail, ensure_ascii=False, indent=2))


def main() -> None:
    """定期処理を今すぐ1回動かす（確認用）。"""
    from kei_agent.config import load_config

    parser = argparse.ArgumentParser(prog="kei-agent-schedule")
    parser.add_argument("name", choices=task_names(load_config()))
    parser.add_argument("--record", action="store_true", help="今日の分を実行済みとして記録する")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_run_once(args.name, args.record))


if __name__ == "__main__":
    main()
