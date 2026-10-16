"""記憶（頭脳の中継から使う）。全部 maid\\data\\ に保存し、外には出さない。

- 会話ログ: 1 往復ごとに data\\log\\日付.jsonl に追記する
- まとめ: 30 分話さなかったら（と、maid を止めるとき）、まだまとめていない会話を
    - 要約して「前の会話」として保存する（data\\memory.db。あとで探せるように意味のベクトルも付ける）
    - 相手について覚えていること（data\\USER.md）とキャラのメモ（data\\MEMORY.md）を書き直す
      （「熱がある」→「治った」のように、状態が変わったら書き換える）
- 「覚えて」「忘れて」と言われたら、その場でメモを書き直す
- 会話のたびに、メモと、今の話に関係ありそうな前の会話を頭脳に渡す
- キャラから話しかけるときの話題の種を出す

USER.md / MEMORY.md はメモ帳で開いて直してよい。書き直す前の版は data\\history\\ に残る。
"""

from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

log = logging.getLogger("memory")

IDLE_MINUTES = 30          # これだけ話さなかったら、それまでの会話をまとめる
MAX_NOTE_LINES = 40        # メモ 1 つの最大行数
RELATED_EPISODES = 3       # 会話のたびに渡す「前の会話」の数
MIN_SIMILARITY = 0.5       # これより似ていない前の会話は渡さない
HISTORY_KEEP = 30          # 書き直す前のメモをいくつ残すか

REMEMBER_RE = re.compile(r"覚えて|おぼえて|忘れないで|わすれないで|忘れて|わすれて")

EPISODE_PROMPT = (
    "次は、相手（ユーザー）とキャラの会話の記録です。あとで思い出せるように、"
    "何を話したか・相手の様子や出来事・約束したことを、3〜5 文の日本語で要約してください。"
    "相手は「相手」、キャラは「キャラ」と書く。要約だけを出力する。"
)
# 会話から「覚えておくこと」を、誰のことかを付けて書き出させる（小さいモデルは誰の発言かを取り違えやすいので、
# 書き出しと、メモへの書き込みを分ける）
FACTS_PROMPT = (
    "あなたは記憶係です。次は「相手」（ユーザー。人間）と「キャラ」（AI のキャラクター）の会話です。"
    "あとで覚えておくとよいことを 1 行ずつ書き出してください。各行は次のどちらかで始める:\n"
    "相手: … 相手自身のこと（相手が自分について言ったこと・したこと・これからすること・体調・好み・生活）\n"
    "キャラ: … キャラ自身のこと（キャラが言った自分の好き嫌いや気持ち・キャラが約束したこと・キャラが次に話したいこと）\n"
    "注意:\n"
    "- 好き嫌いや気持ちは、それを言った本人のこととして書く（キャラが「オムライスがすき」と言ったら「キャラ: オムライスが好き」）。\n"
    "- 相手が質問しただけのことを、相手の好みにしない。\n"
    "- 相手が「忘れて」と言ったら「相手: 〜は忘れてほしい」と書く。\n"
    "- 覚えるほどでもない雑談は書かない。何も無ければ「なし」とだけ出力する。"
)
NOTES_RULES = (
    "【新しくわかったこと】を【今のメモ】に反映した全文を出力してください。ルール:\n"
    "- 新しいことは足す。同じことはまとめる。\n"
    "- 状態が変わったら書き換える（例:「熱がある」のあとに治ったとわかったら「熱は治った（{today}）」にする）。\n"
    "- 古くなって意味がなくなったものと、「忘れてほしい」と言われたものは消す。\n"
    "- 1 行に 1 つ、「- 」で始める。体調や予定のように変わるものには（月/日）を付ける。{max_lines} 行まで。\n"
    "- 書いてないことや推測は足さない。メモの全文だけを出力する。"
)
USER_NOTES_PROMPT = "あなたは記憶係です。【今のメモ】は、相手（ユーザー。人間）について覚えていること（事実・好み・生活・体調・最近していること）です。\n" + NOTES_RULES
CHAR_NOTES_PROMPT = ("あなたは記憶係です。【今のメモ】は、キャラ自身の覚え書き（キャラの好き嫌い・相手との思い出・約束・"
                     "キャラが最近気になっていること・次に話したいこと）です。キャラの性格や口調は書かない。\n" + NOTES_RULES)
