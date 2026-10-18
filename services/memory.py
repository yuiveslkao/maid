"""記憶（頭脳の中継から使う）。全部 maid\\data\\ に保存し、外には出さない。

覚えるのは「相手（ユーザー）のこと」と「二人の間のこと」だけ。キャラの好みや性格はキャラ設定にあって固定なので覚えない。

- PROFILE.md  相手のプロフィール。長く変わらない性格・傾向・好み・生活（抽象的に。「面倒くさがりなところがある」くらい）
- NOW.md      今のこと（具体的に。日時付き。期限が来たら自動で消す）
                [状況] 数時間〜1 日: ごはん・体調・今していること・家にあるもの
                [約束] 果たされるまで（最長 7 日）: 約束・やりかけ
                [近況] 数週間: しばらく続いていること（引っ越しの準備中、忙しい時期など）
- 前の会話     1 回の会話ごとの要約（data\\memory.db。意味の近さで探せるようにベクトルも付ける）

- 会話ログ: 1 往復ごとに data\\log\\日付.jsonl に追記する
- まとめ: 30 分話さなかったら（と、maid を止めるとき）、まだまとめていない会話から
  覚えておくことを種類付きで書き出させ、それぞれに振り分ける
- 「覚えて」「忘れて」と言われたら、その場で反映する
- 会話のたびに、PROFILE と NOW と、今の話に関係ありそうな前の会話を頭脳に渡す
- キャラから話しかけるときの話題の種を出す

PROFILE.md / NOW.md はメモ帳で開いて直してよい。PROFILE.md の書き直す前の版は data\\history\\ に残る。
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
MAX_NOTE_LINES = 25        # PROFILE.md の最大行数
# NOW.md の種類と、覚えておく長さ
NOW_KINDS = {"状況": timedelta(days=1), "約束": timedelta(days=7), "近況": timedelta(days=30)}
NOTHING = {"なし", "無し", "特になし", "特に無し", "ない", "ありません", "none", "None", "-"}  # 「状況: なし」は書かない
NOW_LINE_RE = re.compile(r"^- \[(状況|約束|近況)\] (.+?)（(\d{4}-\d{2}-\d{2} \d{2}:\d{2})）$")
RELATED_EPISODES = 3       # 会話のたびに渡す「前の会話」の数
MIN_SIMILARITY = 0.5       # これより似ていない前の会話は渡さない
HISTORY_KEEP = 30          # 書き直す前のメモをいくつ残すか

REMEMBER_RE = re.compile(r"覚えて|おぼえて|忘れないで|わすれないで|忘れて|わすれて")

EPISODE_PROMPT = (
    "次は、相手（ユーザー）とキャラの会話の記録です。あとで思い出せるように、"
    "何を話したか・相手の様子や出来事・約束したことを、3〜5 文の日本語で要約してください。"
    "相手は「相手」、キャラは「キャラ」と書く。要約だけを出力する。"
)
# 会話から「覚えておくこと」を、種類を付けて書き出させる（小さいモデルは、会話を丸ごと渡してメモを書かせると
# 誰の発言かを取り違えたり、その場の思いつきを好みとして書いたりするので、書き出しと振り分けを分ける）
FACTS_PROMPT = (
    "あなたは記憶係です。次は「相手」（ユーザー。人間）と「キャラ」（AI のキャラクター）の会話です。"
    "次に話すときに知っておくとよいことを、1 行ずつ、次のどれかで始めて書き出してください。\n"
    "プロフィール: 相手の長く変わらない性格・傾向・好み・生活。具体的な出来事は一般化して抽象的に書く"
    "（例:「ハンバーグは作るのが大変」→「手間のかかることは面倒くさがる」）\n"
    "状況: 今日〜明日くらいの、相手の具体的な状況（ごはん・体調・今していること・これからすること・家にあるもの）\n"
    "約束: 相手とキャラの約束や、やりかけのこと（例:「今度オムライスを作る」）\n"
    "近況: 数週間続く、相手の具体的なこと（例:「引っ越しの準備中」）\n"
    "済み: 果たされた約束・終わったこと・治ったこと（前に覚えたことを消すため）\n"
    "忘れる: 相手が「忘れて」と言ったこと\n"
    "注意:\n"
    "- キャラ自身の好み・気持ち・希望は書かない（キャラが「ハンバーグがいい」と言っても書かない）。\n"
    "- 相手が質問しただけのことは書かない。その会話の中だけのやりとりや雑談は書かない。\n"
    "- 何も無ければ「なし」とだけ出力する。"
)
PROFILE_PROMPT = (
    "あなたは記憶係です。【今のプロフィール】は、相手（ユーザー。人間）の長く変わらない性格・傾向・好み・生活の箇条書きです。"
    "【足すこと】と【忘れること】を反映した全文を出力してください。ルール:\n"
    "- 抽象度を高く保つ。具体的な出来事ではなく、傾向として書く（「面倒くさがりなところがある」「辛いものが苦手」など）。\n"
    "- 似たものはまとめる。矛盾したら新しい方を残す。【忘れること】は消す。\n"
    "- 1 行に 1 つ、「- 」で始める。{max_lines} 行まで。書いてないことや推測は足さない。全文だけを出力する。"
)
TIDY_PROMPT = ("あなたは記憶係です。【今のプロフィール】は相手（ユーザー）の性格・傾向・好み・生活です。整理してください。"
               "同じ内容はまとめ、矛盾するものは新しい方を残し、具体的すぎるものは傾向として抽象的に書き直す。"
               "1 行に 1 つ、「- 」で始める。{max_lines} 行まで。整理した全文だけを出力する。")

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
        self.profile_md = self.dir / "PROFILE.md"
        self.now_md = self.dir / "NOW.md"
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
            self._rewrite(self.profile_md, TIDY_PROMPT.format(max_lines=MAX_NOTE_LINES), "")
            self.write_now(self.now_items())  # 期限切れを消す
        return bool(turns)

    def sessions(self, turns: list[Turn]) -> list[list[Turn]]:
        out: list[list[Turn]] = []
        for t in turns:
            if out and t.when - out[-1][-1].when < self.idle:
                out[-1].append(t)
            else:
                out.append([t])
        return out

    def facts(self, turns: list[Turn]) -> dict[str, list[str]]:
        """会話から覚えておくことを書き出し、種類ごとに分ける。"""
        talk = "\n".join(line for t in turns for line in t.lines())
        answer = self._ask([{"role": "system", "content": FACTS_PROMPT}, {"role": "user", "content": talk}], 600)
        out: dict[str, list[str]] = {k: [] for k in ("プロフィール", *NOW_KINDS, "済み", "忘れる")}
        for line in answer.splitlines():
            line = line.strip().lstrip("-・* ").strip()
            kind, sep, text = line.replace("：", ":").partition(":")
            text = text.strip().rstrip("。")
            if sep and kind.strip() in out and text and text not in NOTHING:
                out[kind.strip()].append(text)
        return out

    def update_notes(self, turns: list[Turn]) -> None:
        with self.work_lock:
            when = turns[-1].when if turns else datetime.now()
            f = self.facts(turns)
            # NOW.md: 済み・忘れることを消して、新しいことを足す（頭脳に書き直させず、プログラムで行う）
            gone = f["済み"] + f["忘れる"]
            items = [it for it in self.now_items(when)
                     if not any(max(overlap(g, it[1]), overlap(it[1], g)) >= 0.4 for g in gone)]
            for kind in NOW_KINDS:
                for text in f[kind]:
                    if not any(k == kind and overlap(text, t) >= 0.8 for k, t, _ in items):
                        items.append((kind, text, when))
            self.write_now(items)
            # PROFILE.md: 足すこと・忘れることがあるときだけ、頭脳に書き直させる
            if f["プロフィール"] or f["忘れる"]:
                change = "【足すこと】\n" + ("\n".join(f"- {x}" for x in f["プロフィール"]) or "（なし）")
                change += "\n\n【忘れること】\n" + ("\n".join(f"- {x}" for x in f["忘れる"]) or "（なし）")
                self._rewrite(self.profile_md, PROFILE_PROMPT.format(max_lines=MAX_NOTE_LINES), change)

    # ---------------------------------------------------------------- NOW.md
    def now_items(self, now: datetime | None = None) -> list[tuple[str, str, datetime | None]]:
        """NOW.md の中身（期限切れは除く）。手で書いた日時の無い行は、ずっと残す。"""
        now = now or datetime.now()
        items = []
        for line in self.read_note(self.now_md).splitlines():
            m = NOW_LINE_RE.match(line.strip())
            if m:
                when = datetime.strptime(m.group(3), "%Y-%m-%d %H:%M")
                if now - when < NOW_KINDS[m.group(1)]:
                    items.append((m.group(1), m.group(2), when))
            elif line.strip().startswith("- "):
                items.append(("メモ", line.strip()[2:], None))
        return items

    def write_now(self, items: list[tuple[str, str, datetime | None]]) -> None:
        lines = [f"- {text}" if when is None else f"- [{kind}] {text}（{when:%Y-%m-%d %H:%M}）" for kind, text, when in items]
        text = "\n".join(lines) + ("\n" if lines else "")
        with self.lock:
            if text != (self.now_md.read_text(encoding="utf-8") if self.now_md.exists() else ""):
                self.now_md.write_text(text, encoding="utf-8")
                log.info("NOW.md を更新しました（%d 行）", len(lines))

    def _rewrite(self, path: Path, prompt: str, talk: str) -> None:
        old = self.read_note(path)
        if not old and not talk:
            return
        content = f"【今のプロフィール】\n{old or '（まだ無い）'}" + (f"\n\n{talk}" if talk else "")
        answer = self._ask([{"role": "system", "content": prompt}, {"role": "user", "content": content}], 1200)
        lines = [ln.strip() for ln in answer.splitlines() if ln.strip().startswith("- ")][:MAX_NOTE_LINES]
        new = "\n".join(lines)
        forget = "【忘れること】\n- " in talk
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
        now = datetime.now()
        parts = []
        profile = self.read_note(self.profile_md)
        if profile:
            parts.append("相手について:\n" + profile)
        items = self.now_items(now)
        if items:
            parts.append("相手の今のこと:\n" + "\n".join(
                f"- {text}" if when is None else f"- [{kind}] {text}（{ago(when, now)}）" for kind, text, when in items))
        rel = self.related(query)
        if rel:
            parts.append("関係がありそうな前の会話:\n" + "\n".join(f"- {ago(datetime.fromisoformat(s), now)}: {t}" for s, t in rel))
        return CONTEXT_HEAD + "\n" + "\n\n".join(parts) if parts else ""

    # ---------------------------------------------------------------- 話しかける話題
    def topic(self, rng: random.Random | None = None) -> str:
        """キャラから話しかけるときの話題の種。今のこと・前の会話・相手のこと・励まし・雑談から選ぶ。"""
        rng = rng or random.Random()
        now = datetime.now()
        recent = self.episodes()[-5:]
        items = self.now_items(now)
        profile = [ln[2:] for ln in self.read_note(self.profile_md).splitlines() if ln.startswith("- ")]
        promises = [it for it in items if it[0] == "約束"]
        states = [it for it in items if it[0] != "約束"]
        choices = [
            ("約束", 4, lambda: "約束ややりかけのことに触れる（楽しみにしている・どうなったか聞く）: " + rng.choice(promises)[1]) if promises else None,
            ("今のこと", 4, lambda: "相手の今の状況を気にかけたり、どうなったか聞いたりする: " + rng.choice(states)[1]) if states else None,
            ("前の会話の続き", 3, lambda: "この前の話の続きを聞く。前の会話: " + rng.choice(recent)[2]) if recent else None,
            ("相手のこと", 2, lambda: "相手の性格や好みに合わせた話をする: " + rng.choice(profile)) if profile else None,
            ("励まし", 1, lambda: "相手が今していることを応援したり、がんばりをほめたりする"),
            ("休憩", 1, lambda: "相手を気づかう（休憩・水分・姿勢など）。押しつけない"),
            ("時間帯", 1, lambda: f"今の時間（{now.hour} 時ごろ）らしい話をする（ごはん・眠さ・一日の様子など）"),
            ("雑談", 1, lambda: "思いついた雑談や、相手への質問をする（好きなもの・最近のこと・今日あったこと）"),
        ]
        choices = [c for c in choices if c]
        _, _, make = rng.choices(choices, weights=[w for _, w, _ in choices])[0]
        return make()


def ago(when: datetime, now: datetime) -> str:
    """「さっき」「3 時間前」「昨日」「5 日前」。"""
    d = now - when
    if d < timedelta(hours=1):
        return "さっき"
    if d < timedelta(days=1):
        return f"{int(d.total_seconds() // 3600)} 時間前"
    if d < timedelta(days=2):
        return "昨日"
    return f"{d.days} 日前"


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
