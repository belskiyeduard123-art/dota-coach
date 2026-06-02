#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dota Coach — веб-версия (Flask).

Вся логика и КЛЮЧИ живут на сервере. Пользователь вводит только Steam ID.
Поток: браузер -> /api/analyze -> OpenDota -> метрики -> инсайты -> YandexGPT -> ответ.

Запуск локально:
    pip install flask requests
    $env:YANDEX_API_KEY="..."        (PowerShell)
    $env:YANDEX_FOLDER_ID="..."
    python app.py
    Открыть в браузере: http://127.0.0.1:5000
"""

import os
import re
import time
import json
from collections import Counter

import requests
from flask import Flask, request, jsonify, send_from_directory


def load_env_file(path=".env"):
    """
    Простой ридер .env: читает строки вида KEY=VALUE и кладёт их в окружение,
    если они там ещё не заданы. Без внешних зависимостей.
    Строки-комментарии (начинающиеся с #) и пустые игнорируются.
    Кавычки по краям значения убираются.
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # не перетираем то, что уже задано через $env:
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass


# читаем .env ДО того, как берём ключи из окружения
load_env_file()

app = Flask(__name__, static_folder="static")

# --- Конфиг (ключи только на сервере) ---
OPENDOTA_BASE = "https://api.opendota.com/api"
OPENDOTA_API_KEY = os.environ.get("OPENDOTA_API_KEY")
YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID")
YANDEX_LLM_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

MATCH_LIMIT = 20
CORE_LANE_ROLES = {1, 2}
LANE_ROLE_NAMES = {1: "safelane", 2: "mid", 3: "offlane", 4: "jungle", 0: "неизв."}

# Кэш справочника героев (id -> {name, img, icon}). Грузится один раз.
_HERO_CACHE = {}


def load_heroes():
    """Подтягивает справочник героев (имена + картинки) один раз и кэширует."""
    global _HERO_CACHE
    if _HERO_CACHE:
        return _HERO_CACHE
    try:
        stats = od_get("/heroStats")
        for h in stats:
            hid = h.get("id")
            if hid is None:
                continue
            img = h.get("img") or ""
            icon = h.get("icon") or ""
            base = "https://cdn.cloudflare.steamstatic.com"
            _HERO_CACHE[hid] = {
                "name": h.get("localized_name", f"Hero {hid}"),
                "img": (base + img) if img.startswith("/") else img,
                "icon": (base + icon) if icon.startswith("/") else icon,
            }
    except Exception:
        pass
    return _HERO_CACHE


def hero_info(hero_id):
    heroes = load_heroes()
    return heroes.get(hero_id, {"name": f"Hero {hero_id}", "img": "", "icon": ""})


# --------------------------------------------------------------------------- #
# OpenDota
# --------------------------------------------------------------------------- #

def _params(extra=None):
    p = {"api_key": OPENDOTA_API_KEY} if OPENDOTA_API_KEY else {}
    if extra:
        p.update(extra)
    return p


def od_get(path, **extra):
    resp = requests.get(f"{OPENDOTA_BASE}{path}", params=_params(extra), timeout=30)
    if not OPENDOTA_API_KEY:
        time.sleep(1.05)
    resp.raise_for_status()
    return resp.json()


def parse_account_id(raw):
    """
    Принимает разные форматы и возвращает account_id (Steam32):
      - account_id числом (123456789)
      - SteamID64 (76561198xxxxxxxxx) -> конвертирует вычитанием константы
      - ссылку на профиль Dotabuff/OpenDota
      - ссылку steamcommunity с /profiles/76561198... (SteamID64)
    Возвращает строку account_id или None.
    """
    raw = raw.strip()
    STEAM64_BASE = 76561197960265728

    # кастомная ссылка steamcommunity /id/<ник> — числового ID в ней нет
    if re.search(r"/id/[^/\s]+", raw):
        return "VANITY"  # сигнал для эндпоинта: показать подсказку

    # ссылка steamcommunity /profiles/<steamid64>
    m = re.search(r"/profiles/(\d{17})", raw)
    if m:
        return str(int(m.group(1)) - STEAM64_BASE)

    # ссылка dotabuff/opendota /players/<account_id>
    m = re.search(r"/players/(\d+)", raw)
    if m:
        return m.group(1)

    # голое число
    if raw.isdigit():
        n = int(raw)
        # SteamID64 — это большое число, начинающееся на 7656119...
        if raw.startswith("7656119") and len(raw) == 17:
            return str(n - STEAM64_BASE)
        return raw

    # число где-то в строке
    m = re.search(r"(\d{5,})", raw)
    if m:
        val = m.group(1)
        if val.startswith("7656119") and len(val) == 17:
            return str(int(val) - STEAM64_BASE)
        return val
    return None


def rank_tier_to_name(rank_tier):
    if not rank_tier:
        return "Без калибровки / скрыт"
    medals = {1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon",
              5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal"}
    medal = medals.get(rank_tier // 10, "?")
    stars = rank_tier % 10
    return f"{medal} {stars}" if stars else medal


def rank_icon_url(rank_tier):
    """
    URL картинки медали с CDN OpenDota. Формат файла rank_icon_{medal}.png,
    где medal — десятки rank_tier (1..8). Immortal (80+) — особый файл.
    """
    if not rank_tier:
        return ""
    medal = rank_tier // 10
    base = "https://www.opendota.com/assets/images/dota2/rank_icons"
    return f"{base}/rank_icon_{medal}.png"


def safe_div(a, b):
    return a / b if b else 0.0


def extract_player_row(detail, account_id):
    account_id = int(account_id)
    me = next((p for p in (detail.get("players") or [])
               if p.get("account_id") == account_id), None)
    if not me:
        return None
    duration_min = (detail.get("duration", 0) or 0) / 60
    is_radiant = me.get("player_slot", 0) < 128
    radiant_win = detail.get("radiant_win", False)
    win = (is_radiant and radiant_win) or (not is_radiant and not radiant_win)
    return {
        "match_id": detail.get("match_id"),
        "start_time": detail.get("start_time", 0),
        "hero_id": me.get("hero_id"),
        "lane_role": me.get("lane_role"),
        "lane_name": LANE_ROLE_NAMES.get(me.get("lane_role"), "неизв."),
        "win": win,
        "duration_min": round(duration_min, 1),
        "deaths": me.get("deaths", 0),
        "last_hits": me.get("last_hits", 0),
        "denies": me.get("denies", 0),
        "gpm": me.get("gold_per_min", 0),
        "xpm": me.get("xp_per_min", 0),
        "lh_per_min": round((me.get("last_hits", 0) or 0) / duration_min, 2)
        if duration_min else 0,
    }


def summarize(rows):
    if not rows:
        return None
    role_dist = Counter(r["lane_name"] for r in rows)
    core = [r for r in rows if r["lane_role"] in CORE_LANE_ROLES]

    def block(subset, label):
        if not subset:
            return {"label": label, "matches": 0}
        n = len(subset)
        hero_c = Counter(r["hero_id"] for r in subset)
        hero_w = Counter()
        for r in subset:
            if r["win"]:
                hero_w[r["hero_id"]] += 1
        return {
            "label": label, "matches": n,
            "winrate": round(safe_div(sum(r["win"] for r in subset), n) * 100, 1),
            "avg_last_hits": round(safe_div(sum(r["last_hits"] for r in subset), n), 1),
            "avg_lh_per_min": round(safe_div(sum(r["lh_per_min"] for r in subset), n), 2),
            "avg_denies": round(safe_div(sum(r["denies"] for r in subset), n), 1),
            "avg_deaths": round(safe_div(sum(r["deaths"] for r in subset), n), 1),
            "avg_gpm": round(safe_div(sum(r["gpm"] for r in subset), n)),
            "avg_xpm": round(safe_div(sum(r["xpm"] for r in subset), n)),
            "hero_pool_size": len(hero_c),
            "top_heroes": [
                {"hero_id": h, "games": c, "wins": hero_w[h],
                 "winrate": round(safe_div(hero_w[h], c) * 100),
                 "name": hero_info(h)["name"], "icon": hero_info(h)["icon"],
                 "img": hero_info(h)["img"]}
                for h, c in hero_c.most_common(5)
            ],
        }

    return {
        "total_matches": len(rows),
        "role_distribution": dict(role_dist),
        "core_block": block(core, "Кор-матчи (safelane/mid)"),
        "all_block": block(rows, "Все матчи"),
    }


def fetch_benchmarks(hero_id):
    return od_get("/benchmarks", hero_id=hero_id)


def _pct(bracket, value):
    if not bracket:
        return None
    pct = 0
    for p in sorted(bracket, key=lambda x: x.get("value", 0)):
        if value >= p.get("value", 0):
            pct = int(p.get("percentile", 0) * 100)
        else:
            break
    return pct


def benchmark_comparison(rows):
    if not rows:
        return []
    freq = Counter(r["hero_id"] for r in rows if r["hero_id"])
    out = []
    for hid, _ in freq.most_common(3):
        try:
            bm = fetch_benchmarks(hid).get("result", {})
        except Exception:
            continue
        hr = [r for r in rows if r["hero_id"] == hid]
        avg_gpm = safe_div(sum(r["gpm"] for r in hr), len(hr))
        avg_xpm = safe_div(sum(r["xpm"] for r in hr), len(hr))
        out.append({
            "hero_id": hid, "games": len(hr),
            "player_avg_gpm": round(avg_gpm),
            "gpm_percentile": _pct(bm.get("gold_per_min", []), avg_gpm),
            "player_avg_xpm": round(avg_xpm),
            "xpm_percentile": _pct(bm.get("xp_per_min", []), avg_xpm),
        })
    return out


# --------------------------------------------------------------------------- #
# Инсайты (считаются в коде)
# --------------------------------------------------------------------------- #

def find_insights(summary, benchmarks):
    insights = []
    block = summary.get("core_block") if summary.get("core_block", {}).get("matches") \
        else summary.get("all_block", {})
    if not block or not block.get("matches"):
        return ["Недостаточно данных для анализа."]

    winrate = block.get("winrate", 0)
    pool = block.get("hero_pool_size", 0)
    matches = block.get("matches", 0)
    deaths = block.get("avg_deaths", 0)

    farm_pcts = [b.get("gpm_percentile", 0) for b in benchmarks
                 if isinstance(b.get("gpm_percentile"), int)]
    avg_farm = sum(farm_pcts) / len(farm_pcts) if farm_pcts else None

    if avg_farm is not None and avg_farm >= 75 and winrate < 50:
        insights.append(
            f"ПРОТИВОРЕЧИЕ: фарм в топ-{round(avg_farm)}% (очень сильный), но winrate "
            f"всего {winrate}%. Отлично фармит, но не конвертирует это в победы.")
    elif avg_farm is not None and avg_farm < 40:
        insights.append(
            f"Фарм слабый (топ-{round(avg_farm)}%) — узкое место кора, корень проблем.")

    if matches and pool / matches > 0.7:
        insights.append(
            f"Пул распылён: {pool} героев на {matches} матчей — нет наработанной глубины.")

    winners = [h for h in block.get("top_heroes", [])
               if h.get("winrate", 0) >= 60 and h.get("games", 0) >= 2]
    if winners:
        ids = ", ".join(str(h["hero_id"]) for h in winners)
        insights.append(f"Лучшие результаты на героях (id): {ids} — кандидаты в основной пул.")

    if deaths >= 8:
        insights.append(f"Много смертей в среднем ({deaths}) — вероятна жадность.")

    if not insights:
        insights.append("Явных противоречий не найдено; показатели сбалансированы.")
    return insights


# --------------------------------------------------------------------------- #
# YandexGPT
# --------------------------------------------------------------------------- #

COACH_SYSTEM = """Ты — жёсткий, но честный тренер по Dota 2 для коров (carry/mid).
Тебе дают статистику игрока И уже найденные ключевые наблюдения (insights).

ГЛАВНОЕ ПРАВИЛО: найди самое НЕОЧЕВИДНОЕ — противоречие, парадокс. Например: фарм
в топ-перцентилях, но низкий winrate = фармит, но не конвертирует в победу. Это
ценно. А "у тебя низкий winrate, поработай над игрой" — мусор, так писать ЗАПРЕЩЕНО.

ЗАПРЕТЫ:
- НЕ советуй улучшать то, что уже в топ-перцентилях.
- НЕ пиши размытое ("проблемы могут быть связаны с разными аспектами").
- НЕ перечисляй всё подряд. Один-два точных вывода лучше пяти общих.

ФОРМАТ (ровно так, с заголовками):
ГЛАВНЫЙ ВЫВОД: одно меткое наблюдение.
ПОЧЕМУ: коротко объясни механику.
ПЛАН НА НЕДЕЛЮ: ровно 3 конкретных действия с измеримой целью.

Пиши по-русски, прямо, без воды и лести. Опирайся ТОЛЬКО на данные."""


def get_coaching(name, rank, summary, benchmarks):
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        return "[Сервер не настроен: нет ключей YandexGPT]"
    insights = find_insights(summary, benchmarks)
    insights_text = "\n".join(f"- {s}" for s in insights)
    prompt = (f"Игрок: {name}\nМедаль: {rank}\n\n"
              f"КЛЮЧЕВЫЕ НАБЛЮДЕНИЯ (опирайся на них):\n{insights_text}\n\n"
              f"Сводка:\n{json.dumps(summary, ensure_ascii=False)}\n\n"
              f"Бенчмарки:\n{json.dumps(benchmarks, ensure_ascii=False)}\n\n"
              "Сделай разбор строго по формату.")
    body = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt",
        "completionOptions": {"stream": False, "temperature": 0.3, "maxTokens": "1500"},
        "messages": [
            {"role": "system", "text": COACH_SYSTEM},
            {"role": "user", "text": prompt},
        ],
    }
    headers = {"Content-Type": "application/json",
               "Authorization": f"Api-Key {YANDEX_API_KEY}"}
    try:
        r = requests.post(YANDEX_LLM_URL, headers=headers, json=body, timeout=60)
        r.raise_for_status()
        alts = r.json().get("result", {}).get("alternatives", [])
        return alts[0].get("message", {}).get("text", "[пустой ответ]") if alts \
            else "[пустой ответ YandexGPT]"
    except Exception as e:
        return f"[Ошибка YandexGPT: {e}]"


# --------------------------------------------------------------------------- #
# Маршруты
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(force=True, silent=True) or {}
    raw = str(data.get("steam_id", ""))
    account_id = parse_account_id(raw)
    if account_id == "VANITY":
        return jsonify({"error": (
            "Это ссылка с ником (steamcommunity.com/id/...). Из неё нельзя "
            "получить ID напрямую. Открой свой профиль в Dota 2 на сайте "
            "opendota.com или dotabuff.com и вставь ссылку оттуда — либо введи "
            "числовой Steam ID. Подсказку, как его найти, ищи в профиле Steam.")}), 400
    if not account_id:
        return jsonify({"error": "Не распознал Steam ID. Введи число или ссылку на профиль."}), 400

    # 1. Профиль
    try:
        player = od_get(f"/players/{account_id}")
    except Exception as e:
        return jsonify({"error": f"Не удалось получить профиль: {e}"}), 502
    profile = player.get("profile") or {}
    name = profile.get("personaname", "Игрок")
    rank = rank_tier_to_name(player.get("rank_tier"))

    # 2. Список матчей
    try:
        match_list = od_get(f"/players/{account_id}/matches", limit=MATCH_LIMIT)
    except Exception as e:
        return jsonify({"error": f"Ошибка списка матчей: {e}"}), 502

    if not match_list:
        return jsonify({
            "error": "no_matches",
            "message": ("OpenDota не видит твоих матчей. Чаще всего причина — "
                        "выключена настройка «Открыть доступ к данным публичных "
                        "матчей» в Dota 2. Включи её, сыграй матч и попробуй снова.")
        }), 404

    # 3. Детали по каждому матчу
    rows = []
    for m in match_list:
        try:
            detail = od_get(f"/matches/{m.get('match_id')}")
            row = extract_player_row(detail, account_id)
            if row:
                rows.append(row)
        except Exception:
            continue

    if not rows:
        return jsonify({"error": "Не удалось собрать детальные данные матчей."}), 502

    summary = summarize(rows)
    core_rows = [r for r in rows if r["lane_role"] in CORE_LANE_ROLES]
    benchmarks = benchmark_comparison(core_rows if core_rows else rows)
    # обогащаем бенчмарки именами/иконками героев
    for b in benchmarks:
        info = hero_info(b.get("hero_id"))
        b["name"] = info["name"]
        b["icon"] = info["icon"]
    coaching = get_coaching(name, rank, summary, benchmarks)

    # наглядные метрики с перцентилями (для полосок на фронте)
    block = summary["core_block"] if summary["core_block"]["matches"] else summary["all_block"]
    farm_pcts = [b.get("gpm_percentile", 0) for b in benchmarks
                 if isinstance(b.get("gpm_percentile"), int)]
    avg_farm_pct = round(sum(farm_pcts) / len(farm_pcts)) if farm_pcts else None
    metrics = [
        {"label": "Винрейт", "value": f"{block['winrate']}%",
         "pct": round(block["winrate"]), "good_high": True},
        {"label": "Фарм (GPM)", "value": str(block["avg_gpm"]),
         "pct": avg_farm_pct, "good_high": True,
         "note": f"лучше {avg_farm_pct}% игроков" if avg_farm_pct is not None else None},
        {"label": "Ластхиты/мин", "value": str(block["avg_lh_per_min"]),
         "pct": None, "good_high": True},
        {"label": "Смертей/игра", "value": str(block["avg_deaths"]),
         "pct": None, "good_high": False},
    ]

    # --- График: серия побед/поражений по матчам (от старых к новым) ---
    series = [{"win": r["win"]} for r in sorted(rows, key=lambda x: x.get("start_time", 0))]

    # --- Обработка краёв ---
    notes = []
    # мало матчей — предупреждаем, что выводы статистически слабые
    if len(rows) < 8:
        notes.append({"type": "warn",
                      "text": f"Всего {len(rows)} матчей в выборке — выводы примерные. "
                              "Сыграй больше игр для точного разбора."})
    # саппорт-мейн: коров мало, но играет
    core_n = summary["core_block"]["matches"]
    roles = summary.get("role_distribution", {})
    support_like = roles.get("offlane", 0) + roles.get("jungle", 0) + roles.get("неизв.", 0)
    if core_n == 0:
        notes.append({"type": "info",
                      "text": "Похоже, ты играешь не кором (safelane/mid). Разбор сейчас "
                              "заточен под коров — для саппортов выводы по фарму менее показательны."})

    return jsonify({
        "name": name,
        "rank": rank,
        "rank_icon": rank_icon_url(player.get("rank_tier")),
        "profile_url": f"https://www.opendota.com/players/{account_id}",
        "summary": summary,
        "benchmarks": benchmarks,
        "coaching": coaching,
        "metrics": metrics,
        "top_heroes": block.get("top_heroes", []),
        "series": series,
        "notes": notes,
    })


if __name__ == "__main__":
    # порт берётся из окружения (хостинг назначает свой), локально — 5000
    port = int(os.environ.get("PORT", 5000))
    # debug только локально; на хостинге запуск идёт через gunicorn, не сюда
    app.run(host="0.0.0.0", port=port, debug=True)
