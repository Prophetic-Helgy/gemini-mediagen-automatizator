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
import json, time, base64, os
PROMPT = {prompt_json}
TMPDIR = {tmpdir_json}
TIMEOUT = {timeout}
MARK = "###JSON###"

def fail(err, **kw):
    print(MARK + json.dumps({{"ok": False, "error": err, **kw}}, ensure_ascii=False))
    raise SystemExit(0)

tab = None
try:
    for t in list_tabs():
        if "gemini.google.com" in (t.get("url") or ""):
            switch_tab(t["targetId"]); tab = t; break
except Exception:
    pass
if tab is None:
    new_tab("https://gemini.google.com/app")
else:
    js("location.assign('https://gemini.google.com/app')")
try:
    wait_for_load()
except Exception:
    pass

COMPOSER = '.ql-editor, div[contenteditable="true"][role="textbox"], textarea'
def composer_rect():
    return js("(() => {{ const el = document.querySelector('" + COMPOSER + "');"
        "if (!el) return null; el.scrollIntoView({{block:'center'}});"
        "const r = el.getBoundingClientRect();"
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

CHIP_JS = """(() => {{ const allDeep = (r, a = []) => {{ for (const e of r.querySelectorAll('*')) {{
        a.push(e); if (e.shadowRoot) allDeep(e.shadowRoot, a); }} return a; }};
    return allDeep(document).some(e => {{ const t = (e.textContent||'').trim(); const q = e.getBoundingClientRect();
        return /^изображения$/i.test(t) && q.width > 0 && q.width < 400; }}); }})()"""

PLUS_JS = """(() => {{ const b = [...document.querySelectorAll('button')]
    .find(x => /загрузка и инструмент/i.test(x.getAttribute('aria-label')||''));
    if (!b) return null; const r = b.getBoundingClientRect();
    return {{x: r.x + r.width/2, y: r.y + r.height/2}}; }})()"""

def open_plus_js():
    return js("""(() => {{ const b = [...document.querySelectorAll('button')]
        .find(x => /загрузка и инструмент/i.test(x.getAttribute('aria-label')||''));
        if (!b) return false; b.click(); return true; }})()""")

def click_img_item_js():
    return js("""(() => {{ const allDeep = (r, a = []) => {{ for (const e of r.querySelectorAll('*')) {{
            a.push(e); if (e.shadowRoot) allDeep(e.shadowRoot, a); }} return a; }};
        const els = allDeep(document).filter(e => /^изображения$/i.test((e.textContent||'').trim()));
        if (!els.length) return null;
        const leaf = els[els.length-1];
        let t = leaf; while (t && !/button|list-item|action|mat-list-item|toolbox-item/i.test(t.tagName + ' ' + (t.className||''))) t = t.parentElement;
        (t || leaf).click();
        return (t || leaf).tagName; }})()""")

mode_ok = False
mode_via = ""
for _attempt in range(3):
    cdp("Page.bringToFront")
    time.sleep(0.8)
    for _ in range(2):
        key(27, "Escape"); time.sleep(0.3)
    # путь A (UI 2026-09-14): JS-click «+», плоское меню — клик пункта «Изображения».
    # Идёт ПЕРВЫМ: trusted-клик + стрелки на новом UI гоняет фокус по плоскому меню
    # и Enter'ом выбирает чужой пункт — состояние мусорится перед путём B (diag 2026-09-14).
    if open_plus_js():
        time.sleep(1.5)
        if click_img_item_js():
            time.sleep(1.8)
            mode_ok = bool(js(CHIP_JS))
            if mode_ok:
                mode_via = "js_flat_menu"
                break
    # сброс состояния перед запасным путём
    js("location.assign('https://gemini.google.com/app')")
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
    return js("(() => {{ const el = document.querySelector('" + COMPOSER + "');"
        "return el ? (el.value ?? el.textContent) : null; }})()") or ""

comp = composer_rect() or comp
click_at_xy(comp["x"], comp["y"])
time.sleep(0.3)
js("(() => {{ const el = document.querySelector('" + COMPOSER + "');"
   "if (!el) return; el.focus(); getSelection().selectAllChildren(el); }})()")
cdp("Input.insertText", text=PROMPT)
time.sleep(0.4)
if (editor_text() or "").strip() != PROMPT.strip():
    fail("prompt_mismatch", have=(editor_text() or "")[:200])

def send_button():
    return js("""(() => {{ const btns = [...document.querySelectorAll('button')].filter(b =>
        /отправ|send/i.test(b.getAttribute('aria-label') || '') || b.classList.contains('send-button'));
        const b = btns.find(x => !x.disabled && x.getBoundingClientRect().width > 0);
        if (!b) return null; const r = b.getBoundingClientRect();
        return {{x: r.x + r.width/2, y: r.y + r.height/2}}; }})()""")

def sent_ok():
    return not (editor_text() or "").strip()

sent = False
# путь A (UI 2026-09-14): JS .click() по «Отправить сообщение» — проверен diag
# 2026-09-14 (после него editor пустеет и открывается чат). Enter в режиме
# «Изображения» вставляет перевод строки, поэтому идёт НЕ первым.
js("""(() => {{ const btns = [...document.querySelectorAll('button')].filter(b =>
        /отправ|send/i.test(b.getAttribute('aria-label') || '') || b.classList.contains('send-button'));
    const b = btns.find(x => !x.disabled && x.getBoundingClientRect().width > 0);
    if (!b) return false; b.scrollIntoView({{block:'center'}}); b.click(); return true; }})()""")
for _ in range(4):
    time.sleep(1.5)
    if sent_ok():
        sent = True; break
if not sent:
    # путь B (запасной, старый UI): Enter в сфокусированном редакторе
    js("(() => {{ const el = document.querySelector('" + COMPOSER + "'); if (el) el.focus(); }})()")
    for t in ("keyDown", "keyUp"):
        cdp("Input.dispatchKeyEvent", type=t, key="Enter", code="Enter",
            windowsVirtualKeyCode=13, nativeVirtualKeyCode=13, text="\\r" if t == "keyDown" else "")
    for _ in range(3):
        time.sleep(1.2)
        if sent_ok():
            sent = True; break
if not sent:
    # путь C: trusted-клик по координатам кнопки
    sb = send_button()
    if sb:
        click_at_xy(sb["x"], sb["y"])
    for _ in range(4):
        time.sleep(1.5)
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

def gen_image():
    return js("""(() => {{ const imgs = [...document.querySelectorAll('img')]
        .filter(i => /\\bimage\\b/i.test(i.className||'') && i.complete && i.naturalWidth >= 512);
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
  const imgs = [...document.querySelectorAll('img')].filter(i =>
      /\\bimage\\b/i.test(i.className||'') && i.complete && i.naturalWidth >= 512);
  const img = imgs[imgs.length-1];
  if (!img) return null;
  const c = document.createElement('canvas');
  c.width = img.naturalWidth; c.height = img.naturalHeight;
  c.getContext('2d').drawImage(img, 0, 0);
  const url = c.toDataURL('image/png');
  return {{w: c.width, h: c.height, b64: url.slice(url.indexOf(',') + 1)}};
}})()""")
if not data or not data.get("b64"):
    fail("extract_failed")
os.makedirs(TMPDIR, exist_ok=True)
out = os.path.join(TMPDIR, "frame.png")
open(out, "wb").write(base64.b64decode(data["b64"]))

print(MARK + json.dumps({{"ok": True, "file": out, "w": data["w"], "h": data["h"],
                          "elapsed": round(time.time() - t0, 1), "via": "canvas",
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
import json, time
MARK = "###JSON###"
tab = None
try:
    for t in list_tabs():
        if "gemini.google.com" in (t.get("url") or ""):
            switch_tab(t["targetId"]); tab = t; break
except Exception:
    pass
if tab is None:
    new_tab("https://gemini.google.com/app")
else:
    js("location.assign('https://gemini.google.com/app')")
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
    state, emails = preflight()
    if state == "account_mismatch":
        # несколько аккаунтов уже залогинены — переключаемся строго на ACCOUNT
        log(f"активен не {ACCOUNT} (emails={emails}) — пробую переключить аккаунт")
        sw = harness_run(switch_script(), 150)
        log(f"переключение: {sw}")
        state, emails = preflight()
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
