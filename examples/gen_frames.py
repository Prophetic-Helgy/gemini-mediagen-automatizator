#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пример-референс скилла gemini-imagegen-browser: батч-генерация кадров
(Nano Banana Pro) через залогиненную вкладку gemini.google.com, без API-ключа.

Конвейер одного кадра: чистый лендинг -> выбор режима «Изображения» (JS-click «+»
и клик по пункту плоского меню; фолбэк — trusted-клик + стрелки) -> промпт с
верификацией -> DOM-поллинг img -> canvas toDataURL. Аккаунт — строго ACCOUNT
(префлайт: e-mail в DOM; активен другой из уже залогиненных -> переключение через
AccountChooser строго на ACCOUNT, пароль не вводим; не вышло -> стоп).

Лимит подписки: из сообщения Gemini парсятся дата/время сброса (часы UI могут
отличаться от системных — поправка OFFSET_H, по умолчанию 0), ждём до сброса
+2 мин и продолжаем
очередь. Идемпотентность: готовые файлы не перезагенерируются. Ряд на каждый
кадр (успех и провал) — gen_registry.csv.

  python examples/gen_frames.py

Перед запуском: заполните ACCOUNT ниже; в Chrome открыта вкладка
gemini.google.com, залогиненная на этот аккаунт; установлен browser-harness.
"""

import csv
import datetime as dt
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # каталог рядом со скриптом: кадры, реестр
# --- слой вызова browser-harness (публичный пакет: pip install browser-harness) --
LIMIT_MARKERS = [
    "вы достигли лимита", "достигнут лимит", "лимита на сегодня", "возвращайтесь завтра",
    "попробуйте снова завтра", "come back tomorrow", "try again tomorrow",
    "you've reached your limit", "you have reached your limit", "reached your limit",
    "monthly reset", "превышен лимит",
]


def harness_run(frame_script: str, timeout: int) -> dict:
    """Выполнить python-скрипт внутри browser-harness, вернуть разбор ###JSON###."""
    try:
        proc = subprocess.run(["browser-harness"], input=frame_script, capture_output=True,
                              text=True, encoding="utf-8", timeout=timeout + 240)
    except FileNotFoundError:
        return {"ok": False, "error": "browser_harness_not_installed"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "harness_timeout"}
    out = proc.stdout or ""
    if "###JSON###" in out:
        try:
            return json.loads(out.split("###JSON###", 1)[1].strip().splitlines()[0])
        except (ValueError, IndexError):
            pass
    return {"ok": False, "error": "harness_no_json", "tail": (out + (proc.stderr or ""))[-500:]}


ACCOUNT = ""   # <-- ВАШ Google-аккаунт с подпиской Google AI Pro (e-mail целиком)
MODEL = "Nano Banana Pro (Gemini web)"
OFFSET_H = 0   # поправка часов: UI может показывать время сброса в поясе,
               # отличном от системного — проверьте один раз по первому сообщению
               # о лимите и поставьте разницу; по умолчанию 0 (без поправки)
REGISTRY = HERE / "gen_registry.csv"
REG_HEADER = ["kind", "name", "frame", "prompt", "model", "date_utc",
              "file", "sha256", "status", "elapsed_s", "notes"]
MIN_SIDE = 1024
MAX_SIDE = 2000     # стороны достаточно для печати A6 при 300 dpi (~1300 px)
PACE_S = 90
# род. падеж месяцев — для дат вида «15 сентября» в сообщении о лимите
MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5,
          "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10,
          "ноября": 11, "декабря": 12}

JOBS = [
    ("sample_mug", "Продуктовая фотография: керамическая матовая кружка цвета "
     "слоновой кости на светлом цементном фоне, мягкий боковой свет из окна, "
     "легкая тень, минимализм. Людей нет. Кадр без текста, букв и логотипов. "
     "Фотореалистично. Формат изображения: квадрат 1:1, разрешение не ниже "
     "1024×1024."),
    ("sample_linen", "Продуктовая фотография: аккуратно сложенная льняная "
     "скатерть натурального бежевого цвета на тёмно-сером фоне, студийный "
     "мягкий свет, видна фактура ткани. Людей нет. Кадр без текста. "
     "Фотореалистично. Формат изображения: квадрат 1:1, разрешение не ниже "
     "1024×1024."),
]
REMARKS = ["", "Важно: кадр целиком без текста, букв и людей; объект заполняет весь кадр."]

