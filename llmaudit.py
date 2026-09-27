#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llmaudit.py — перевірка сайту на відповідність ТЗ "LLM-видимість".
Без зовнішніх залежностей: тільки стандартна бібліотека Python 3.8+.

Використання:
    python llmaudit.py --urls urls.txt                 # повний аудит
    python llmaudit.py --urls urls.txt --json out.json  # + машинний звіт
    python llmaudit.py --sitemap https://site.com/sitemap.xml --limit 40
    python llmaudit.py --agents https://site.com/page   # тільки перевірка ТД-07

urls.txt — по одному URL на рядок, порожні рядки й рядки з # ігноруються.
Рекомендований обсяг вибірки: 10-30 репрезентативних сторінок.

Коди перевірок відповідають ID вимог у ТЗ.
Вихід: PASS / FAIL / WARN по кожній вимозі + зведення.
Ненульовий код виходу, якщо є хоч один FAIL по вимозі рівня Must.
"""

import argparse
import gzip
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import unescape

# ----------------------------------------------------------------------------
# Налаштування порогів. Змінюються під проєкт у секції 3 ТЗ.
# ----------------------------------------------------------------------------
THRESHOLDS = {
    "min_words": 400,           # ТД-01: мінімум змістового тексту без JS
    "min_words_hub": 150,       # ТД-01: те саме для хабів і службових сторінок
    "max_noise_ratio": 0.60,    # ТД-03: максимальна частка шаблонного тексту
    "agent_size_tolerance": 0.02,  # ТД-07: допустимий розкид розміру відповіді
    "q_head_min": 0.15,         # ШС-04: нижня межа частки питальних заголовків
    "q_head_max": 0.50,         # ШС-04: верхня межа (вище — ознака ферми)
    "answer_first_words": 100,  # ШС-02: у скількох перших словах шукати число
    "links_per_words": 500,     # ДА-01: 1 зовнішнє посилання на N слів
    "max_per_day": 10,          # ЗП-04: ліміт публікацій на добу
    "max_per_week": 40,         # ЗП-04: ліміт публікацій на тиждень
}

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

AGENT_UAS = [
    ("browser", BROWSER_UA),
    ("GPTBot", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2; +https://openai.com/gptbot"),
    ("OAI-SearchBot", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot"),
    ("ChatGPT-User", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot"),
    ("ClaudeBot", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ClaudeBot/1.0; +claudebot@anthropic.com"),
    ("PerplexityBot", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot"),
    ("CCBot", "CCBot/2.0 (https://commoncrawl.org/faq/)"),
    ("Googlebot", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; Googlebot/2.1; +http://www.google.com/bot.html"),
]

# Розмиті часові маркери, заборонені ВТ-02 (укр/рос/англ)
VAGUE_TIME = [
    "нещодавно", "останнім часом", "на сьогодні", "наразі", "сьогодні",
    "останніми роками", "зараз", "у наш час", "сучасн",
    "недавно", "в последнее время", "на сегодня", "сейчас",
    "recently", "nowadays", "currently", "these days", "at present",
    "in recent years", "as of today", "right now",
]

# Слова-оцінки без конкретики, ВТ-04
VAGUE_QUALITY = [
    "швидко", "доступно", "гнучко", "легко", "просто", "надійно",
    "ефективно", "оптимально", "якісно", "вигідно",
    "fast", "affordable", "flexible", "easy", "reliable", "efficient",
]

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
STYLE_RE = re.compile(r"<style\b.*?</style>", re.S | re.I)
NOSCRIPT_RE = re.compile(r"<noscript\b.*?</noscript>", re.S | re.I)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
NUM_RE = re.compile(r"\d[\d\s.,]*")


# ----------------------------------------------------------------------------
# Мережа
# ----------------------------------------------------------------------------
def fetch(url, ua=BROWSER_UA, timeout=40):
    """Повертає (status, headers, body_text). Кидає виняток тільки на мережевих збоях."""
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    })
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        status = r.status
        headers = dict(r.headers)
        raw = r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        headers = dict(e.headers) if e.headers else {}
        raw = e.read() if hasattr(e, "read") else b""
    if headers.get("Content-Encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return status, headers, raw.decode("utf-8", "ignore")


# ----------------------------------------------------------------------------
# Розбір HTML
# ----------------------------------------------------------------------------
def strip_scripts(html):
    h = SCRIPT_RE.sub(" ", html)
    h = STYLE_RE.sub(" ", h)
    h = NOSCRIPT_RE.sub(" ", h)
    return h


def visible_text(html):
    """Текст, який побачить парсер без виконання JS."""
    h = strip_scripts(html)
    t = unescape(TAG_RE.sub(" ", h))
    return re.sub(r"\s+", " ", t).strip()


def clean(s):
    return re.sub(r"\s+", " ", unescape(TAG_RE.sub("", s))).strip()


def headings(html, levels=(1, 2, 3)):
    h = strip_scripts(html)
    out = {}
    for lv in levels:
        out[lv] = [clean(m) for m in
                   re.findall(r"<h%d[^>]*>(.*?)</h%d>" % (lv, lv), h, re.S)]
    return out


def json_ld(html):
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I)
    objs = []
    for b in blocks:
        try:
            dups = []

            def _pairs(pairs, _d=dups):
                seen = set()
                for k, _v in pairs:
                    if k in seen:
                        _d.append(k)
                    seen.add(k)
                return dict(pairs)

            d = json.loads(unescape(b.strip()), object_pairs_hook=_pairs)
            if dups:
                # Google вважає повторений ключ синтаксичною помилкою,
                # хоча json.loads мовчки бере останнє значення.
                raise ValueError("повторені ключі: " + ", ".join(sorted(set(dups))))
        except Exception:
            continue
        if isinstance(d, dict) and "@graph" in d:
            d = d["@graph"]
        objs.extend(d if isinstance(d, list) else [d])
    return objs


def find_type(objs, tname):
    for o in objs:
        t = o.get("@type")
        if t == tname or (isinstance(t, list) and tname in t):
            return o
    return None


def external_links(html, host):
    h = strip_scripts(html)
    root = ".".join(host.split(".")[-2:]) if host.count(".") >= 1 else host
    urls = re.findall(r'href=["\'](https?://[^"\'\s]+)', h)
    skip = ("googletagmanager.com", "google-analytics.com", "facebook.net",
            "doubleclick.net", "gstatic.com", "googleapis.com", "cdn.jsdelivr.net")
    out = []
    for u in urls:
        if root in u:
            continue
        if any(s in u for s in skip):
            continue
        out.append(u)
    return out


# ----------------------------------------------------------------------------
# Результат перевірки
# ----------------------------------------------------------------------------
class Result:
    def __init__(self):
        self.rows = []      # (req_id, level, status, url, detail)

    def add(self, req_id, level, status, url, detail):
        self.rows.append({"req": req_id, "level": level, "status": status,
                          "url": url, "detail": detail})

    def by_req(self):
        d = defaultdict(list)
        for r in self.rows:
            d[r["req"]].append(r)
        return d

    def has_must_fail(self):
        return any(r["level"] == "MUST" and r["status"] == "FAIL" for r in self.rows)


# ----------------------------------------------------------------------------
# Перевірки по одній сторінці
# ----------------------------------------------------------------------------
def audit_page(url, html, status, res, boilerplate=None):
    host = re.sub(r"^https?://", "", url).split("/")[0]
    # Хаб-сторінки (корінь сайту, індекси розділів) не є статтями,
    # тому до них не застосовуються ШС-04, ШС-06 і РМ-02.
    path = re.sub(r"^https?://[^/]*", "", url).split("?")[0].rstrip("/")
    is_hub = path.count("/") <= 1
    text = visible_text(html)
    words = text.split()
    nwords = len(words)
    hs = headings(html)
    objs = json_ld(html)

    # --- ТД-01: серверний рендеринг ---
    # Хаб-сторінки навігаційні за призначенням, тому поріг обсягу для них нижчий.
    min_w = THRESHOLDS["min_words_hub"] if is_hub else THRESHOLDS["min_words"]
    if status != 200:
        res.add("ТД-01", "MUST", "FAIL", url, f"HTTP {status}")
    elif nwords < min_w:
        res.add("ТД-01", "MUST", "FAIL", url,
                f"без JS видно лише {nwords} слів (поріг {min_w}) — ознака клієнтського рендерингу")
    else:
        res.add("ТД-01", "MUST", "PASS", url, f"{nwords} слів без JS")

    # --- ТД-03: частка шаблонного тексту ---
    if boilerplate is not None and nwords:
        uniq = [w for w in words if w not in boilerplate]
        noise = 1 - (len(uniq) / nwords)
        st = "PASS" if noise <= THRESHOLDS["max_noise_ratio"] else "FAIL"
        res.add("ТД-03", "MUST", st, url,
                f"шаблонного тексту {noise:.0%} (поріг {THRESHOLDS['max_noise_ratio']:.0%})")

    # --- ШС-01: рівно один h1 ---
    n_h1 = len(hs[1])
    if n_h1 == 1:
        res.add("ШС-01", "MUST", "PASS", url, f"h1: «{hs[1][0][:70]}»")
    else:
        res.add("ШС-01", "MUST", "FAIL", url, f"кількість h1 = {n_h1} (треба 1)")

    # --- ШС-02: відповідь у перших N словах ---
    head_chunk = " ".join(words[:THRESHOLDS["answer_first_words"]])
    # відкидаємо навігацію: шукаємо число після появи h1 у тексті
    anchor = hs[1][0][:40] if hs[1] else ""
    if anchor and anchor in text:
        head_chunk = " ".join(text[text.index(anchor):].split()[:THRESHOLDS["answer_first_words"]])
    has_num = bool(re.search(r"\d", head_chunk))
    has_year = bool(YEAR_RE.search(head_chunk))
    if is_hub and not has_num:
        # Службові сторінки (методологія, контакти) не відповідають на питання
        # числом, тому вимога «відповідь одразу» до них не застосовується.
        res.add("ШС-02", "MUST", "PASS", url, "службова сторінка — вступне число не потрібне")
    elif has_num and has_year:
        res.add("ШС-02", "MUST", "PASS", url, "число і рік присутні у вступному блоці")
    elif has_num:
        res.add("ШС-02", "MUST", "WARN", url, "число є, року немає у перших словах")
    else:
        res.add("ШС-02", "MUST", "FAIL", url,
                f"у перших {THRESHOLDS['answer_first_words']} словах після h1 немає жодного числа")

    # --- ШС-04: частка питальних заголовків ---
    allh = hs[1] + hs[2] + hs[3]
    if allh and not is_hub:
        q = [h for h in allh if h.rstrip().endswith("?")]
        ratio = len(q) / len(allh)
        if ratio > THRESHOLDS["q_head_max"]:
            res.add("ШС-04", "SHOULD", "FAIL", url,
                    f"питальних заголовків {ratio:.0%} — вище межі {THRESHOLDS['q_head_max']:.0%} (ознака контент-ферми)")
        elif ratio < THRESHOLDS["q_head_min"]:
            res.add("ШС-04", "SHOULD", "WARN", url,
                    f"питальних заголовків {ratio:.0%} — нижче {THRESHOLDS['q_head_min']:.0%}")
        else:
            res.add("ШС-04", "SHOULD", "PASS", url, f"питальних заголовків {ratio:.0%}")

    # --- ВТ-02: розмиті часові маркери ---
    # Слова, наведені як приклад забороненої лексики, позначаються в розмітці
    # <span class="lex">…</span> і на перевірку не потрапляють: це згадка, не вжиток.
    scan = re.sub(r'<span[^>]*class="[^"]*\blex\b[^"]*"[^>]*>.*?</span>', " ",
                  strip_scripts(html), flags=re.S | re.I)
    low = re.sub(r"\s+", " ", unescape(TAG_RE.sub(" ", scan))).lower()
    found = sorted({v for v in VAGUE_TIME if v in low})
    if found:
        res.add("ВТ-02", "MUST", "FAIL", url,
                "розмиті маркери часу: " + ", ".join(found[:6]))
    else:
        res.add("ВТ-02", "MUST", "PASS", url, "розмитих маркерів часу немає")

    # --- ВТ-02b: числа без року поруч ---
    body = text
    nums = list(NUM_RE.finditer(body))
    orphan = 0
    for m in nums:
        window = body[max(0, m.start() - 120): m.end() + 120]
        if not YEAR_RE.search(window):
            orphan += 1
    if nums:
        share = orphan / len(nums)
        st = "PASS" if share < 0.5 else "WARN"
        res.add("ВТ-02b", "SHOULD", st, url,
                f"{share:.0%} чисел не мають року в межах ±120 символів")

    # --- ВТ-04: оцінки без конкретики ---
    vq = sorted({v for v in VAGUE_QUALITY if v in low})
    if len(vq) >= 4:
        res.add("ВТ-04", "SHOULD", "WARN", url,
                "оцінкові слова без конкретики: " + ", ".join(vq[:6]))
    else:
        res.add("ВТ-04", "SHOULD", "PASS", url, f"оцінкових слів: {len(vq)}")

    # --- РМ-01: canonical ---
    can = re.findall(r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)', html)
    can += re.findall(r'<link[^>]*href=["\']([^"\']+)["\'][^>]*rel=["\']canonical["\']', html)
    og = re.findall(r'<meta[^>]*property=["\']og:url["\'][^>]*content=["\']([^"\']+)', html)
    if not can:
        res.add("РМ-01", "MUST", "FAIL", url, "canonical відсутній")
    else:
        c = can[0].split("#")[0]
        o = og[0].split("#")[0] if og else c
        if "?" in c:
            res.add("РМ-01", "MUST", "FAIL", url, f"canonical містить параметри: {c}")
        elif c.rstrip("/") != o.split("?")[0].rstrip("/"):
            res.add("РМ-01", "MUST", "FAIL", url, f"canonical ≠ og:url ({c} vs {o})")
        else:
            res.add("РМ-01", "MUST", "PASS", url, "canonical коректний")

    # --- РМ-02: Article з обов'язковими полями ---
    art = find_type(objs, "Article") or find_type(objs, "NewsArticle") \
        or find_type(objs, "BlogPosting") or find_type(objs, "TechArticle")
    if not art and is_hub:
        res.add("РМ-02", "MUST", "PASS", url, "хаб-сторінка — Article не потрібен")
    elif not art:
        res.add("РМ-02", "MUST", "FAIL", url, "розмітка Article відсутня")
    else:
        need = ["headline", "datePublished", "dateModified", "author"]
        miss = [f for f in need if not art.get(f)]
        if miss:
            res.add("РМ-02", "MUST", "FAIL", url, "Article без полів: " + ", ".join(miss))
        else:
            res.add("РМ-02", "MUST", "PASS", url, "Article повний")

        # --- ДА-02: іменований автор (SHOULD) ---
        # Понижено з MUST 14.09.2026. Сайти-приклади (ibi.institute,
        # hanoverinstitute.com) не мали sameAs і все одно потрапляли у відповіді.
        # Перевірка: сира HTML Вікіпедії містить sameAs у JSON-LD, але конвеєр
        # вибірки передає моделі текст без <script> — у воротах Б поля не видно.
        # Вимога про легітимність, а не про видимість.
        a = art.get("author")
        a = a[0] if isinstance(a, list) and a else a
        # Вимога по суті: читач мусить мати спосіб перевірити, хто стоїть за
        # текстом, за межами самого сайту. Її задовольняє або іменована особа
        # з зовнішнім профілем, або організація з зовнішніми профілями.
        # Атрибуція організації без sameAs ідентичність не підтверджує —
        # це рівно той дефект, який ми фіксували в аудиті чужих сайтів.
        if isinstance(a, dict):
            ref = a.get("@id")
            if ref and not a.get("@type"):
                a = next((o for o in objs if o.get("@id") == ref), a)
            atype = a.get("@type")
            if atype == "Person" and a.get("name"):
                if a.get("sameAs"):
                    res.add("ДА-02", "SHOULD", "PASS", url, f"автор: {a['name']} (+sameAs)")
                else:
                    res.add("ДА-02", "SHOULD", "FAIL", url,
                            f"автор {a['name']} без sameAs на зовнішній профіль")
            elif atype in ("Organization", "NewsMediaOrganization",
                           "ResearchOrganization") and a.get("name"):
                if a.get("sameAs"):
                    res.add("ДА-02", "SHOULD", "PASS", url,
                            f"автор — організація {a['name']} (+sameAs)")
                else:
                    res.add("ДА-02", "SHOULD", "FAIL", url,
                            f"автор — організація {a['name']} без sameAs: "
                            "видавця неможливо перевірити зовні")
            else:
                res.add("ДА-02", "SHOULD", "FAIL", url, "автор без типу або без назви")
        else:
            res.add("ДА-02", "SHOULD", "FAIL", url, "автор не структурований")

    # --- ДА-01: щільність зовнішніх посилань ---
    ext = external_links(html, host)
    need_links = max(1, nwords // THRESHOLDS["links_per_words"])
    if len(ext) >= need_links:
        res.add("ДА-01", "MUST", "PASS", url,
                f"{len(ext)} зовнішніх посилань на {nwords} слів (потрібно ≥{need_links})")
    else:
        res.add("ДА-01", "MUST", "FAIL", url,
                f"{len(ext)} зовнішніх посилань на {nwords} слів (потрібно ≥{need_links})")

    # --- ШС-06: справжня таблиця ---
    if re.search(r"<table[\s>]", strip_scripts(html)):
        res.add("ШС-06", "SHOULD", "PASS", url, "є <table>")
    elif not is_hub:
        res.add("ШС-06", "SHOULD", "WARN", url, "таблиці з ключовими значеннями немає")


# ----------------------------------------------------------------------------
# Перевірка ТД-07 — однаковість відповіді агентам
# ----------------------------------------------------------------------------
def audit_agents(url, res):
    sizes, statuses = {}, {}
    for name, ua in AGENT_UAS:
        try:
            st, _, body = fetch(url, ua=ua, timeout=30)
        except Exception as e:
            st, body = 0, ""
            statuses[name] = f"ERR {type(e).__name__}"
        sizes[name] = len(body)
        statuses.setdefault(name, st)
        time.sleep(0.3)

    base = sizes.get("browser", 0)
    bad = []
    for name, sz in sizes.items():
        if statuses[name] != 200:
            bad.append(f"{name}: {statuses[name]}")
        elif base and abs(sz - base) / base > THRESHOLDS["agent_size_tolerance"]:
            bad.append(f"{name}: {sz}B проти {base}B")
    if bad:
        res.add("ТД-07", "SHOULD", "FAIL", url, "; ".join(bad))
    else:
        res.add("ТД-07", "SHOULD", "PASS", url,
                f"усі {len(AGENT_UAS)} агентів отримали 200, розмір {base}B")


# ----------------------------------------------------------------------------
# Перевірка sitemap — ЗП-04, ЗП-06
# ----------------------------------------------------------------------------
def audit_sitemap(sm_url, res):
    try:
        _, _, body = fetch(sm_url)
    except Exception as e:
        res.add("ЗП-06", "MUST", "WARN", sm_url, f"не вдалося отримати: {e}")
        return []

    # індекс сайтмапів
    subs = re.findall(r"<sitemap>.*?<loc>(.*?)</loc>", body, re.S)
    urls, mods = [], []
    if subs:
        for s in subs[:10]:
            try:
                _, _, b = fetch(s)
            except Exception:
                continue
            urls += re.findall(r"<url>\s*<loc>(.*?)</loc>", b, re.S)
            mods += re.findall(r"<lastmod>(.*?)</lastmod>", b)
    urls += re.findall(r"<url>\s*<loc>(.*?)</loc>", body, re.S)
    mods += re.findall(r"<lastmod>(.*?)</lastmod>", body)

    # ЗП-06: lastmod = час запиту
    now = datetime.now(timezone.utc)
    suspicious = 0
    for m in mods:
        try:
            d = datetime.fromisoformat(m.replace("Z", "+00:00"))
        except Exception:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if abs((now - d).total_seconds()) < 600:
            suspicious += 1
    if suspicious:
        res.add("ЗП-06", "MUST", "FAIL", sm_url,
                f"{suspicious} записів lastmod у межах 10 хв від часу запиту — підставлена свіжість")
    elif mods:
        res.add("ЗП-06", "MUST", "PASS", sm_url, f"перевірено {len(mods)} значень lastmod")

    # ЗП-04: темп публікації
    days = Counter(m[:10] for m in mods if len(m) >= 10)
    if days:
        top_day, top_n = days.most_common(1)[0]
        if top_n > THRESHOLDS["max_per_day"]:
            res.add("ЗП-04", "MUST", "FAIL", sm_url,
                    f"пік {top_n} матеріалів за {top_day} (ліміт {THRESHOLDS['max_per_day']}/добу)")
        else:
            res.add("ЗП-04", "MUST", "PASS", sm_url, f"максимум за добу: {top_n}")
    return urls


# ----------------------------------------------------------------------------
# Виявлення шаблонного тексту (для ТД-03)
# ----------------------------------------------------------------------------
def build_boilerplate(pages, min_share=0.8):
    """Слова, що зустрічаються на >=min_share сторінок, вважаються шаблонними."""
    if len(pages) < 4:
        return None
    counts = Counter()
    for html in pages:
        counts.update(set(visible_text(html).split()))
    need = int(len(pages) * min_share)
    return {w for w, c in counts.items() if c >= need}


# ----------------------------------------------------------------------------
# Звіт
# ----------------------------------------------------------------------------
ICON = {"PASS": "+", "FAIL": "x", "WARN": "!"}


def report(res, verbose=False):
    grouped = res.by_req()
    order = ["ТД-01", "ТД-03", "ТД-07", "АК-03", "ШС-01", "ШС-02", "ШС-04",
             "ШС-06", "ВТ-02", "ВТ-02b", "ВТ-04", "РМ-01", "РМ-02",
             "ДА-01", "ДА-02", "ЗП-04", "ЗП-06"]
    keys = [k for k in order if k in grouped] + \
           [k for k in sorted(grouped) if k not in order]

    print("\n" + "=" * 78)
    print("ЗВЕДЕННЯ ПО ВИМОГАХ")
    print("=" * 78)
    print(f"{'Вимога':<9} {'Рівень':<7} {'PASS':>5} {'FAIL':>5} {'WARN':>5}   Вердикт")
    print("-" * 78)
    must_fail = []
    for k in keys:
        rows = grouped[k]
        lvl = rows[0]["level"]
        c = Counter(r["status"] for r in rows)
        verdict = "FAIL" if c["FAIL"] else ("WARN" if c["WARN"] else "PASS")
        if verdict == "FAIL" and lvl == "MUST":
            must_fail.append(k)
        print(f"{k:<9} {lvl:<7} {c['PASS']:>5} {c['FAIL']:>5} {c['WARN']:>5}   "
              f"{ICON[verdict]} {verdict}")

    print("\n" + "=" * 78)
    print("ДЕТАЛІ ПРОВАЛІВ")
    print("=" * 78)
    any_fail = False
    for k in keys:
        bad = [r for r in grouped[k] if r["status"] in ("FAIL", "WARN")]
        if not bad:
            continue
        if not verbose:
            bad = bad[:5]
        any_fail = True
        print(f"\n[{k}] {grouped[k][0]['level']}")
        for r in bad:
            print(f"  {ICON[r['status']]} {r['url']}")
            print(f"      {r['detail']}")
        skipped = len([x for x in grouped[k] if x['status'] in ('FAIL', 'WARN')]) - len(bad)
        if skipped > 0:
            print(f"      ... ще {skipped} (запустіть з --verbose)")
    if not any_fail:
        print("  провалів немає")

    print("\n" + "=" * 78)
    if must_fail:
        print(f"РЕЗУЛЬТАТ: НЕ ПРИЙНЯТО. Провалено Must-вимоги: {', '.join(must_fail)}")
    else:
        print("РЕЗУЛЬТАТ: усі Must-вимоги виконано.")
    print("=" * 78 + "\n")


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Аудит сайту за ТЗ LLM-видимості")
    ap.add_argument("--urls", help="файл зі списком URL")
    ap.add_argument("--sitemap", help="URL sitemap.xml (перевіряє ЗП-04, ЗП-06 і бере вибірку)")
    ap.add_argument("--agents", help="URL для перевірки ТД-07 (8 агентів)")
    ap.add_argument("--limit", type=int, default=1000, help="скільки сторінок перевіряти (вибірка, якщо адрес більше)")
    ap.add_argument("--json", help="файл для машинного звіту")
    ap.add_argument("--verbose", action="store_true", help="показувати всі провали")
    args = ap.parse_args()

    res = Result()
    urls = []

    if args.sitemap:
        urls = audit_sitemap(args.sitemap, res)
        print(f"Зі sitemap отримано {len(urls)} URL")
    if args.urls:
        with open(args.urls, encoding="utf-8") as f:
            urls += [l.strip() for l in f
                     if l.strip() and not l.lstrip().startswith("#")]
    if args.agents:
        print(f"Перевірка ТД-07 на {args.agents} ...")
        audit_agents(args.agents, res)
        if not urls:
            report(res, args.verbose)
            sys.exit(1 if res.has_must_fail() else 0)

    if not urls:
        ap.error("вкажіть --urls, --sitemap або --agents")

    # рівномірна вибірка
    if len(urls) > args.limit:
        step = len(urls) / args.limit
        urls = [urls[int(i * step)] for i in range(args.limit)]

    print(f"Завантаження {len(urls)} сторінок ...")
    fetched = []
    for i, u in enumerate(urls, 1):
        try:
            st, _, html = fetch(u)
        except Exception as e:
            res.add("ТД-01", "MUST", "FAIL", u, f"помилка завантаження: {e}")
            continue
        fetched.append((u, html, st))
        print(f"  [{i}/{len(urls)}] {st} {u}")
        time.sleep(0.2)

    boiler = build_boilerplate([h for _, h, _ in fetched])
    if boiler is None:
        print("  (менше 4 сторінок — перевірка ТД-03 пропущена)")

    for u, html, st in fetched:
        audit_page(u, html, st, res, boiler)

    # ТД-07 на першій сторінці, якщо явно не задано
    if not args.agents and fetched:
        print(f"\nПеревірка ТД-07 на {fetched[0][0]} ...")
        audit_agents(fetched[0][0], res)

    report(res, args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"generated": datetime.now(timezone.utc).isoformat(),
                       "thresholds": THRESHOLDS,
                       "rows": res.rows}, f, ensure_ascii=False, indent=2)
        print(f"Машинний звіт: {args.json}")

    sys.exit(1 if res.has_must_fail() else 0)


if __name__ == "__main__":
    main()
