#!/usr/bin/env python3
"""Хранилище: JSONL для постов, JSON для состояния. Никаких БД — это тест."""
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.posts_path = self.dir / "posts.jsonl"
        self.state_path = self.dir / "state.json"
        self.log_path = self.dir / "agent_log.jsonl"
        self._lock = threading.Lock()
        self._state = self._load_state()
        self._posts = self._load_posts()

    # ---------- state ----------
    def _load_state(self):
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"update_offset": 0, "threads": {}, "reactions": {}}

    def _save_state(self):
        # На Windows replace() может упасть с PermissionError, если файл
        # в этот момент открыт другим процессом (например, ручной проверкой).
        # Раньше это роняло бота без следа — теперь ретраим.
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8")
        last_err = None
        for _ in range(5):
            try:
                tmp.replace(self.state_path)
                return
            except PermissionError as e:
                last_err = e
                time.sleep(0.2)
        print(f"[!] Не удалось сохранить state.json: {last_err}")

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def set_state(self, key, value):
        with self._lock:
            self._state[key] = value
            self._save_state()

    # ---------- posts ----------
    def _load_posts(self):
        posts = {}
        if self.posts_path.exists():
            for line in self.posts_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    p = json.loads(line)
                    posts[int(p["post_id"])] = p
                except Exception:
                    continue
        return posts

    def add_post(self, post_id, chat_id, date, text, kind="text", source="live"):
        post = {
            "post_id": int(post_id),
            "chat_id": chat_id,
            "date": date,
            "iso": datetime.fromtimestamp(date, tz=timezone.utc).isoformat() if date else None,
            "text": text,
            "kind": kind,
            "source": source,
        }
        with self._lock:
            self._posts[int(post_id)] = post
            with open(self.posts_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(post, ensure_ascii=False) + "\n")
        return post

    def get_post(self, post_id):
        return self._posts.get(int(post_id))

    def all_posts(self):
        return sorted(self._posts.values(), key=lambda p: p.get("date") or 0)

    def count_posts(self):
        return len(self._posts)

    def recent_posts(self, n=40):
        return self.all_posts()[-n:]

    def search(self, query, limit=12):
        """Наивный поиск по подстрокам — для теста достаточно."""
        words = [w.lower() for w in query.split() if len(w) > 2]
        scored = []
        for p in self._posts.values():
            t = (p.get("text") or "").lower()
            if not t:
                continue
            score = sum(t.count(w) for w in words)
            if score:
                scored.append((score, p))
        scored.sort(key=lambda x: (-x[0], -(x[1].get("date") or 0)))
        return [p for _, p in scored[:limit]]

    # ---------- связь пост <-> комментарии ----------
    def link_thread(self, post_id, group_chat_id, group_msg_id):
        with self._lock:
            self._state.setdefault("threads", {})[str(post_id)] = {
                "group_chat_id": group_chat_id,
                "group_msg_id": group_msg_id,
            }
            self._save_state()

    def get_thread(self, post_id):
        return self._state.get("threads", {}).get(str(post_id))

    # ---------- реакции ----------
    def get_reactions(self, post_id):
        return self._state.get("reactions", {}).get(str(post_id), {})

    def set_reactions(self, post_id, mapping):
        with self._lock:
            self._state.setdefault("reactions", {})[str(post_id)] = mapping
            self._save_state()

    # ---------- что уже отработано ----------
    # Ключевое для догоняющего режима: реакция может быть СОХРАНЕНА,
    # но не обработана (бот убит/перезапущен посреди работы). Telegram такое
    # обновление повторно не пришлёт — счётчик с тех пор не менялся.
    # Поэтому факт обработки пишем отдельно.
    def is_handled(self, post_id, skill):
        return skill in self._state.get("handled", {}).get(str(post_id), [])

    def mark_handled(self, post_id, skill):
        with self._lock:
            lst = self._state.setdefault("handled", {}).setdefault(str(post_id), [])
            if skill not in lst:
                lst.append(skill)
            self._save_state()

    def pending(self, reactions_map, resolve):
        """Список (post_id, emoji, skill), где реакция стоит, а обработки не было."""
        out = []
        for pid, emojis in self._state.get("reactions", {}).items():
            for emoji, cnt in (emojis or {}).items():
                if not cnt:
                    continue
                skill = resolve(emoji, reactions_map)
                if skill and not self.is_handled(pid, skill):
                    out.append((int(pid), emoji, skill))
        return sorted(out)

    # ---------- лог для метрик теста ----------
    def log(self, kind, post_id=None, skill=None, payload=None):
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "post_id": post_id,
            "skill": skill,
            "payload": payload,
        }
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def stats(self):
        answers = silences = 0
        if self.log_path.exists():
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                if '"answer"' in line:
                    answers += 1
                elif '"silence"' in line:
                    silences += 1
        return {
            "posts": len(self._posts),
            "voice": sum(1 for p in self._posts.values() if p.get("kind") == "voice"),
            "linked": len(self._state.get("threads", {})),
            "answers": answers,
            "silences": silences,
        }
