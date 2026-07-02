# -*- coding: utf-8 -*-
"""
MEXC ST / Assessment Zone -> Telegram монитор.

Что делает при каждом запуске:
  1) Ветка API: берёт https://api.mexc.com/api/v3/exchangeInfo и смотрит,
     у каких пар стоит флаг "st": true. Новые ST-пары -> уведомление.
  2) Ветка объявлений: читает страницу https://www.mexc.com/announcements/delistings,
     ищет новые статьи про ST / Assessment Zone / делистинг,
     вытаскивает из статьи тикеры (XXX/USDT) -> уведомление.

Состояние (что уже видели) хранится в файле state.json рядом со скриптом,
поэтому одно и то же уведомление не приходит дважды.

Нужны две переменные окружения:
  TG_TOKEN   - токен телеграм-бота от @BotFather
  TG_CHAT_ID - твой chat id (число)
"""

import json
import os
import re
import sys
import time

import requests

# ----------------------------- настройки ------------------------------------

TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

EXCHANGE_INFO_URL = "https://api.mexc.com/api/v3/exchangeInfo"

# Раздел объявлений биржи, куда падают новости про ST / Assessment Zone / делистинг
ANNOUNCEMENT_PAGES = [
    "https://www.mexc.com/announcements/delistings",
    "https://www.mexc.com/announcements/delistings?page=2",
]

# Если в заголовке статьи есть хоть одно из этих слов - статья нам интересна
TITLE_KEYWORDS = [
    "assessment zone",
    "st warning",
    "st-warning",
    "delist",
    "removal",
    "suspension of trading",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_SEEN_ARTICLES = 800  # сколько старых статей помнить, чтобы файл не рос вечно


# ----------------------------- утилиты --------------------------------------

def log(msg: str) -> None:
    print(f"[mexc-monitor] {msg}", flush=True)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"Не смог прочитать state.json ({e}), начинаю с чистого листа")
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def tg_send(text: str) -> None:
    """Отправить сообщение в Telegram. Длинные сообщения режем на куски."""
    if not TG_TOKEN or not TG_CHAT_ID:
        log("TG_TOKEN / TG_CHAT_ID не заданы - сообщение не отправлено:")
        log(text)
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    # Телеграм не принимает сообщения длиннее ~4096 символов
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [text]
    for chunk in chunks:
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": TG_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            if r.status_code != 200:
                log(f"Telegram ответил {r.status_code}: {r.text[:300]}")
            time.sleep(1)
        except Exception as e:
            log(f"Ошибка отправки в Telegram: {e}")