# Кадр-рецепт — один python-скрипт, исполняемый внутри browser-harness, с блоком
# выбора режима «Изображения». 2026-09-14 UI сменился: trusted-клик по «+»
# больше не открывает оверлей (открывается JS .click()), а меню стало ПЛОСКИМ
# («Изображения» — первый пункт, без «Другие варианты» ↓↓↓→). Пробуются оба пути:
# сначала новый (JS-click + клик пункта), затем запасной (trusted + клавиши).
# При лимите fail несёт окно текста сообщения — по нему парсится время сброса.
FRAME = r'''
import json, time, base64, os, re
PROMPT = {prompt_json}
TMPDIR = {tmpdir_json}
TIMEOUT = {timeout}
MARK = "###JSON###"

def fail(err, **kw):
    print(MARK + json.dumps({{"ok": False, "error": err, **kw}}, ensure_ascii=False))
    raise SystemExit(0)

tab = None
u_prefix = ""
try:
    for t in list_tabs():
        tu = t.get("url") or ""
        if "gemini.google.com" in tu:
            switch_tab(t["targetId"]); tab = t
            m = re.search(r"/u/\d+", tu)
            if m: u_prefix = m.group(0)
            break
except Exception:
    pass
# грабля 2026-09-15: навигация без /u/N откатывает на u/0 (чужой аккаунт)
GEM_APP = json.dumps("https://gemini.google.com" + u_prefix + "/app")
if tab is None:
    new_tab("https://gemini.google.com/app")
else:
    js("location.assign(" + GEM_APP + ")")   # URL литералом: URL — встроенный конструктор JS
try:
    wait_for_load()
except Exception:
    pass

COMPOSER = '.ql-editor, div[contenteditable="true"][role="textbox"], textarea'
# после чата в DOM живёт невидимый .ql-editor — брать ПОСЛЕДНИЙ ВИДИМЫЙ редактор
ED_EXPR = ("(() => {{ const eds = [...document.querySelectorAll('" + COMPOSER + "')]"
           ".filter(e => {{ const r = e.getBoundingClientRect();"
           "return r.width > 0 && r.height > 0; }});"
           "return eds.length ? eds[eds.length-1] : null; }})()")
def composer_rect():
    return js("(() => {{ const el = " + ED_EXPR + "; if (!el) return null;"
        "el.scrollIntoView({{block:'center'}}); const r = el.getBoundingClientRect();"
        "return {{x: r.x + r.width/2, y: r.y + Math.min(r.height/2, 30)}}; }})()")

deadline = time.time() + 90
comp = None
while time.time() < deadline:
    if "accounts.google.com" in (page_info().get("url") or ""):
        fail("login_wall")
    comp = composer_rect()
    if comp:
        break
    time.sleep(1.0)
if not comp:
    fail("composer_not_found", url=page_info().get("url"))

def key(vk, code):
    cdp("Input.dispatchKeyEvent", type="keyDown", windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, code=code, key=code)
    cdp("Input.dispatchKeyEvent", type="keyUp", windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, code=code, key=code)

ALL_DEEP = ("const allDeep = (r, a = []) => {{ for (const e of r.querySelectorAll('*')) {{ "
            "a.push(e); if (e.shadowRoot) allDeep(e.shadowRoot, a); }} return a; }};")
# названия пунктов плавают: «Создание изображений» (род. падеж) — матч ПОДСТРОКОЙ
ITEM_RX = r"/создание\s+изображений/i.test(t) || /^изображения$/i.test(t)"
# чип режима сверяем ТОЛЬКО у композера (низ экрана) — иначе ловим пункт бокового меню
CHIP_JS = ("(() => {{ " + ALL_DEEP +
    " return allDeep(document).some(e => {{ const t = (e.textContent||'').trim();"
    " const q = e.getBoundingClientRect();"
    " return (" + ITEM_RX + ") && q.width > 0 && q.width < 400 && q.top > innerHeight*0.55; }}); }})()")

# две волны: секция режимов дорисовывается асинхронно (5–20 с) — ждём появления пункта
ITEM_COUNT_JS = ("(() => {{ " + ALL_DEEP +
    " return allDeep(document).filter(e => {{ const t = (e.textContent||'').trim();"
    " return (" + ITEM_RX + ") && e.children.length === 0; }}).length; }})()")

PLUS_JS = """(() => {{ const b = [...document.querySelectorAll('button')]
    .find(x => /загрузка и инструмент/i.test(x.getAttribute('aria-label')||''));
    if (!b) return null; const r = b.getBoundingClientRect();
    return {{x: r.x + r.width/2, y: r.y + r.height/2}}; }})()"""

def open_plus_js():
    return js("""(() => {{ const b = [...document.querySelectorAll('button')]
        .find(x => /загрузка и инструмент/i.test(x.getAttribute('aria-label')||''));
        if (!b) return false; b.click(); return true; }})()""")

# хендлер пункта живёт на nearest BUTTON/role~menuitem|option (2026-09-17):
# подъём по классам стопорится на span.mdc-list-item__content; shadow — через host
CLICK_IMG_ITEM_JS = ("(() => {{ " + ALL_DEEP +
    " const els = allDeep(document).filter(e => {{ const t = (e.textContent||'').trim();"
    " return (" + ITEM_RX + ") && e.children.length === 0; }});"
    " if (!els.length) return null;"
    " const leaf = els[els.length-1];"
    " const up = e => e.parentElement || (e.getRootNode && e.getRootNode() && e.getRootNode().host) || null;"
    " let t = leaf, guard = 0;"
    " while (t && guard++ < 12 && !(t.tagName === 'BUTTON' ||"
    " /menuitem|option/i.test((t.getAttribute && t.getAttribute('role')) || ''))) t = up(t);"
    " t = t || leaf;"
    " if (t.disabled || t.getAttribute('aria-disabled') === 'true') return 'disabled';"
    " t.scrollIntoView({{block:'center'}}); t.click();"
    " return t.tagName + '|' + (t.getAttribute('role') || ''); }})()")

def mode_chip_ok():
    return bool(js(CHIP_JS))

# лендинг стартует в режиме «Изображения»: чип уже у композера — цикл выбора НЕЛЬЗЯ
# продавливать через «+»-меню (ломает отправку, грабля 2026-09-16)
mode_ok = mode_chip_ok()
mode_via = "already"
for _attempt in range(3 if not mode_ok else 0):
    cdp("Page.bringToFront")
    time.sleep(0.8)
    for _ in range(2):
        key(27, "Escape"); time.sleep(0.3)
    # путь A (UI 2026-09-14): JS-click «+», плоское меню — клик пункта «Изображения».
    # Идёт ПЕРВЫМ: trusted-клик + стрелки на новом UI гоняет фокус по плоскому меню
    # и Enter'ом выбирает чужой пункт — состояние мусорится перед путём B (diag 2026-09-14).
    if open_plus_js():
        # меню раскрывается ДВУМЯ волнами — поллим пункт до ~18 с
        for _w in range(12):
            if (js(ITEM_COUNT_JS) or 0) > 0:
                break
            time.sleep(1.5)
        clicked = js(CLICK_IMG_ITEM_JS)
        if clicked and clicked != "disabled":
            time.sleep(1.8)
            if mode_chip_ok():
                mode_ok = True
                mode_via = "js_flat_menu"
                break
    # сброс состояния перед запасным путём
    js("location.assign(" + GEM_APP + ")")
    try:
        wait_for_load()
    except Exception:
        pass
    time.sleep(2.5)
    # путь B (запасной, старый UI): trusted-клик «+», ↓↓↓ «Другие варианты», → подменю, Enter
    plus = js(PLUS_JS)
    if plus:
        click_at_xy(plus["x"], plus["y"])
        time.sleep(2.2)
        for _ in range(3):
            key(40, "ArrowDown"); time.sleep(0.4)
        key(39, "ArrowRight"); time.sleep(1.6)
        key(13, "Enter"); time.sleep(1.8)
        mode_ok = bool(js(CHIP_JS))
        if mode_ok:
            mode_via = "trusted_keys"
            break
    time.sleep(1.0)
if not mode_ok:
    dbg = os.path.join(TMPDIR, "debug_mode.png")
    try:
        shot = cdp("Page.captureScreenshot", format="png")
        open(dbg, "wb").write(base64.b64decode(shot["data"]))
    except Exception:
        dbg = ""
    fail("image_mode_not_selected", dbg=dbg)

def editor_text():
    return js("(() => {{ const el = " + ED_EXPR + ";"
        "return el ? (el.value ?? el.textContent) : null; }})()") or ""

def clear_editor():
    # JS selectAllChildren Quill не признаёт — реальные CDP Ctrl+A (modifiers=2) + Delete
    for tp in ("keyDown", "keyUp"):
        cdp("Input.dispatchKeyEvent", type=tp, key="a", code="KeyA", modifiers=2,
            windowsVirtualKeyCode=65, nativeVirtualKeyCode=65)
    for tp in ("keyDown", "keyUp"):
        cdp("Input.dispatchKeyEvent", type=tp, key="Delete", code="Delete",
            windowsVirtualKeyCode=46, nativeVirtualKeyCode=46)
    time.sleep(0.3)

sq = lambda s: re.sub(r"\s+", " ", (s or "")).strip()
# trusted-клик по лендинговому композеру иногда не даёт фокус — проба «т» с перекликом
comp = composer_rect() or comp
focused = False
for _try in range(6):
    cdp("Page.bringToFront")
    click_at_xy(comp["x"], comp["y"])
    time.sleep(0.5)
    # keyDown(без text)+insertText+keyUp: голый insertText не задаёт selection Quill —
    # Ctrl+A+Delete после него не срабатывает (2026-09-17)
    cdp("Input.dispatchKeyEvent", type="keyDown", key="т", code="KeyT",
        windowsVirtualKeyCode=84, nativeVirtualKeyCode=84)
    cdp("Input.insertText", text="т")
    cdp("Input.dispatchKeyEvent", type="keyUp", key="т", code="KeyT",
        windowsVirtualKeyCode=84, nativeVirtualKeyCode=84)
    time.sleep(0.4)
    if (editor_text() or "").strip() == "т":
        focused = True
        break
    clear_editor()
    comp = composer_rect() or comp
if not focused:
    fail("focus_no_accept")   # честный провал лучше ложного send_failed
for _clr in range(4):
    clear_editor()
    if not (editor_text() or "").strip():
        break
else:
    fail("clear_failed")      # никогда не печатать поверх остатка
cdp("Input.insertText", text=PROMPT)
time.sleep(0.5)
if sq(editor_text()) != sq(PROMPT):
    fail("prompt_mismatch", have=(editor_text() or "")[:200])
# SYNC WAIT (2026-09-18): JS-click по «Отправить» сразу после insertText (~<2 с)
# отправляет ПУСТОЕ сообщение и создаёт пустые чаты (нет user-query). Модель
# Quill/Angular догоняет DOM за ~7 с: до отправки выдерживаем паузу и ещё раз
# убеждаемся, что текст промпта всё ещё в редакторе.
for _ in range(3):
    time.sleep(3)
    if sq(editor_text()) != sq(PROMPT):
        fail("prompt_dropped_before_send", have=(editor_text() or "")[:120])

# Кнопка отправки — STRICT: infinitive в НАЧАЛЕ trimmed aria-label. Мягкий
# /отправ|send/i ловит декоев — заголовки чатов сайдбара («Отправление поезда…»)
# тоже матчатся и «отправка» уходит в пустоту. Приоритет точного
# «Отправить сообщение», ниже всех — самый нижний.
SEND_PRED = ("(b => { const al = (b.getAttribute('aria-label') || '').trim();"
             "return /^(отправить|send)/i.test(al) || b.classList.contains('send-button'); }")
def _send_expr():
    return ("(() => {{ const btns = [...document.querySelectorAll('button')]"
            ".filter({SEND_PRED}"
            ".filter(b => !b.disabled && b.getAttribute('aria-disabled') !== 'true'"
            "  && b.getBoundingClientRect().width > 0);"
            " const rank = b => ((b.getAttribute('aria-label') || '').trim() === 'Отправить сообщение') ? 2 : 1;"
            " btns.sort((a, b) => rank(b) - rank(a)"
            "   || b.getBoundingClientRect().y - a.getBoundingClientRect().y);"
            " return btns[0] || null; }})()").replace("{SEND_PRED}", SEND_PRED)

def send_button():
    return js("""(() => {{ const b = %s; if (!b) return null;
        const r = b.getBoundingClientRect();
        return {{x: r.x + r.width/2, y: r.y + r.height/2}}; }})()""" % _send_expr())

def sent_ok():
    # «редактор опустел» само по себе врёт (пустеет и ДО отправки): нужен
    # response-container или URL /app/<id> — признак принятого сообщения
    if (editor_text() or "").strip():
        return False
    resp = (js("document.querySelectorAll('response-container').length") or 0) > 0
    url = page_info().get("url") or ""
    return resp or bool(re.search(r"/app/[0-9a-f]{{8,}}", url))

def click_send():
    return js("""(() => {{ const b = %s;
    if (!b) return false; b.scrollIntoView({{block:'center'}}); b.click(); return true; }})()""" % _send_expr())

sent = False
# JS .click() по «Отправить сообщение» — принятие с задержкой 30–90 с: полл до 150 с
# с повторным кликом раз в ~12 с. Enter НЕ жаловать НИКОГДА — вставляет перевод строки.
click_send()
_ts = time.time(); _last_click = _ts
while time.time() - _ts < 150:
    time.sleep(3)
    if sent_ok():
        sent = True; break
    if time.time() - _last_click > 12:
        click_send(); _last_click = time.time()
if not sent:
    # Путь C (2026-09-18): trusted-клик по координатам «Отправить» НЕ работает —
    # он чистит композер без создания чата. Если после JS-ретраев текст всё ещё
    # в редакторе — ещё JS-клики; если композер пуст, а чата нет — это не
    # «недоклик», а пустая отправка, diagnostic вместо бесконечных кликов.
    if (editor_text() or "").strip():
        for _ in range(3):
            click_send()
            time.sleep(6)
            if sent_ok():
                sent = True; break
if not sent:
    dbg = os.path.join(TMPDIR, "debug_send.png")
    try:
        shot = cdp("Page.captureScreenshot", format="png")
        open(dbg, "wb").write(base64.b64decode(shot["data"]))
    except Exception:
        dbg = ""
    fail("send_failed", dbg=dbg, have=(editor_text() or "")[:120],
         url=page_info().get("url"))
t0 = time.time()

IMG_FILTER = ("(/image/i.test(i.className||'') || /blob:|gg-dl|googleusercontent/i.test(i.src||''))"
              " && i.complete && i.naturalWidth >= 512")

def gen_image():
    return js("""(() => {{ const imgs = [...document.querySelectorAll('img')]
        .filter(i => """ + IMG_FILTER + """);
        return imgs.length ? {{n: imgs.length}} : {{n: 0,
          text: [...document.querySelectorAll('response-container')]
                .slice(-1).map(e => (e.innerText||'').slice(0,200))[0] || ''}}; }})()""")

def limit_in_page():
    txt = js("(document.body.innerText||'')") or ""
    low = txt.lower()
    for m in {LIMIT_MARKERS_JS}:
        i = low.find(m)
        if i >= 0:
            return m, txt[max(0, i - 120):i + 320]   # окно текста: там дата/время сброса
    return None

img_found = False
while time.time() - t0 < TIMEOUT:
    time.sleep(2.5)
    lim = limit_in_page()
    if lim:
        fail("limit_reached", marker=lim[0], msg=lim[1])
    st = gen_image() or {{"n": 0}}
    if st.get("n", 0) > 0:
        img_found = True
        break
if not img_found:
    fail("timeout_or_no_image", elapsed=round(time.time() - t0, 1))

time.sleep(1.5)

data = js("""(async () => {{
  const imgs = [...document.querySelectorAll('img')].filter(i => """ + IMG_FILTER + """);
  const img = imgs[imgs.length-1];
  if (!img) return null;
  try {{
    const c = document.createElement('canvas');
    c.width = img.naturalWidth; c.height = img.naturalHeight;
    c.getContext('2d').drawImage(img, 0, 0);
    const url = c.toDataURL('image/png');
    return {{w: c.width, h: c.height, b64: url.slice(url.indexOf(',') + 1)}};
  }} catch (e) {{
    // https-картинка (lh3 gg-dl) на странице чата делает canvas tainted,
    // fetch режет CSP — вернём URL: топ-левел навигацию CSP не блокирует
    return /^https:/.test(img.src) ? {{nav: img.src}} : null;
  }}
}})()""")
if data and data.get("nav"):
    js("location.assign(" + json.dumps(data["nav"]) + ")")   # URL литералом, не переменной
    try:
        wait_for_load()
    except Exception:
        pass
    time.sleep(2.0)
    data = js("""(async () => {{ const img = document.querySelector('img');
        if (!img || !img.complete || !img.naturalWidth) return null;
        const c = document.createElement('canvas');
        c.width = img.naturalWidth; c.height = img.naturalHeight;
        c.getContext('2d').drawImage(img, 0, 0);
        const url = c.toDataURL('image/png');
        return {{w: c.width, h: c.height, b64: url.slice(url.indexOf(',') + 1), via: 'direct_nav'}}; }})()""")
if not data or not data.get("b64"):
    fail("extract_failed")
os.makedirs(TMPDIR, exist_ok=True)
out = os.path.join(TMPDIR, "frame.png")
open(out, "wb").write(base64.b64decode(data["b64"]))

print(MARK + json.dumps({{"ok": True, "file": out, "w": data["w"], "h": data["h"],
                          "elapsed": round(time.time() - t0, 1),
                          "via": data.get("via") or "canvas",
                          "mode_via": mode_via}}))
'''


