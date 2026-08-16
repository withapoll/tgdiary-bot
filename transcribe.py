#!/usr/bin/env python3
"""Транскрибация голосовых. OpenAI-совместимый /audio/transcriptions (multipart, stdlib)."""
import json
import mimetypes
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def _multipart(fields, files):
    boundary = "----tgdiary" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += f"{v}\r\n".encode()
    for k, path in files.items():
        path = Path(path)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="{k}"; '
                 f'filename="{path.name}"\r\n').encode()
        body += f"Content-Type: {ctype}\r\n\r\n".encode()
        body += path.read_bytes()
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def transcribe_file(path, cfg):
    stt = cfg.get("stt", {})
    if not stt.get("enabled", True):
        return None
    key = stt.get("api_key") or cfg.get("llm", {}).get("api_key", "")
    if not key or key.startswith("PASTE"):
        print("[!] Не задан stt.api_key — пропускаю транскрибацию.")
        return None

    url = stt.get("base_url", "https://api.openai.com/v1") + "/audio/transcriptions"
    fields = {
        "model": stt.get("model", "whisper-1"),
        "language": stt.get("language", "ru"),
        "response_format": "json",
    }
    # Подсказка модели про смешанную лексику — прямое лечение code-switching.
    prompt = stt.get("prompt")
    if prompt:
        fields["prompt"] = prompt

    data, ctype = _multipart(fields, {"file": path})
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": ctype, "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            out = json.loads(r.read().decode("utf-8"))
        return out.get("text", "").strip() or None
    except urllib.error.HTTPError as e:
        print(f"[stt] HTTP {e.code}: {e.read().decode('utf-8','ignore')[:300]}")
    except Exception as e:
        print(f"[stt] {type(e).__name__}: {e}")
    return None
