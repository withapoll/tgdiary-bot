#!/usr/bin/env python3
"""
tgdiary — агент-оппонент над каналом-дневником.
Long polling, только stdlib. Запуск: python bot.py

Архитектура (проверено по core.telegram.org/bots/api):
  1. channel_post            -> сохраняем пост (голосовые транскрибируем)
  2. message с is_automatic_forward=True в группе обсуждений
                             -> запоминаем связь post_id -> message_id в группе.
                                Это ЕДИНСТВЕННЫЙ способ ответить в комментарии.
  3. message_reaction_count  -> в каналах реакции анонимные, приходит только
                                счётчик, с задержкой до нескольких минут.
                                Диффим со стороженным состоянием -> новый эмодзи -> скилл.
"""
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from store import Store          # noqa: E402
from agent import Agent          # noqa: E402
from transcribe import transcribe_file  # noqa: E402

CFG_PATH = BASE / "config.json"


def load_config():
    if not CFG_PATH.exists():
        print(f"[!] Нет {CFG_PATH}. Скопируй config.example.json -> config.json и заполни.")
        sys.exit(1)
    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("bot_token") or cfg["bot_token"].startswith("PASTE"):
        print("[!] Не задан bot_token в config.json")
        sys.exit(1)
    return cfg


class Telegram:
    def __init__(self, token, timeout=70):
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.timeout = timeout

    def call(self, method, **params):
        url = f"{self.base}/{method}"
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            print(f"[tg:{method}] HTTP {e.code}: {body[:300]}")
            return None
        except Exception as e:
            print(f"[tg:{method}] {type(e).__name__}: {e}")
            return None
        if not out.get("ok"):
            print(f"[tg:{method}] not ok: {out}")
            return None
        return out.get("result")

    def download(self, file_id, dest_dir):
        info = self.call("getFile", file_id=file_id)
        if not info or "file_path" not in info:
            return None
        remote = info["file_path"]
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{file_id}_{Path(remote).name}"
        if dest.exists():
            return dest
        try:
            with urllib.request.urlopen(f"{self.file_base}/{remote}", timeout=120) as r:
                dest.write_bytes(r.read())
            return dest
        except Exception as e:
            print(f"[tg:download] {e}")
            return None


def extract_text(msg):
    """Текст поста + пометка о типе носителя."""
    if msg.get("text"):
        return msg["text"], "text"
    if msg.get("caption"):
        return msg["caption"], "caption"
    return "", "media"


def main():
    cfg = load_config()
    tg = Telegram(cfg["bot_token"])
    store = Store(BASE / "data")
    agent = Agent(cfg, store)

    me = tg.call("getMe")
    if not me:
        print("[!] getMe не прошёл — проверь токен.")
        sys.exit(1)
    print(f"[+] Бот @{me.get('username')} запущен. Ctrl+C для остановки.")
    print(f"[+] Постов в архиве: {store.count_posts()}")

    channel_id = cfg.get("channel_id")          # -100...
    reactions_map = cfg.get("reactions", {})
    allowed = ["channel_post", "message_reaction_count", "message", "edited_channel_post"]

    offset = store.get_state("update_offset", 0)

    # ---------- ДОГОНЯЮЩИЙ РЕЖИМ ----------
    # Бот не обязан работать круглосуточно. При старте:
    #   1) вычитываем всё, что Telegram накопил, пока он был выключен
    #      (обновления хранятся на стороне Telegram ~24 часа);
    #   2) обрабатываем реакции, которые сохранены, но не отработаны.
    print("[.] Догоняю пропущенное...")
    drained = 0
    while True:
        batch = tg.call("getUpdates", offset=offset, timeout=0,
                        allowed_updates=allowed)
        if not batch:
            break
        for upd in batch:
            offset = upd["update_id"] + 1
            store.set_state("update_offset", offset)
            try:
                handle(upd, tg, store, agent, cfg, channel_id, reactions_map,
                       defer=True)
            except Exception:
                print("[!] Ошибка при разборе пропущенного апдейта:")
                traceback.print_exc()
            drained += 1
        if drained > 500:
            break
    print(f"[+] Разобрано пропущенных обновлений: {drained}")

    todo = store.pending(reactions_map, resolve_skill)
    if todo:
        print(f"[+] Реакции без ответа: {len(todo)} — обрабатываю.")
        for post_id, emoji, skill in todo:
            print(f"[>] Догоняю: {emoji} на посте #{post_id} -> {skill}")
            try:
                run_skill(skill, post_id, tg, store, agent, cfg)
            except Exception:
                print("[!] Ошибка догоняющей обработки:")
                traceback.print_exc()
    else:
        print("[+] Необработанных реакций нет.")
    print("[+] Слушаю новые события.\n")

    while True:
        try:
            updates = tg.call("getUpdates", offset=offset, timeout=50,
                              allowed_updates=allowed)
        except Exception:
            print("[!] Сбой getUpdates:")
            traceback.print_exc()
            time.sleep(5)
            continue

        if updates is None:
            time.sleep(5)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                store.set_state("update_offset", offset)
            except Exception:
                print("[!] Не удалось сохранить offset:")
                traceback.print_exc()
            try:
                handle(upd, tg, store, agent, cfg, channel_id, reactions_map)
            except Exception:
                print("[!] Ошибка обработки апдейта:")
                traceback.print_exc()