TIDY_PROMPT = ("あなたは記憶係です。【今のメモ】を整理してください。同じ内容はまとめ、矛盾するものは新しい方を残し、"
               "古くて意味がなくなったものは消す。1 行に 1 つ、「- 」で始める。{max_lines} 行まで。整理したメモの全文だけを出力する。")

CONTEXT_HEAD = "【あなたが覚えていること。会話に関係があるときだけ自然に使い、全部を言わない】"


@dataclass
class Turn:
    when: datetime
    user: str
    assistant: str
    proactive: bool = False

    def lines(self) -> list[str]:
        out = [] if self.proactive or not self.user else [f"相手: {self.user}"]
        return out + ([f"キャラ: {self.assistant}"] if self.assistant else [])


class Memory:
    def __init__(self, folder: Path, llm: Callable[[list, int], str], embed: Callable[[list[str]], list | None] | None = None,
                 idle_minutes: int = IDLE_MINUTES):
        """llm(messages, max_tokens) は返事の文字列を、embed(texts) は意味のベクトルの一覧（使えなければ None）を返す。"""
        self.dir = Path(folder)
        self.llm = llm
        self.embed = embed or (lambda texts: None)
        self.idle = timedelta(minutes=idle_minutes)
        self.lock = threading.RLock()       # ファイルや DB を読み書きする短い間だけ
        self.work_lock = threading.RLock()  # 記憶のまとめ（頭脳を使うので長い）。会話はこれを待たない
        (self.dir / "log").mkdir(parents=True, exist_ok=True)
        (self.dir / "history").mkdir(exist_ok=True)
        self.user_md = self.dir / "USER.md"
        self.char_md = self.dir / "MEMORY.md"
        self.state_path = self.dir / "state.json"
        self.db = sqlite3.connect(self.dir / "memory.db", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS episodes (id INTEGER PRIMARY KEY, started TEXT, ended TEXT, summary TEXT, embedding TEXT)")
        self.db.commit()
        self._vectors: list[tuple[int, str, str, list[float] | None]] | None = None
        self._busy_since = 0.0

    # ---------------------------------------------------------------- 記録
    def record(self, user: str, assistant: str, proactive: bool = False, when: datetime | None = None) -> None:
        when = when or datetime.now()
        row = {"t": when.isoformat(timespec="seconds"), "user": user, "assistant": assistant, "proactive": proactive}
        with self.lock, (self.dir / "log" / f"{when:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if not proactive and REMEMBER_RE.search(user or ""):
            # 「覚えて」「忘れて」はその場で反映する（返事を待たせないよう裏で）
            threading.Thread(target=self.update_notes, args=([Turn(when, user, assistant)],), daemon=True).start()

    def _state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, **kw) -> None:
        self.state_path.write_text(json.dumps({**self._state(), **kw}, ensure_ascii=False), encoding="utf-8")

    def pending(self) -> list[Turn]:
        """まだまとめていない会話。"""
        since = self._state().get("summarized_until", "")
        turns = []
        for path in sorted((self.dir / "log").glob("*.jsonl")):
            if since and path.stem < since[:10]:
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row["t"] > since:
                    turns.append(Turn(datetime.fromisoformat(row["t"]), row.get("user", ""), row.get("assistant", ""),
                                      bool(row.get("proactive"))))
        return turns

    # ---------------------------------------------------------------- まとめ
    def maybe_consolidate(self, now: datetime | None = None) -> bool:
        """最後の会話から 30 分たっていたら、まとめる。"""
        turns = self.pending()
        if not turns or (now or datetime.now()) - turns[-1].when < self.idle:
            return False
        return self.consolidate(turns)

    def consolidate(self, turns: list[Turn] | None = None, tidy: bool = False) -> bool:
        """まだまとめていない会話を要約し、メモを書き直す。tidy なら最後にメモを整理する（maid を止めるとき）。"""
        with self.work_lock:
            turns = self.pending() if turns is None else turns
            if not turns and not tidy:
                return False
            self._busy_since = time.perf_counter()
            log.info("記憶をまとめています（%d 往復）...", len(turns))
            try:
                return self._consolidate(turns, tidy)
            finally:
                log.info("記憶をまとめました（%.0f 秒）", time.perf_counter() - self._busy_since)
                self._busy_since = 0.0

    def busy(self) -> bool:
        """記憶のまとめ中か（その間は頭脳がそちらに使われるので、返事が遅くなる）。"""
        return bool(self._busy_since)

    def _consolidate(self, turns: list[Turn], tidy: bool) -> bool:
        # 30 分以上あいたところで分けて、1 回の会話ずつまとめる（古い順。状態の変化を順に反映するため）
        for session in self.sessions(turns):
            if any(t.user for t in session):
                text = "\n".join(line for t in session for line in t.lines())
                summary = self._ask([{"role": "system", "content": EPISODE_PROMPT}, {"role": "user", "content": text}], 400)
                if summary:
                    self.add_episode(session[0].when, session[-1].when, summary)
                self.update_notes(session)
            with self.lock:
                self._save_state(summarized_until=session[-1].when.isoformat(timespec="seconds"))
        if tidy:
            for path in (self.user_md, self.char_md):
                self._rewrite(path, TIDY_PROMPT.format(max_lines=MAX_NOTE_LINES), "")
        return bool(turns)

    def sessions(self, turns: list[Turn]) -> list[list[Turn]]:
        out: list[list[Turn]] = []
        for t in turns:
            if out and t.when - out[-1][-1].when < self.idle:
                out[-1].append(t)
            else:
                out.append([t])
        return out

    def facts(self, turns: list[Turn]) -> tuple[list[str], list[str]]:
        """会話から覚えておくことを書き出し、（相手のこと, キャラのこと）に分ける。"""
        talk = "\n".join(line for t in turns for line in t.lines())
        answer = self._ask([{"role": "system", "content": FACTS_PROMPT}, {"role": "user", "content": talk}], 600)
        user, char = [], []
        for line in answer.splitlines():
            line = line.strip().lstrip("-・* ").strip()
            for prefix, bucket in (("相手:", user), ("相手：", user), ("キャラ:", char), ("キャラ：", char)):
                if line.startswith(prefix) and line[len(prefix):].strip():
                    bucket.append(line[len(prefix):].strip())
        return user, char

    def update_notes(self, turns: list[Turn]) -> None:
        with self.work_lock:
            today = f"{turns[-1].when:%m/%d}" if turns else f"{datetime.now():%m/%d}"
            user, char = self.facts(turns)
            for path, prompt, facts in ((self.user_md, USER_NOTES_PROMPT, user), (self.char_md, CHAR_NOTES_PROMPT, char)):
                if facts:
                    self._rewrite(path, prompt.format(today=today, max_lines=MAX_NOTE_LINES), "\n".join(f"- {f}" for f in facts))

    def _rewrite(self, path: Path, prompt: str, talk: str) -> None:
        old = self.read_note(path)
        if not old and not talk:
            return
        content = f"【今のメモ】\n{old or '（まだ無い）'}" + (f"\n\n【新しくわかったこと】\n{talk}" if talk else "")
        answer = self._ask([{"role": "system", "content": prompt}, {"role": "user", "content": content}], 1200)
        lines = [ln.strip() for ln in answer.splitlines() if ln.strip().startswith("- ")][:MAX_NOTE_LINES]
        new = "\n".join(lines)
        forget = bool(re.search(r"忘れ|わすれ", talk))
        # 頭脳がうまく書けなかったとき（空・半分以下に減った）は、忘れてと言われていない限り書き換えない
        if not new or (old and not forget and len(lines) < len(old.splitlines()) / 2):
            if answer:
                log.warning("%s の書き直しを見送りました（結果が短すぎる）", path.name)
            return
        if new != old:
            with self.lock:
                self._backup(path)
                path.write_text(new + "\n", encoding="utf-8")
            log.info("%s を更新しました（%d 行）", path.name, len(lines))

    def _backup(self, path: Path) -> None:
        if not path.exists():
            return
        hist = self.dir / "history"
        (hist / f"{path.stem}-{datetime.now():%Y%m%d-%H%M%S}.md").write_bytes(path.read_bytes())
        for old in sorted(hist.glob(f"{path.stem}-*.md"))[:-HISTORY_KEEP]:
            old.unlink()

    def _ask(self, messages: list, max_tokens: int) -> str:
        try:
            return (self.llm(messages, max_tokens) or "").strip()
        except Exception as e:  # 頭脳が使えないときは、次の機会にまとめる
            log.warning("記憶の整理に失敗しました: %s", e)
            return ""

    @staticmethod
    def read_note(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    # ---------------------------------------------------------------- 前の会話
    def add_episode(self, started: datetime, ended: datetime, summary: str) -> None:
        vec = self._embed_one(summary)
        with self.lock:
            self.db.execute("INSERT INTO episodes (started, ended, summary, embedding) VALUES (?, ?, ?, ?)",
                            (started.isoformat(timespec="seconds"), ended.isoformat(timespec="seconds"), summary,
                             json.dumps(vec) if vec else None))
            self.db.commit()
            self._vectors = None

    def _embed_one(self, text: str) -> list[float] | None:
        try:
            vecs = self.embed([text])
        except Exception as e:
            log.info("意味のベクトルを作れませんでした: %s", e)
            return None
        return vecs[0] if vecs else None

    def episodes(self) -> list[tuple[int, str, str, list[float] | None]]:
        with self.lock:
            if self._vectors is None:
                rows = self.db.execute("SELECT id, started, summary, embedding FROM episodes ORDER BY id").fetchall()
                self._vectors = [(i, s, text, json.loads(e) if e else None) for i, s, text, e in rows]
            return self._vectors

    def related(self, query: str, limit: int = RELATED_EPISODES) -> list[tuple[str, str]]:
        """今の発言に関係ありそうな前の会話（日時, 要約）。意味の近さ（ベクトル）と、言葉の重なりの両方で探す。"""
        eps = self.episodes()
        if not query.strip():
            return []
        if not eps:
            if query == "こんにちは":  # 起動時の読み込み（前の会話がまだ無くても）
                self._embed_one(query)
            return []
        qvec = self._embed_one(query) if any(e[3] for e in eps) else None
        scored = []
        for _, started, summary, vec in eps:
            score = 0.6 * overlap(query, summary)
            if qvec and vec:
                score += cosine(qvec, vec)
            if score >= MIN_SIMILARITY:
                scored.append((score, started, summary))
        scored.sort(reverse=True)
        return [(s, text) for _, s, text in scored[:limit]]

    def context(self, query: str) -> str:
        """頭脳に渡す「覚えていること」。"""
        parts = []
        user, char = self.read_note(self.user_md), self.read_note(self.char_md)
        if user:
            parts.append("相手について:\n" + user)
        if char:
            parts.append("あなたのメモ:\n" + char)
        rel = self.related(query)
        if rel:
            parts.append("関係がありそうな前の会話:\n" + "\n".join(f"- {s[5:10].replace('-', '/')}: {t}" for s, t in rel))
        return CONTEXT_HEAD + "\n" + "\n\n".join(parts) if parts else ""

    # ---------------------------------------------------------------- 話しかける話題
    def topic(self, rng: random.Random | None = None) -> str:
        """キャラから話しかけるときの話題の種。前の会話・相手のこと・キャラの関心・励まし・雑談から選ぶ。"""
        rng = rng or random.Random()
        recent = self.episodes()[-5:]
        user = [ln[2:] for ln in self.read_note(self.user_md).splitlines() if ln.startswith("- ")]
        char = [ln[2:] for ln in self.read_note(self.char_md).splitlines() if ln.startswith("- ")]
        hour = datetime.now().hour
        choices = [
            ("前の会話の続き", lambda: "この前の話の続きを聞く。前の会話: " + rng.choice(recent)[2]) if recent else None,
            ("相手のこと", lambda: "相手について覚えていることに触れて、気にかけたり質問したりする: " + rng.choice(user)) if user else None,
            ("自分の関心", lambda: "自分が最近気になっていることや話したいことを話す: " + rng.choice(char)) if char else None,
            ("励まし", lambda: "相手が今していることを応援したり、がんばりをほめたりする"),
            ("休憩", lambda: "相手を気づかう（休憩・水分・姿勢など）。押しつけない"),
            ("時間帯", lambda: f"今の時間（{hour} 時ごろ）らしい話をする（ごはん・眠さ・一日の様子など）"),
            ("雑談", lambda: "思いついた雑談や、相手への質問をする（好きなもの・最近のこと・今日あったこと）"),
        ]
        choices = [c for c in choices if c]
        # 覚えていることがあるときは、それに基づく話題を多めに
        weights = [3 if name in ("前の会話の続き", "相手のこと", "自分の関心") else 1 for name, _ in choices]
        name, make = rng.choices(choices, weights=weights)[0]
        return make()


def overlap(a: str, b: str) -> float:
    """2 文字ずつの重なり（日本語でも使える、言葉の重なりの目安）。0〜1。"""
    def grams(s: str) -> set[str]:
        s = re.sub(r"\s+", "", s)
        return {s[i:i + 2] for i in range(len(s) - 1)}
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / len(ga) if ga else 0.0


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