def esc(s: str) -> str:
    """Экранируем HTML для Telegram."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------ ветка 1: MEXC API (ST флаг) ------------------------

def check_st_via_api(state: dict, first_run: bool) -> None:
    log("Ветка 1: запрашиваю exchangeInfo...")
    try:
        r = requests.get(EXCHANGE_INFO_URL, headers=HEADERS, timeout=40)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"Не смог получить exchangeInfo: {e}")
        return

    symbols = data.get("symbols", [])
    if not symbols:
        log("exchangeInfo вернул пустой список - пропускаю")
        return

    st_now = sorted({s.get("symbol", "") for s in symbols if s.get("st") is True})
    prev = set(state.get("st_symbols", []))
    new_st = [s for s in st_now if s not in prev]
    removed_st = [s for s in sorted(prev) if s not in st_now]

    log(f"ST-пар сейчас: {len(st_now)}, новых: {len(new_st)}, ушло: {len(removed_st)}")

    if first_run:
        tg_send(
            "🤖 <b>MEXC монитор запущен!</b>\n\n"
            f"Сейчас на бирже <b>{len(st_now)}</b> пар с меткой ST.\n"
            "Это первый запуск - я просто запомнил текущий список. "
            "Дальше буду присылать только <b>новые</b> попадания в ST/Assessment Zone."
        )
    else:
        if new_st:
            lines = "\n".join(
                f"• <b>{esc(s)}</b> - https://www.mexc.com/exchange/"
                f"{esc(s.replace('USDT', '_USDT').replace('USDC', '_USDC'))}"
                for s in new_st
            )
            tg_send(
                "🚨 <b>Новые токены в ST zone (данные API)</b>\n\n"
                f"{lines}\n\n"
                "⚠️ Метка ST означает риск делистинга."
            )
        if removed_st:
            lines = "\n".join(f"• {esc(s)}" for s in removed_st)
            tg_send(
                "ℹ️ <b>Пары больше не в ST-списке</b> "
                "(сняли метку или уже делистнули):\n\n" + lines
            )

    state["st_symbols"] = st_now


# ---------------- ветка 2: парсинг объявлений биржи --------------------------

ARTICLE_RE = re.compile(
    r'href="(/announcements/article/([a-z0-9\-]+?)-(\d{8,}))"[^>]*title="([^"]+)"',
    re.IGNORECASE,
)
PAIR_RE = re.compile(r"\b([A-Z0-9]{2,15})\s*/\s*(USDT|USDC|USD1|BTC|ETH)\b")


def title_is_interesting(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in TITLE_KEYWORDS)


def classify(title: str) -> str:
    t = title.lower()
    if "assessment zone" in t:
        return "🟡 ASSESSMENT ZONE"
    if "st" in t and ("warning" in t or "label" in t or "tag" in t):
        return "🔴 ST ZONE"
    if "delist" in t or "removal" in t:
        return "⛔ ДЕЛИСТИНГ"
    return "📢 ОБЪЯВЛЕНИЕ"


def extract_pairs_from_article(url: str) -> list:
    try:
        r = requests.get(url, headers=HEADERS, timeout=40)
        r.raise_for_status()
        pairs = sorted({f"{m[0]}/{m[1]}" for m in PAIR_RE.findall(r.text)})
        # выкидываем мусорные совпадения из шапки сайта
        return [p for p in pairs if p not in ("BTC/USDT", "ETH/USDT") or len(pairs) <= 3]
    except Exception as e:
        log(f"Не смог прочитать статью {url}: {e}")
        return []


def check_announcements(state: dict, first_run: bool) -> None:
    log("Ветка 2: читаю страницу объявлений...")
    seen = state.get("seen_articles", [])
    seen_set = set(seen)

    found = {}  # id -> (url, title)
    for page in ANNOUNCEMENT_PAGES:
        try:
            r = requests.get(page, headers=HEADERS, timeout=40)
            r.raise_for_status()
        except Exception as e:
            log(f"Не смог открыть {page}: {e}")
            continue
        for m in ARTICLE_RE.finditer(r.text):
            path, _slug, art_id, title = m.groups()
            found[art_id] = ("https://www.mexc.com" + path, title.strip())

    if not found:
        log("Не нашёл ни одной статьи - возможно, MEXC поменял вёрстку страницы")
        return

    log(f"Всего статей на странице: {len(found)}")

    new_items = []
    for art_id, (url, title) in found.items():
        if art_id in seen_set:
            continue
        seen.append(art_id)
        if title_is_interesting(title):
            new_items.append((art_id, url, title))

    if first_run:
        log(f"Первый запуск: запомнил {len(found)} статей без уведомлений")
    else:
        for _art_id, url, title in new_items:
            pairs = extract_pairs_from_article(url)
            pairs_text = (
                "\n\n<b>Затронутые пары:</b>\n" + "\n".join(f"• {esc(p)}" for p in pairs[:40])
                if pairs
                else ""
            )
            tg_send(
                f"{classify(title)} <b>Новое объявление MEXC</b>\n\n"
                f"<b>{esc(title)}</b>{pairs_text}\n\n"
                f"🔗 {url}"
            )

    # не даём списку "виденного" расти бесконечно
    state["seen_articles"] = seen[-MAX_SEEN_ARTICLES:]


# ----------------------------- запуск ----------------------------------------

def main() -> int:
    state = load_state()
    first_run = not state  # пустой state = самый первый запуск

    check_st_via_api(state, first_run)
    check_announcements(state, first_run)

    save_state(state)
    log("Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