def handle(upd, tg, store, agent, cfg, channel_id, reactions_map, defer=False):
    # ---------- 1. Новый пост в канале ----------
    if "channel_post" in upd:
        msg = upd["channel_post"]
        if channel_id and str(msg["chat"]["id"]) != str(channel_id):
            return
        text, kind = extract_text(msg)

        # голосовое / видео-кружок / аудио -> транскрибация
        media = msg.get("voice") or msg.get("video_note") or msg.get("audio")
        if media:
            path = tg.download(media["file_id"], BASE / "data" / "media")
            if path:
                print(f"[.] Транскрибирую {path.name} ...")
                tr = transcribe_file(path, cfg)
                if tr:
                    text = (text + "\n" + tr).strip() if text else tr
                    kind = "voice"
                    print(f"[+] Транскрипт: {tr[:120]}...")

        store.add_post(
            post_id=msg["message_id"],
            chat_id=msg["chat"]["id"],
            date=msg.get("date"),
            text=text,
            kind=kind,
        )
        print(f"[+] Пост #{msg['message_id']} сохранён ({kind}, {len(text)} симв.)")
        return

    # ---------- 2. Автофорвард в группу обсуждений ----------
    if "message" in upd:
        msg = upd["message"]
        if msg.get("is_automatic_forward"):
            orig = msg.get("forward_origin") or {}
            orig_id = (
                orig.get("message_id")
                or (msg.get("forward_from_message_id"))
            )
            if orig_id:
                store.link_thread(
                    post_id=orig_id,
                    group_chat_id=msg["chat"]["id"],
                    group_msg_id=msg["message_id"],
                )
                print(f"[+] Связал пост #{orig_id} с комментариями "
                      f"({msg['chat']['id']}/{msg['message_id']})")
            return

        # Ручные команды в группе/личке
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            handle_command(text, msg, tg, store, agent)
        return

    # ---------- 3. Реакции (анонимные, только счётчики) ----------
    if "message_reaction_count" in upd:
        ev = upd["message_reaction_count"]
        if channel_id and str(ev["chat"]["id"]) != str(channel_id):
            return
        post_id = ev["message_id"]
        current = {}
        for r in ev.get("reactions", []):
            t = r.get("type", {})
            if t.get("type") == "emoji":
                current[t["emoji"]] = r.get("total_count", 0)

        previous = store.get_reactions(post_id)
        store.set_reactions(post_id, current)

        # В догоняющем режиме только фиксируем реакции: посты из этой же пачки
        # могут прийти позже реакций на них. Обработка — вторым проходом.
        if defer:
            return

        # новые эмодзи или выросший счётчик = новый жест
        for emoji, cnt in current.items():
            if cnt > previous.get(emoji, 0):
                skill = resolve_skill(emoji, reactions_map)
                if not skill:
                    print(f"[.] Реакция {emoji} без скилла — игнор.")
                    continue
                print(f"[>] Реакция {emoji} на посте #{post_id} -> скилл '{skill}'")
                run_skill(skill, post_id, tg, store, agent, cfg)
        return


