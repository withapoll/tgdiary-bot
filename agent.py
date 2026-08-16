#!/usr/bin/env python3
"""
Агент: правила из промпт-конституции + вызов LLM.
Поддерживает OpenAI-совместимые API (OpenAI, DeepSeek, любой прокси) и GigaChat.
"""
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

# Промпт-конституция. Правила из спека 04-тест-2-недели.md, раздел 5.
CONSTITUTION = """Ты — агент-оппонент в личном дневнике пользователя. Ты НЕ ассистент и НЕ друг.

ЖЕЛЕЗНЫЕ ПРАВИЛА:
1. ПРАВО МОЛЧАТЬ ВАЖНЕЕ ПРАВА ОТВЕТИТЬ. Если нечего сказать по существу — верни ровно: SILENCE
2. НЕ ХВАЛИ. Никаких «отличная идея», «интересная мысль», «хороший вопрос». Одна похвала ерунды обесценивает всю твою критику.
3. ВОЗРАЖАЙ ТОЛЬКО С ОПОРОЙ: источник, цифра, факт или цитата из архива пользователя. «Мне кажется, это спорно» — запрещено.
4. НЕ ЛЕЗЬ В ЛИЧНОЕ. Если пост про усталость, злость, выгорание, сомнения в себе, отношения, здоровье — верни SILENCE. Всегда.
5. НЕ СОЗДАВАЙ ЗАДАЧИ И СПИСКИ ДЕЛ. Это дневник, а не бэклог.
6. НЕ ИЗОБРАЖАЙ ЧЕЛОВЕКА. Ты инструмент. Без «я понимаю тебя», без эмодзи-эмпатии.
7. КРИВОЙ ТРАНСКРИПТ — ПЕРЕСПРОСИ. UNCLEAR используется ТОЛЬКО когда текст физически нечитаем: обрывки слов, бессвязный набор, испорченная расшифровка речи. Если мысль читается, но в ней не хватает деталей или обоснования — это НЕ повод для UNCLEAR, это ровно тот случай, когда ты возражаешь по существу (недостаток обоснования и есть слабое место). Формат при нечитаемости: UNCLEAR: <что именно непонятно>
8. ЧЕСТНОЕ «НЕ ЗНАЮ». Если не хватает данных — так и скажи, не додумывай.
9. НЕ ЗАДАВАЙ УТОЧНЯЮЩИХ ВОПРОСОВ ВМЕСТО ОТВЕТА. Ты не ассистент, ждущий ТЗ. Пользователь пишет дневник, а не запрос к тебе — работай с тем, что есть. Вопрос допустим только как финал возражения («…на чём основана цифра 8%?»), но не вместо него.

ФОРМАТ: 2-6 предложений. Без вступлений и прощаний. Сразу по делу. Обычный текст, можно <b>жирный</b> и <i>курсив</i> (HTML), без markdown."""

SKILLS = {
    "oppose": """Задача: найди самое слабое место в мысли пользователя.
Что именно неверно, какой контрпример это ломает, что он не учёл.
Если мысль устойчива и возразить нечем по существу — верни SILENCE.""",

    "research": """Задача: пользователь просит разобраться в теме поста.
У тебя ЕСТЬ доступ в интернет (WebSearch, WebFetch) — воспользуйся им.
Найди конкретные факты: цифры, цены, даты, названия продуктов, ссылки на источники.
Формат ответа: 3-5 находок, каждая — факт + откуда он.
Не пересказывай общеизвестное и не рассуждай без данных. Если поиск ничего не дал — так и скажи.""",

    "remember": """Задача: пользователь помечает это как решение.
Сформулируй одной строкой, ЧТО решено, и одной строкой — при каком условии
это решение придётся пересмотреть. Больше ничего.""",

    "recall": """Задача: пользователь спрашивает, что он писал об этом раньше.
Ниже дан его архив. Найди прошлые высказывания по теме.
ГЛАВНОЕ: если он раньше утверждал ОБРАТНОЕ — покажи оба высказывания с датами
и спроси, что изменилось. Если противоречий нет, а есть развитие мысли — покажи траекторию.
Если в архиве по теме ничего нет — верни SILENCE.""",
}