def log(msg):
    print(f"[gen_frames] {msg}", flush=True)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def registry_append(row):
    if not REGISTRY.exists():
        REGISTRY.write_text(",".join(REG_HEADER) + "\n", encoding="utf-8")
    with REGISTRY.open("a", encoding="utf-8", newline="") as fh:
        csv.DictWriter(fh, fieldnames=REG_HEADER).writerow(row)


def preflight():
    """Aктивный аккаунт gemini — ACCOUNT; mismatch/login_wall — возвращаем статус."""
    res = harness_run(PREFLIGHT_SCRIPT, 150)
    if not res.get("ok"):
        return res.get("error", "?"), []
    emails = res.get("emails") or []
    if ACCOUNT in emails:
        return "ok", emails
    return "account_mismatch", emails


def switch_script():
    """AccountChooser: клик по строке строго ACCOUNT среди уже залогиненных."""
    return SWITCH_SCRIPT.replace("__ACC__", json.dumps(ACCOUNT))


def ensure_account():
    """Гарант аккаунта (2026-09-16): вкладка может МОЛЧА смениться на чужой
    аккаунт посреди батча — скан нужен ПЕРЕД КАЖДЫМ кадром, не только на старте."""
    state, emails = preflight()
    if state == "account_mismatch":
        log(f"гарант: активен не {ACCOUNT} (emails={emails[:3]}) — переключаю")
        sw = harness_run(switch_script(), 150)
        log(f"переключение: {sw}")
        state, emails = preflight()
    return state, emails


