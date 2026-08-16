#!/usr/bin/env python3
"""
Импорт архива из Telegram Desktop (Export chat history -> JSON) в store.
Запуск: python import_export.py path/to/result.json [--transcribe]

Без архива «архивариус» не проверяется: противоречия видны на горизонте месяцев,
а за две недели теста они не накопятся.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from store import Store  # noqa: E402


def flatten_text(entity):
    """text в экспорте бывает строкой или списком фрагментов."""
    if isinstance(entity, str):
        return entity
    if isinstance(entity, list):
        parts = []
        for it in entity:
            if isinstance(it, str):
                parts.append(it)
            elif isinstance(it, dict):
                parts.append(it.get("text", ""))
        return "".join(parts)
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="result.json из Telegram Desktop")
    ap.add_argument("--transcribe", action="store_true",
                    help="транскрибировать голосовые (нужен config.json и media рядом)")
    ap.add_argument("--limit", type=int, default=0, help="взять только N последних")
    args = ap.parse_args()

    src = Path(args.path)
    if not src.exists():
        print(f"[!] Нет файла {src}")
        sys.exit(1)

    data = json.loads(src.read_text(encoding="utf-8"))
    messages = data.get("messages", [])
    print(f"[+] В экспорте сообщений: {len(messages)}")

    cfg = {}
    if args.transcribe:
        cfg_path = BASE / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        else:
            print("[!] config.json не найден — транскрибация выключена.")
            args.transcribe = False

    store = Store(BASE / "data")
    added = voice = skipped = 0
    export_root = src.parent

    items = messages[-args.limit:] if args.limit else messages

    for m in items:
        if m.get("type") != "message":
            continue
        mid = m.get("id")
        if mid is None or store.get_post(mid):
            skipped += 1
            continue

        text = flatten_text(m.get("text", "")).strip()
        kind = "text"

        media_path = m.get("file") or m.get("media_file")
        is_voice = m.get("media_type") in ("voice_message", "video_message", "audio_file")

        if is_voice and args.transcribe and media_path:
            full = export_root / media_path
            if full.exists():
                from transcribe import transcribe_file
                print(f"[.] {full.name} ...")
                tr = transcribe_file(full, cfg)
                if tr:
                    text = (text + "\n" + tr).strip() if text else tr
                    kind = "voice"
                    voice += 1
            else:
                print(f"[!] Медиа не найдено: {full}")
        elif is_voice:
            kind = "voice_untranscribed"

        if not text and kind == "text":
            skipped += 1
            continue

        ts = None
        if m.get("date_unixtime"):
            ts = int(m["date_unixtime"])
        elif m.get("date"):
            try:
                ts = int(datetime.fromisoformat(m["date"]).replace(
                    tzinfo=timezone.utc).timestamp())
            except Exception:
                pass

        store.add_post(
            post_id=mid,
            chat_id=data.get("id"),
            date=ts,
            text=text,
            kind=kind,
            source="export",
        )
        added += 1

    print(f"\n[+] Добавлено: {added}")
    print(f"[+] Транскрибировано голосовых: {voice}")
    print(f"[+] Пропущено (пустые/дубли): {skipped}")
    print(f"[+] Всего в архиве: {store.count_posts()}")
    print("\nПроверь качество расшифровки перед стартом теста:")
    print("  python -c \"from store import Store;import pathlib;"
          "s=Store(pathlib.Path('data'));"
          "[print(p['iso'][:10],p['text'][:200],'\\n') "
          "for p in s.all_posts() if p['kind']=='voice'][:5]\"")


if __name__ == "__main__":
    main()