# Маркеры личного/эмоционального. Посты с ними НЕ попадают в контекст,
# отправляемый в LLM: правило 4 конституции должно работать на уровне данных,
# а не только на уровне промпта.
PERSONAL_MARKERS = [
    "выгор", "устал", "депресс", "тревог", "одиноч", "бесит", "ненавиж",
    "плак", "страшно", "паник", "не хочу жить", "смысла нет", "ничего не хочу",
    "поссор", "развод", "расста", "болею", "врач", "больниц", "терапевт",
    "психолог", "таблетк", "не могу больше", "опустились руки",
]


def is_personal(text):
    """Грубый фильтр личного. Лучше лишний раз не отдать текст модели,
    чем отдать дневниковую запись про выгорание."""
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in PERSONAL_MARKERS)


# Мусор, который нельзя подмешивать в контекст: тестовые записи, пинги, односложное.
# Проверено на живом запуске: контекст «ещё тестовая запись для бота» заставлял
# модель считать весь диалог тестом и отвечать SILENCE на содержательный пост.
NOISE_MARKERS = [
    "тестовая запись", "тест бота", "проверка бота", "тест ", "тестирую",
    "проверка связи", "ping", "pong", "test post", "hello world", "раз два три",
]


def is_noise(text, min_len=25):
    """Мусорный пост: не годится как контекст для рассуждений."""
    if not text:
        return True
    t = text.strip()
    if len(t) < min_len:
        return True
    low = t.lower()
    return any(m in low for m in NOISE_MARKERS)


