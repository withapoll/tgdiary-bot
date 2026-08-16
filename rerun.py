#!/usr/bin/env python3
"""
Прогнать скилл на посте вручную И ОТПРАВИТЬ результат в комментарии.

Нужно, когда реакция «съедена» впустую: бот получил обновление и записал его
в state.json, но не успел доработать (был остановлен, упал, перезапущен).
Telegram повторно такое обновление не пришлёт — счётчик уже не меняется.

Использование:
    python rerun.py 6 research
    python rerun.py 6 oppose
    python rerun.py --list            # показать посты в архиве
    python rerun.py --reset 6         # забыть реакции поста, чтобы сработал заново
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import bot as botmod          # noqa: E402
from agent import Agent       # noqa: E402
from store import Store       # noqa: E402


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    store = Store(BASE / "data")

    if args[0] == "--list":
        for p in store.all_posts():
            link = store.get_thread(p["post_id"])
            mark = "✓" if link else "✗ нет связи с комментариями"
            print(f"#{p['post_id']:<4} {mark:<28} {(p.get('text') or '')[:70]!r}")
        return

    if args[0] == "--reset":
        pid = args[1]
        store.set_reactions(pid, {})
        print(f"[+] Реакции поста #{pid} забыты — можно поставить эмодзи заново.")
        return

    post_id = int(args[0])
    skill = args[1] if len(args) > 1 else "oppose"

    post = store.get_post(post_id)
    if not post:
        print(f"[!] Поста #{post_id} нет в архиве. Посмотри: python rerun.py --list")
        return

    print(f"[.] Пост #{post_id}: {(post.get('text') or '')[:90]!r}")
    print(f"[.] Скилл: {skill}. Ресёрч может занять 1-3 минуты...\n")

    tg = botmod.Telegram(cfg["bot_token"])
    agent = Agent(cfg, store)
    botmod.run_skill(skill, post_id, tg, store, agent, cfg)


if __name__ == "__main__":
    main()
