#!/usr/bin/env python3
"""
Прогон логики без реального Telegram и без LLM.
Проверяет: сохранение поста -> связь с комментариями -> дифф реакций -> вызов скилла.
Запуск: python selftest.py
"""
import json
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

TMP = BASE / "data_test"
if TMP.exists():
    shutil.rmtree(TMP)

import bot as botmod          # noqa: E402
from store import Store       # noqa: E402
from agent import Agent       # noqa: E402

CALLS = []


class FakeTG:
    def call(self, method, **params):
        CALLS.append((method, params))
        return {"ok": True}

    def download(self, *a, **k):
        return None


class FakeAgent(Agent):
    """Агент без сети: воспроизводит поведение SILENCE / обычного ответа."""
    def _complete(self, system, user, skill=None):
        if "выгорел" in user or "устал" in user:
            return "SILENCE"
        if "кашей" in user or "@#$" in user:
            return "UNCLEAR: расшифровка обрывается на середине фразы"
        if "отличная" in user:
            return "Отличная идея, но есть нюанс. Данных по рынку нет."
        return "Слабое место: ты берёшь конверсию 8% как данность. У slaash формы нет в hero — до неё доскроллит меньше половины."


def show(title):
    print(f"\n{'='*60}\n{title}\n{'='*60}")


def main():
    store = Store(TMP)
    agent = FakeAgent({"llm": {}}, store)
    tg = FakeTG()
    cfg = {}
    reactions = {"🤔": "oppose", "🔍": "research", "📌": "remember", "👀": "recall"}
    CH = -1001234567890

    show("1. Приходит пост в канал")
    upd = {"update_id": 1, "channel_post": {
        "message_id": 101, "chat": {"id": CH}, "date": 1755200000,
        "text": "Думаю делать вейтлист-лендинг для Netwrk, целюсь в конверсию 8%."}}
    botmod.handle(upd, tg, store, agent, cfg, CH, reactions)
    assert store.get_post(101), "пост не сохранён"
    print("OK: пост сохранён,", store.count_posts(), "шт.")

    show("2. Автофорвард в группу обсуждений (связь для комментариев)")
    upd = {"update_id": 2, "message": {
        "message_id": 5001, "chat": {"id": -1009876543210},
        "is_automatic_forward": True,
        "forward_origin": {"type": "channel", "message_id": 101}}}
    botmod.handle(upd, tg, store, agent, cfg, CH, reactions)
    link = store.get_thread(101)
    assert link, "связь не создана"
    print("OK: связь ->", link)

    show("3. Реакция 🤔 -> скилл oppose -> ответ в комментарии")
    CALLS.clear()
    upd = {"update_id": 3, "message_reaction_count": {
        "chat": {"id": CH}, "message_id": 101,
        "reactions": [{"type": {"type": "emoji", "emoji": "🤔"}, "total_count": 1}]}}
    botmod.handle(upd, tg, store, agent, cfg, CH, reactions)
    assert CALLS, "бот не отправил ответ"
    m, p = CALLS[0]
    assert m == "sendMessage" and p["chat_id"] == -1009876543210
    assert p["reply_parameters"]["message_id"] == 5001
    print("OK: sendMessage в группу, reply на", p["reply_parameters"]["message_id"])
    print("   текст:", p["text"][:90], "...")

    show("4. Повторный тот же счётчик -> НЕ должен сработать снова")
    CALLS.clear()
    botmod.handle(upd, tg, store, agent, cfg, CH, reactions)
    assert not CALLS, "сработал повторно на той же реакции!"
    print("OK: дубль реакции проигнорирован")

    show("5. Личный пост -> агент обязан молчать (правило 4)")
    CALLS.clear()
    store.add_post(102, CH, 1755300000, "Что-то я выгорел, ничего не хочу делать.")
    store.link_thread(102, -1009876543210, 5002)
    upd = {"update_id": 5, "message_reaction_count": {
        "chat": {"id": CH}, "message_id": 102,
        "reactions": [{"type": {"type": "emoji", "emoji": "🤔"}, "total_count": 1}]}}
    botmod.handle(upd, tg, store, agent, cfg, CH, reactions)
    assert not CALLS, "агент влез в личное!"
    print("OK: промолчал на личном посте")

    show("6. Кривой транскрипт -> UNCLEAR, а не выводы")
    CALLS.clear()
    store.add_post(103, CH, 1755400000, "это всё кашей @#$ непонятно что")
    store.link_thread(103, -1009876543210, 5003)
    upd = {"update_id": 6, "message_reaction_count": {
        "chat": {"id": CH}, "message_id": 103,
        "reactions": [{"type": {"type": "emoji", "emoji": "🤔"}, "total_count": 1}]}}
    botmod.handle(upd, tg, store, agent, cfg, CH, reactions)
    assert CALLS and "⚠️" in CALLS[0][1]["text"]
    print("OK: вернул переспрос:", CALLS[0][1]["text"][:70])

    show("7. Страховка «не хвали» режет похвалу")
    out = agent._postprocess("Отличная идея, но есть нюанс. Данных по рынку нет.", 101, "oppose")
    print("   после фильтра:", repr(out))
    assert out is None or "тличная идея" not in out
    print("OK: похвала вырезана")

    show("8. Реакция без скилла -> игнор")
    CALLS.clear()
    upd = {"update_id": 8, "message_reaction_count": {
        "chat": {"id": CH}, "message_id": 101,
        "reactions": [{"type": {"type": "emoji", "emoji": "🤔"}, "total_count": 1},
                      {"type": {"type": "emoji", "emoji": "❤"}, "total_count": 1}]}}
    botmod.handle(upd, tg, store, agent, cfg, CH, reactions)
    assert not CALLS, "сработал на нераспознанном эмодзи"
    print("OK: ❤ без скилла проигнорирован")

    show("9. Поиск по архиву")
    res = agent.search_archive("вейтлист конверсия")
    print("   ", (res or "")[:110], "...")
    assert res

    show("10. Метрики теста")
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))

    shutil.rmtree(TMP)
    print("\n\n  ВСЕ 10 ПРОВЕРОК ПРОШЛИ  \n")


if __name__ == "__main__":
    main()