class Agent:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.llm = cfg.get("llm", {})
        self.provider = self.llm.get("provider", "openai")

    # ---------- публичные скиллы ----------
    def run(self, skill, post):
        if skill not in SKILLS:
            print(f"[!] Неизвестный скилл: {skill}")
            return None
        text = (post.get("text") or "").strip()
        if not text:
            return None

        # Правило 4 на уровне данных: личный пост вообще не уходит в модель.
        if is_personal(text):
            self.store.log("silence", post["post_id"], skill, "личный пост, фильтр до LLM")
            print("[.] Пост помечен как личный — не отправляю в модель.")
            return None

        if skill == "recall":
            ctx = self._archive_context(text, exclude_id=post["post_id"])
            if not ctx:
                self.store.log("silence", post["post_id"], skill, "пустой архив по теме")
                return None
            user = f"АРХИВ ПОЛЬЗОВАТЕЛЯ:\n{ctx}\n\nТЕКУЩИЙ ПОСТ ({post.get('iso','')}):\n{text}"
        else:
            recent = self._recent_context(exclude_id=post["post_id"], n=6)
            user = (
                (f"НЕДАВНИЙ КОНТЕКСТ (для понимания, не отвечай на него):\n{recent}\n\n" if recent else "")
                + f"ПОСТ ({post.get('iso','')}):\n{text}"
            )

        answer = self._complete(CONSTITUTION + "\n\n" + SKILLS[skill], user, skill=skill)

        # Ретрай без контекста: SILENCE на содержательный пост — почти всегда
        # следствие подмешанного контекста, а не отсутствия что сказать.
        # Проверено вживую: тот же пост без контекста даёт развёрнутое возражение.
        if (answer or "").strip().upper().startswith("SILENCE") \
                and skill in ("oppose", "research") \
                and not is_noise(text, min_len=40):
            print("[.] SILENCE на содержательный пост — повторяю без контекста.")
            bare = f"ПОСТ ({post.get('iso','')}):\n{text}"
            retry = self._complete(CONSTITUTION + "\n\n" + SKILLS[skill], bare, skill=skill)
            if retry and not retry.strip().upper().startswith("SILENCE"):
                answer = retry

        return self._postprocess(answer, post.get("post_id"), skill)

    def search_archive(self, query):
        hits = self.store.search(query, limit=10)
        if not hits:
            return "В архиве ничего по этому запросу."
        ctx = "\n\n".join(
            f"[{p.get('iso','')[:10]}] {(p.get('text') or '')[:600]}" for p in hits
        )
        sys = (CONSTITUTION + "\n\nЗадача: ответь на вопрос пользователя, опираясь ТОЛЬКО "
               "на его собственный архив ниже. Цитируй с датами. Если в архиве ответа нет — так и скажи.")
        out = self._complete(sys, f"АРХИВ:\n{ctx}\n\nВОПРОС: {query}")
        return self._postprocess(out, None, "search") or "В архиве ответа нет."

    def weekly_digest(self):
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        posts = [
            p for p in self.store.all_posts()
            if p.get("iso") and datetime.fromisoformat(p["iso"]) > week_ago
        ]
        if not posts:
            return "За неделю постов нет."
        ctx = "\n\n".join(f"[{p.get('iso','')[:10]}] {(p.get('text') or '')[:500]}" for p in posts)
        sys = (CONSTITUTION + """

Задача: недельный дайджест. Строго три раздела, без воды:
1. ПОВТОРЯЕТСЯ — темы, к которым пользователь возвращался больше одного раза (это сигнал, что идея не умирает).
2. ЗАВИСЛО — идеи, заявленные и не продолженные.
3. ПРОТИВОРЕЧИТ — где высказывания расходятся между собой.
Если раздел пуст — напиши «—». Не хвали, не подводи итоги, не желай удачи.""")
        out = self._complete(sys, f"ПОСТЫ ЗА НЕДЕЛЮ:\n{ctx}")
        return self._postprocess(out, None, "digest")

    # ---------- контекст ----------
    def _recent_context(self, exclude_id=None, n=6):
        parts = []
        for p in self.store.recent_posts(n + 1):
            if exclude_id and p["post_id"] == exclude_id:
                continue
            t = (p.get("text") or "").strip()
            if t and not is_personal(t) and not is_noise(t):
                parts.append(f"[{p.get('iso','')[:10]}] {t[:300]}")
        return "\n".join(parts[-n:])

    def _archive_context(self, text, exclude_id=None, limit=10):
        words = [w for w in re.findall(r"\w{4,}", text.lower())][:12]
        hits = self.store.search(" ".join(words), limit=limit) if words else []
        parts = []
        for p in hits:
            if exclude_id and p["post_id"] == exclude_id:
                continue
            t = (p.get("text") or "").strip()
            if is_personal(t) or is_noise(t):
                continue
            parts.append(f"[{p.get('iso','')[:10]}] {t[:600]}")
        return "\n\n".join(parts)

    # ---------- постобработка ----------
    def _postprocess(self, answer, post_id, skill):
        if not answer:
            return None
        a = answer.strip()

        if a.upper().startswith("SILENCE") or a.upper() == "SILENCE":
            self.store.log("silence", post_id, skill, "агент промолчал")
            return None

        if a.upper().startswith("UNCLEAR"):
            self.store.log("unclear", post_id, skill, a[:300])
            return "⚠️ " + a[len("UNCLEAR:"):].strip()

        # страховка от нарушения правила «не хвали»
        banned = ["отличная идея", "отличный вопрос", "интересная мысль",
                  "хорошая мысль", "great idea", "молодец"]
        low = a.lower()
        for b in banned:
            if b in low:
                print(f"[!] Агент нарушил правило «не хвали» ({b}) — режу ответ.")
                self.store.log("violation", post_id, skill, b)
                idx = low.find(b)
                nxt = a.find(".", idx)
                a = (a[:idx] + a[nxt + 1:]).strip() if nxt > 0 else a[:idx].strip()
                if len(a) < 40:
                    return None
        self.store.log("answer", post_id, skill, a[:500])
        return a

    # ---------- LLM ----------
    def _complete(self, system, user, skill=None):
        if self.provider == "claude_cli":
            return self._claude_cli(system, user, skill=skill)
        if self.provider == "anthropic":
            return self._anthropic(system, user)
        if self.provider == "gigachat":
            return self._gigachat(system, user)
        return self._openai_compatible(system, user)

    def _claude_cli(self, system, user, skill=None):
        """
        Вызов Claude Code CLI в print-режиме. Работает на ПОДПИСКЕ (браузерный OAuth),
        API-ключ не нужен и в проекте не хранится.

        Требуется один раз выполнить:  claude auth login
        Проверка:                      claude auth status --text

        Ключевые флаги:
          -p                       одноразовый запуск, без интерактива и диалогов
          --system-prompt-file     конституция как системный промпт (файл, не строка:
                                   иначе кириллица и кавычки ломают шелл)
          --max-turns 1            один ход, без агентского цикла — нам нужен только текст
          --tools ""               НИКАКИХ инструментов: агент не должен читать диск
          --no-session-persistence не засорять историю сессий
          --bare                   пропустить hooks/plugins/MCP/CLAUDE.md — быстрый старт
        """
        import shutil
        import subprocess
        import tempfile

        exe = self.llm.get("cli_path") or shutil.which("claude")
        if not exe:
            for cand in (r"C:\hermes\node\claude", "/c/hermes/node/claude",
                         os.path.expanduser("~/.npm-global/bin/claude")):
                if os.path.exists(cand):
                    exe = cand
                    break
        if not exe:
            print("[!] claude CLI не найден. Установи: npm install -g "
                  "--allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code")
            return None

        sys_file = None
        try:
            # ВАЖНО: файл создаём рядом с проектом и передаём АБСОЛЮТНЫЙ нативный путь.
            # Под MSYS/git-bash пути вида /tmp/x.md нативный claude.exe видит как C:\tmp\x.md
            # и падает с "System prompt file not found".
            proj = os.path.dirname(os.path.abspath(__file__))
            fd, sys_file = tempfile.mkstemp(suffix=".md", prefix=".sysprompt_", dir=proj)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(system)
            sys_file = os.path.abspath(sys_file)

            # research получает веб-инструменты и несколько ходов на поиск.
            # Остальные скиллы работают без инструментов: они рассуждают,
            # а не лазают по диску и сети.
            if skill == "research":
                tools = self.llm.get("research_tools", "WebSearch,WebFetch")
                turns = str(self.llm.get("research_turns", 12))
            else:
                tools = ""
                turns = "1"

            cmd = [
                exe, "-p",
                "--system-prompt-file", sys_file,
                "--max-turns", turns,
                "--tools", tools,
                "--no-session-persistence",
                "--output-format", "json",
            ]
            # Без явного разрешения WebSearch/WebFetch упираются в permission-запрос,
            # который в print-режиме некому подтвердить -> "инструмент заблокирован".
            if skill == "research":
                cmd += ["--allowedTools", tools]
            model = self.llm.get("model")
            if model:
                cmd += ["--model", model]
            if self.llm.get("effort"):
                cmd += ["--effort", self.llm["effort"]]

            # Текст поста передаём через STDIN, а не аргументом:
            # многострочный аргумент обрезается на первом переносе строки.
            timeout = self.llm.get("research_timeout", 600) if skill == "research" \
                else self.llm.get("timeout", 180)
            proc = subprocess.run(
                cmd, input=user, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                print(f"[llm:claude_cli] exit {proc.returncode}: {err[:400]}")
                if "auth" in err.lower() or "login" in err.lower():
                    print("    Выполни:  claude auth login")
                return None

            out = (proc.stdout or "").strip()
            if not out:
                return None
            try:
                data = json.loads(out)
                if data.get("subtype") not in (None, "success"):
                    print(f"[llm:claude_cli] subtype={data.get('subtype')}")
                cost = data.get("total_cost_usd")
                if cost is not None:
                    print(f"[llm] turns={data.get('num_turns')} cost=${cost}")
                return (data.get("result") or "").strip() or None
            except json.JSONDecodeError:
                return out  # на случай текстового формата
        except subprocess.TimeoutExpired:
            print("[llm:claude_cli] таймаут")
        except Exception as e:
            print(f"[llm:claude_cli] {type(e).__name__}: {e}")
        finally:
            if sys_file:
                try:
                    os.unlink(sys_file)
                except OSError:
                    pass
        return None

    def _anthropic(self, system, user):
        """
        Anthropic Messages API.
        Отличия от OpenAI, из-за которых нужен отдельный метод:
          - авторизация через x-api-key, а не Bearer
          - обязательный заголовок anthropic-version
          - system идёт ОТДЕЛЬНЫМ полем, а не сообщением в messages
          - max_tokens обязателен
          - ответ лежит в content[0].text, а не choices[0].message.content
        """
        url = self.llm.get("base_url", "https://api.anthropic.com/v1") + "/messages"
        key = self.llm.get("api_key", "")
        if not key or key.startswith("PASTE"):
            print("[!] Не задан llm.api_key (ключ Anthropic из console.anthropic.com)")
            return None

        system_block = system
        # Кэширование системного промпта: конституция не меняется между вызовами,
        # платить за неё каждый раз незачем.
        if self.llm.get("cache_system", True):
            system_block = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]

        body = {
            "model": self.llm.get("model", "claude-sonnet-4-5"),
            "max_tokens": self.llm.get("max_tokens", 700),
            "temperature": self.llm.get("temperature", 0.6),
            "system": system_block,
            "messages": [{"role": "user", "content": user}],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": key,
                "anthropic-version": self.llm.get("api_version", "2023-06-01"),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.loads(r.read().decode("utf-8"))
            parts = [b.get("text", "") for b in out.get("content", []) if b.get("type") == "text"]
            usage = out.get("usage", {})
            if usage:
                print(f"[llm] in={usage.get('input_tokens')} "
                      f"cache_read={usage.get('cache_read_input_tokens', 0)} "
                      f"out={usage.get('output_tokens')}")
            return "".join(parts).strip() or None
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode("utf-8", "ignore")
            print(f"[llm:anthropic] HTTP {e.code}: {body_txt[:400]}")
            if e.code == 401:
                print("    Ключ неверный. Учти: подписка claude.ai НЕ даёт API-ключ,")
                print("    его нужно завести отдельно на console.anthropic.com")
        except Exception as e:
            print(f"[llm:anthropic] {type(e).__name__}: {e}")
        return None

    def _openai_compatible(self, system, user):
        url = self.llm.get("base_url", "https://api.openai.com/v1") + "/chat/completions"
        key = self.llm.get("api_key", "")
        if not key or key.startswith("PASTE"):
            print("[!] Не задан llm.api_key в config.json")
            return None
        body = {
            "model": self.llm.get("model", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.llm.get("temperature", 0.6),
            "max_tokens": self.llm.get("max_tokens", 700),
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.loads(r.read().decode("utf-8"))
            return out["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            print(f"[llm] HTTP {e.code}: {e.read().decode('utf-8','ignore')[:300]}")
        except Exception as e:
            print(f"[llm] {type(e).__name__}: {e}")
        return None

    def _gigachat(self, system, user):
        """GigaChat: сначала OAuth-токен по Basic, потом запрос."""
        auth = self.llm.get("auth_key", "")
        if not auth or auth.startswith("PASTE"):
            print("[!] Не задан llm.auth_key для GigaChat")
            return None
        try:
            req = urllib.request.Request(
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                data=urllib.parse_qs_encode({"scope": self.llm.get("scope", "GIGACHAT_API_PERS")}),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "RqUID": str(uuid.uuid4()),
                    "Authorization": f"Basic {auth}",
                },
            )
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                token = json.loads(r.read().decode())["access_token"]

            body = {
                "model": self.llm.get("model", "GigaChat"),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": self.llm.get("temperature", 0.6),
                "max_tokens": self.llm.get("max_tokens", 700),
            }
            req2 = urllib.request.Request(
                "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req2, timeout=120, context=ctx) as r:
                out = json.loads(r.read().decode("utf-8"))
            return out["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[gigachat] {type(e).__name__}: {e}")
            return None


# маленький помощник, чтобы не тащить requests
def _qs_encode(d):
    import urllib.parse
    return urllib.parse.urlencode(d).encode("utf-8")


urllib.parse_qs_encode = _qs_encode