def parse_reset(msg):
    """Момент сброса лимита по системному времени из текста сообщения Gemini.

    К показанному времени прибавляется OFFSET_H (пояс UI может отличаться от
    системного; по умолчанию 0 — без поправки). Дата в сообщении может быть
    другим днём; без даты — сегодня, а если момент уже прошёл — завтра.
    """
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", msg or "")
    if not m:
        return None
    hh, mi = int(m.group(1)) % 24 + OFFSET_H, int(m.group(2))
    roll = dt.timedelta(days=hh // 24)                    # перенос через полночь
    hh %= 24
    now = dt.datetime.now()
    day = mo = yr = None
    d1 = re.search(r"\b(\d{1,2})[.](\d{1,2})(?:[.](\d{2,4}))?\b", msg)
    if d1 and 1 <= int(d1.group(1)) <= 31 and 1 <= int(d1.group(2)) <= 12:
        day, mo = int(d1.group(1)), int(d1.group(2))
        if d1.group(3):
            yr = int(d1.group(3))
            yr += 2000 if yr < 100 else 0
    else:
        d2 = re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b", msg.lower())
        if d2:
            day, mo = int(d2.group(1)), MONTHS[d2.group(2)]
    try:
        t = dt.datetime(yr or now.year, mo or now.month, day or now.day, hh, mi) + roll
    except ValueError:
        return None
    while t <= now and day is None:   # время без даты уже минуло -> завтра
        t += dt.timedelta(days=1)
    return t


def wait_until(t):
    """Спать до t + 2 мин (для верности). False, если ждать больше суток."""
    target = t + dt.timedelta(minutes=2)
    secs = (target - dt.datetime.now()).total_seconds()
    if secs > 26 * 3600:
        return False
    log(f"ждём сброс лимита: до {target:%d.%m %H:%M} по системному "
        f"времени (ждём {secs / 3600:.1f} ч) …")
    while True:
        s = (target - dt.datetime.now()).total_seconds()
        if s <= 0:
            return True
        time.sleep(min(s, 600))


PREFLIGHT_SCRIPT = r'''
import json, time, re
MARK = "###JSON###"
tab = None
u_prefix = ""
try:
    for t in list_tabs():
        tu = t.get("url") or ""
        if "gemini.google.com" in tu:
            switch_tab(t["targetId"]); tab = t
            m = re.search(r"/u/\d+", tu)
            if m: u_prefix = m.group(0)   # навигация без /u/N откатывает на u/0
            break
except Exception:
    pass
if tab is None:
    new_tab("https://gemini.google.com/app")
else:
    js("location.assign(" + json.dumps("https://gemini.google.com" + u_prefix + "/app") + ")")
try: wait_for_load()
except Exception: pass

deadline = time.time() + 90
comp = False
while time.time() < deadline:
    url = page_info().get("url") or ""
    if "accounts.google.com" in url:
        print(MARK + json.dumps({"ok": False, "error": "login_wall", "url": url}))
        raise SystemExit(0)
    comp = bool(js("""(() => { const el = document.querySelector('.ql-editor, div[contenteditable="true"][role="textbox"], textarea');
        return el ? 1 : 0; })()"""))
    if comp: break
    time.sleep(1.0)

SCAN = """(() => { const allDeep = (r, a = []) => { for (const e of r.querySelectorAll('*')) {
        a.push(e); if (e.shadowRoot) allDeep(e.shadowRoot, a); } return a; };
    const out = new Set();
    const rx = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}/g;
    for (const e of allDeep(document)) {
        for (const at of ['aria-label','title','alt','data-email']) {
            const v = e.getAttribute ? e.getAttribute(at) : null;
            if (v) { const m = v.match(rx); if (m) m.forEach(x => out.add(x)); }
        }
    }
    return [...out]; })()"""
emails = js(SCAN) or []
if not emails:
    av = js("""(() => { const allDeep = (r, a = []) => { for (const e of r.querySelectorAll('*')) {
            a.push(e); if (e.shadowRoot) allDeep(e.shadowRoot, a); } return a; };
        const b = allDeep(document).find(e => e.tagName === 'BUTTON' &&
            /аватар|account|profile|профил|аккаунт/i.test((e.getAttribute('aria-label')||'') + (e.getAttribute('title')||'')));
        if (!b) return null; const r = b.getBoundingClientRect();
        if (!r.width) return null;
        return {x: r.x + r.width/2, y: r.y + r.height/2}; })()""")
    if av:
        cdp("Page.bringToFront"); time.sleep(0.5)
        click_at_xy(av["x"], av["y"])
        time.sleep(2.0)
        emails = js(SCAN) or []
        for _ in range(2):
            cdp("Input.dispatchKeyEvent", type="keyDown", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27, code="Escape", key="Escape")
            cdp("Input.dispatchKeyEvent", type="keyUp", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27, code="Escape", key="Escape")
            time.sleep(0.2)
print(MARK + json.dumps({"ok": True, "emails": emails, "composer": comp}))
'''


SWITCH_SCRIPT = r'''
import json, time
MARK = "###JSON###"
ACC = __ACC__
tab = None
try:
    for t in list_tabs():
        if "gemini.google.com" in (t.get("url") or ""):
            switch_tab(t["targetId"]); tab = t; break
except Exception:
    pass
if tab is None:
    new_tab("https://accounts.google.com/AccountChooser?hl=ru")
else:
    js("location.assign('https://accounts.google.com/AccountChooser?hl=ru&continue="
       "https%3A%2F%2Fgemini.google.com%2Fapp')")
try: wait_for_load()
except Exception: pass

# ровно один клик по строке с нужным e-mail; чужие аккаунты не трогаем
CLICK = """(() => { const acc = "__ACC_JS__".toLowerCase();
    const allDeep = (r, a = []) => { for (const e of r.querySelectorAll('*')) {
        a.push(e); if (e.shadowRoot) allDeep(e.shadowRoot, a); } return a; };
    const hit = allDeep(document).find(e => (e.textContent || '').trim().toLowerCase() === acc);
    if (!hit) return 'no_row';
    let t = hit, row = null;
    for (let k = 0; k < 8 && t; k++, t = t.parentElement) {
        if (/^(a|button)$/.test(t.tagName.toLowerCase()) ||
            /list-item|account|chooser|option/i.test(t.tagName + ' ' + (t.className || ''))) { row = t; break; }
    }
    (row || hit).click();
    return 'clicked'; })()"""
r = None
for _ in range(24):
    r = js(CLICK.replace("__ACC_JS__", ACC))
    if r == "clicked":
        break
    if "accounts.google.com" not in (page_info().get("url") or ""):
        break                      # chooser не появился — возможно, уже нужен вход
    time.sleep(1.25)
try: wait_for_load()
except Exception: pass
for _ in range(12):
    if "gemini.google.com" in (page_info().get("url") or ""):
        break
    time.sleep(1.25)
print(MARK + json.dumps({"ok": r == "clicked", "click": r,
                         "url": page_info().get("url")}))
'''


def wait_for_reset(msg):
    """Дождаться сброса лимита (сообщение Gemini -> системное время + 2 мин)."""
    t = parse_reset(msg)
    if not t:
        log("в сообщении о лимите нет даты/времени сброса — нужен человек")
        return "unknown"
    if not wait_until(t):
        log(f"сброс {t:%d.%m %H:%M} — ждать дольше суток, останавливаюсь")
        return "too_far"
    log("лимит сброшен, продолжаем генерацию")
    return "ok"


def validate(path):
    """Валидация кадра: >=MIN_SIDE, квадрат ~1:1, не однотон (std>8)."""
    from PIL import Image
    import numpy as np
    problems = []
    try:
        im = Image.open(path).convert("RGB")
    except Exception as e:
        return [f"не читается: {e}"]
    w, h = im.size
    if min(w, h) < MIN_SIDE:
        problems.append(f"размер {w}x{h} < {MIN_SIDE}")
    if not (0.9 <= w / h <= 1.11):
        problems.append(f"аспект {w / h:.2f} далёк от 1:1")
    a = np.asarray(im.resize((256, 256))).astype(int)
    if a.std() < 8:
        problems.append(f"кадр почти однотонный (std={a.std():.1f})")
    return problems


def to_jpeg(src, dst):
    from PIL import Image
    im = Image.open(src).convert("RGB")
    w, h = im.size
    if max(w, h) > MAX_SIDE:
        s = MAX_SIDE / max(w, h)
        im = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
    im.save(dst, "JPEG", quality=90, optimize=True)


def gen_one(name, prompt_base, attempt_note, last_done):
    target = HERE / f"{name}.jpg"
    prompt = prompt_base + (" " + attempt_note if attempt_note else "")
    # гарант перед КАЖДЫМ кадром: после смены аккаунта генерация ушла бы на чужой
    state, emails = ensure_account()
    if state != "ok":
        registry_append({"kind": "image_frame", "name": name, "frame": "1",
                         "prompt": prompt, "model": MODEL,
                         "date_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                         "file": "", "sha256": "", "status": f"fail:{state}",
                         "elapsed_s": 0, "notes": str(emails)[:200]})
        return "fail", None, {"error": state, "emails": emails}
    if last_done is not None:
        need = PACE_S + random.uniform(0, PACE_S * 0.22)
        done = time.time() - last_done
        if done < need:
            log(f"пауза темпа {need - done:.0f} с …")
            time.sleep(need - done)
    log(f"{name}: кадр …")
    with tempfile.TemporaryDirectory(prefix="genfly_") as tmp:
        script = FRAME.format(
            prompt_json=json.dumps(prompt),
            tmpdir_json=json.dumps(tmp.replace("\\", "/")),
            timeout=240,
            LIMIT_MARKERS_JS=json.dumps(LIMIT_MARKERS, ensure_ascii=False))
        t0 = time.time()
        res = harness_run(script, 240)
        elapsed = round(time.time() - t0, 1)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        if not res.get("ok"):
            err = res.get("error", "?")
            dbg = res.get("dbg")
            if dbg and os.path.exists(dbg):        # tmp удаляется — сохраняем след
                import shutil
                shutil.copyfile(dbg, HERE / f"_diag_gen_{name}.png")
            registry_append({"kind": "image_frame", "name": name, "frame": "1",
                             "prompt": prompt, "model": MODEL, "date_utc": now,
                             "file": "", "sha256": "", "status": f"fail:{err}",
                             "elapsed_s": elapsed, "notes": str(res)[:200]})
            if err == "limit_reached":
                return "limit", None, res
            # flaky UI-моменты (оверлей, отправка, извлечение) — ретрай с переразметкой;
            # стендовые (login/стенд таймаут харнесса) — стоп
            if err in ("image_mode_not_selected", "prompt_mismatch", "send_failed",
                       "extract_failed", "timeout_or_no_image", "composer_not_found"):
                log(f"{name}: {err} — ретрай")
                return "retry", time.time(), res
            return "fail", None, res
        src = Path(res["file"])
        problems = validate(src)
        if problems:
            registry_append({"kind": "image_frame", "name": name, "frame": "1",
                             "prompt": prompt, "model": MODEL, "date_utc": now,
                             "file": "", "sha256": sha256(src),
                             "status": "fail:validation", "elapsed_s": elapsed,
                             "notes": "; ".join(problems)})
            log(f"{name}: валидация — {'; '.join(problems)}")
            return "retry", time.time(), None
        to_jpeg(src, target)
        registry_append({"kind": "image_frame", "name": name, "frame": "1",
                         "prompt": prompt, "model": MODEL, "date_utc": now,
                         "file": target.name, "sha256": sha256(target),
                         "status": "ok", "elapsed_s": elapsed,
                         "notes": f"{res.get('w')}x{res.get('h')}; "
                                  f"mode_via={res.get('mode_via')}"})
        log(f"{name}: ок -> {target} ({elapsed} с)")
        return "ok", time.time(), res


def main():
    if not ACCOUNT:
        sys.exit('Заполните ACCOUNT в начале скрипта (e-mail аккаунта с подпиской).')
    state, emails = ensure_account()
    if state != "ok":
        log(f"ОСТАНОВКА: {state} (emails={emails}) — по правилам скилла генерация "
            f"не выполняется; за пользователя не логинимся.")
        return 2
    log(f"аккаунт ok: {ACCOUNT}")
    last_done = None
    exit_code = 0
    for name, base in JOBS:
        if (HERE / f"{name}.jpg").exists():
            log(f"{name} уже есть — пропуск (идемпотентность)")
            continue
        attempt = 1
        while attempt <= 3:
            status, done, res = gen_one(name, base, REMARKS[min(attempt - 1, 1)],
                                        last_done)
            if done is not None:
                last_done = done
            if status == "ok":
                break
            if status == "limit":
                wr = wait_for_reset((res or {}).get("msg") or "")
                if wr == "ok":
                    attempt += 1     # лимит не «наш» провал — кадр пробуем заново
                    continue
                log("остаток очереди не генерируем: ждём человека")
                return 2
            if status == "fail":
                log(f"{name}: стендовая ошибка — стоп по кадру (см. реестр)")
                break   # login/стендового рода — повтор тот же промпт не спасёт
            attempt += 1             # retry
        else:
            log(f"{name}: needs_human_review (3 попытки)")
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
