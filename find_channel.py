#!/usr/bin/env python3
"""
Узнать channel_id приватного канала.

Запуск:
    python find_channel.py                 # берёт токен из config.json
    python find_channel.py <>     # или передай явно

Порядок действий:
  1. Добавь бота в АДМИНЫ приватного канала.
  2. Напиши в канал любой пост (например «тест»).
  3. Запусти этот скрипт.

Скрипт покажет id канала и связанной группы обсуждений, проверит права бота
и (по желанию) сам пропишет channel_id в config.json.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG = BASE / "config.json"


def api(token, method, **params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", "ignore") or '{"ok":false}')
    except Exception as e:
        print(f"[!] Сеть: {type(e).__name__}: {e}")
        return {"ok": False}


def main():
    token = None
    if len(sys.argv) > 1:
        token = sys.argv[1].strip()
    elif CFG.exists():
        token = json.loads(CFG.read_text(encoding="utf-8")).get("bot_token", "")

    if not token or token.startswith("PASTE"):
        print("Укажи токен: python find_channel.py <>")
        print("или заполни bot_token в config.json")
        sys.exit(1)

    me = api(token, "getMe")
    if not me.get("ok"):
        print(f"[!] getMe не прошёл: {me.get('description', me)}")
        print("    Токен неверный или отозван.")
        sys.exit(1)
    bot = me["result"]
    print(f"[+] Бот: @{bot.get('username')} (id {bot.get('id')})\n")

    upd = api(token, "getUpdates", timeout=5, limit=100,
              allowed_updates=["channel_post", "message", "message_reaction_count",
                               "my_chat_member"])
    if not upd.get("ok"):
        print(f"[!] getUpdates: {upd.get('description')}")
        sys.exit(1)

    results = upd.get("result", [])
    if not results:
        print("[!] Апдейтов нет. Значит:")
        print("    - бот не добавлен в админы канала, ИЛИ")
        print("    - в канале нет ни одного поста после добавления бота, ИЛИ")
        print("    - апдейты уже вычитаны запущенным bot.py (останови его и повтори)")
        print("\n    Напиши в канал любой пост и запусти скрипт снова.")
        sys.exit(0)

    chats = {}
    for u in results:
        for key in ("channel_post", "edited_channel_post", "message"):
            m = u.get(key)
            if not m:
                continue
            c = m.get("chat", {})
            chats[c.get("id")] = {
                "type": c.get("type"),
                "title": c.get("title") or c.get("username") or "(лично)",
                "auto_forward": m.get("is_automatic_forward", False),
            }
        mcm = u.get("my_chat_member")
        if mcm:
            c = mcm.get("chat", {})
            chats.setdefault(c.get("id"), {
                "type": c.get("type"),
                "title": c.get("title", ""),
                "auto_forward": False,
            })
        mrc = u.get("message_reaction_count")
        if mrc:
            c = mrc.get("chat", {})
            chats.setdefault(c.get("id"), {
                "type": c.get("type"), "title": c.get("title", ""), "auto_forward": False})

    print("Найденные чаты:\n")
    channel_id = None
    group_id = None
    for cid, info in chats.items():
        mark = ""
        if info["type"] == "channel":
            mark = "  <-- ЭТО КАНАЛ, его id в config.json"
            channel_id = cid
        elif info["type"] in ("supergroup", "group"):
            mark = "  <-- группа обсуждений (комментарии)"
            group_id = cid
        print(f"  {cid}   [{info['type']}] {info['title']}{mark}")

    print()

    # Проверка прав в канале
    if channel_id:
        adm = api(token, "getChatMember", chat_id=channel_id, user_id=bot["id"])
        if adm.get("ok"):
            status = adm["result"].get("status")
            print(f"[{'+' if status == 'administrator' else '!'}] Статус бота в канале: {status}")
            if status != "administrator":
                print("    Бот ДОЛЖЕН быть админом канала, иначе реакции не придут.")
        chat = api(token, "getChat", chat_id=channel_id)
        if chat.get("ok"):
            linked = chat["result"].get("linked_chat_id")
            if linked:
                print(f"[+] Группа обсуждений привязана: {linked}")
                group_id = group_id or linked
            else:
                print("[!] К каналу НЕ привязана группа обсуждений.")
                print("    Без неё бот не сможет отвечать в комментарии.")
                print("    Управление каналом -> Обсуждения -> создать/привязать группу.")

    if group_id:
        adm = api(token, "getChatMember", chat_id=group_id, user_id=bot["id"])
        if adm.get("ok"):
            st = adm["result"].get("status")
            print(f"[{'+' if st in ('administrator', 'member') else '!'}] "
                  f"Статус бота в группе обсуждений: {st}")
            if st not in ("administrator", "member"):
                print("    Добавь бота в группу обсуждений с правом писать сообщения.")

    # Запись в конфиг
    if channel_id and CFG.exists():
        cfg = json.loads(CFG.read_text(encoding="utf-8"))
        if str(cfg.get("channel_id", "")) != str(channel_id):
            ans = input(f"\nЗаписать channel_id = {channel_id} в config.json? [y/N] ")
            if ans.strip().lower() in ("y", "yes", "д", "да"):
                cfg["channel_id"] = str(channel_id)
                CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                print("[+] Записано.")
        else:
            print("\n[+] channel_id в config.json уже верный.")

    print("\nПодсказка: id приватного канала всегда начинается с -100")


if __name__ == "__main__":
    main()