def run_skill(skill, post_id, tg, store, agent, cfg=None):
    post = store.get_post(post_id)
    if not post:
        print(f"[!] Пост #{post_id} не найден в архиве — пропускаю.")
        return

    answer = agent.run(skill, post)
    if not answer:
        print("[.] Агент решил промолчать (это нормальный исход).")
        store.mark_handled(post_id, skill)   # молчание — тоже результат, не повторяем
        return

    link = store.get_thread(post_id)
    if link:
        target_chat = link["group_chat_id"]
        reply_to = link["group_msg_id"]
    else:
        # Фолбэк: автофорвард в группу не пойман (например, пост опубликован
        # до старта бота). Шлём в группу обсуждений без привязки к посту,
        # процитировав начало — лучше, чем потерять ответ.
        target_chat = (cfg or {}).get("discussion_chat_id")
        reply_to = None
        if not target_chat:
            print("[!] Нет связи с группой обсуждений и не задан discussion_chat_id.")
            print(f"    Ответ агента:\n{answer}")
            return
        quote = (post.get("text") or "")[:80].replace("\n", " ")
        answer = f"<i>к посту «{quote}…»</i>\n\n{answer}"
        print("[.] Связи с постом нет — отправляю в группу обсуждений без привязки.")

    sent = False
    for chunk in split_message(answer):
        params = dict(
            chat_id=target_chat,
            text=chunk,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
        )
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to}
        if tg.call("sendMessage", **params) is not None:
            sent = True

    if sent:
        store.mark_handled(post_id, skill)
        print(f"[+] Ответ отправлен (пост #{post_id})")
    else:
        print(f"[!] Отправка не удалась (пост #{post_id}) — "
              f"попробую снова при следующем запуске.")


def handle_command(text, msg, tg, store, agent):
    chat_id = msg["chat"]["id"]
    parts = text.split(maxsplit=1)
    cmd = parts[0].lstrip("/").split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "find":
        if not arg:
            tg.call("sendMessage", chat_id=chat_id, text="Использование: /find <запрос>")
            return
        answer = agent.search_archive(arg)
        for chunk in split_message(answer or "Ничего не нашёл."):
            tg.call("sendMessage", chat_id=chat_id, text=chunk, parse_mode="HTML",
                    link_preview_options={"is_disabled": True})

    elif cmd == "digest":
        answer = agent.weekly_digest()
        for chunk in split_message(answer or "Нечего сказать за период."):
            tg.call("sendMessage", chat_id=chat_id, text=chunk, parse_mode="HTML",
                    link_preview_options={"is_disabled": True})

    elif cmd == "stats":
        s = store.stats()
        tg.call("sendMessage", chat_id=chat_id, text=(
            f"Постов: {s['posts']}\n"
            f"Голосовых: {s['voice']}\n"
            f"Связано с комментариями: {s['linked']}\n"
            f"Ответов агента: {s['answers']}\n"
            f"Промолчал: {s['silences']}"
        ))

    elif cmd in ("start", "help"):
        tg.call("sendMessage", chat_id=chat_id, text=(
            "Агент-оппонент над каналом-дневником.\n\n"
            "Реакции на пост в канале:\n"
            "🤔 — возрази\n🔍 — ресёрч\n📌 — запомни как решение\n"
            "👀 — что я писал об этом раньше\n\n"
            "Команды:\n/find <запрос> — поиск по архиву\n"
            "/digest — недельный дайджест\n/stats — статистика теста"
        ))


def norm_emoji(e):
    """
    Приводит эмодзи к канонической форме для сопоставления с конфигом.
    Telegram может прислать ✍ (U+270D) без вариационного селектора U+FE0F,
    а в config.json записано ✍️ (с ним) — это разные строки, ключ не совпадёт.
    Убираем FE0F/FE0E, ZWJ-последовательности (👨‍💻) не трогаем.
    """
    return "".join(c for c in (e or "") if c not in ("\ufe0f", "\ufe0e"))


def resolve_skill(emoji, reactions_map):
    """Ищет скилл по эмодзи с учётом нормализации в обе стороны."""
    if emoji in reactions_map:
        return reactions_map[emoji]
    target = norm_emoji(emoji)
    for key, skill in reactions_map.items():
        if norm_emoji(key) == target:
            return skill
    return None


def split_message(text, limit=4000):
    out, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > limit:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out or [text[:limit]]


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[+] Остановлен.")
    except Exception:
        # Крах не должен уходить в пустоту: пишем и в консоль, и в файл,
        # иначе окно закрывается и причина теряется.
        print("\n[!!!] БОТ УПАЛ:")
        traceback.print_exc()
        try:
            crash = BASE / "data" / "crash.log"
            crash.parent.mkdir(parents=True, exist_ok=True)
            with open(crash, "a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                traceback.print_exc(file=f)
            print(f"[i] Трассировка записана в {crash}")
        except Exception:
            pass
        raise
