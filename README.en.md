# gemini-imagegen-browser

[Русский](README.md) | **English**

**A Claude Code skill** plus a standalone reference script: generate **images
(Nano Banana, with on-request re-creation in Nano Banana Pro), video and music**
in the **gemini.google.com** web interface through a tab of your Chrome that is
already signed in — **no API key**, using the Google AI subscription you already
pay for.

The agent (Claude Code) drives a real browser through
[browser-harness](https://github.com/browser-use/browser-harness) (CDP): it opens
a clean Gemini landing page, picks a mode from the “+” menu, inserts and verifies
the prompt, submits it, waits for the media to finish rendering, and pulls the
file past CSP (canvas `toDataURL` / CDP network response capture). Everything
runs as the account whose subscription you already pay for: Gemini's web models
(Nano Banana Pro and others) come with no API credits, so this is the only
legitimate way to automate them.

```
Claude Code ──(python script on stdin)──▶ browser-harness ──CDP──▶ your Chrome
                                                            └─ gemini.google.com tab (signed in)
```

## Capabilities

| | |
|---|---|
| **What it generates** | images (Nano Banana; on request, re-creation in Nano Banana Pro), video and music — the three modes of the “+” picker in gemini.google.com |
| **Pro quality** | an image can be re-created in Nano Banana Pro: the “More” (⋯) button under the image message → “Recreate in Pro”; the adjacent “Retry” makes a regular variant. Both layer on top of an already-created image |
| **No API key** | runs on the web subscription; no key needed and none created |
| **Batch mode** | a queue of frames: ≥90 s cadence plus jitter (protection against the daily cap), retries with a progressively tightened prompt |
| **Every step verified** | mode actually selected (chip in the shadow-DOM), prompt inserted byte-for-byte, message actually sent (composer emptied), media actually ready (`naturalWidth`/`readyState`/`duration`) — a failure comes back as a named error with diagnostics, not as an empty file |
| **Image validation** | PIL: minimum size, ~1:1 aspect, “not a flat fill”; failure → automatic retry with a stricter prompt |
| **Rate-limit handling** | when Gemini reports a limit it prints a reset date and time; the script parses them, applies the UI-vs-system clock offset (configurable, `OFFSET_H`), sleeps until reset **+2 minutes**, and **continues the queue** — it never abandons a batch halfway through |
| **Account control** | a preflight finds the active profile's e-mail in the DOM; generation only ever runs on the configured `ACCOUNT`; if needed it switches via AccountChooser to that account only (passwords are never typed) |
| **Idempotency and a registry** | finished files are never regenerated; a CSV row per frame (success and failure) records the exact prompt, sha256, status and duration — the batch is reproducible |
| **CSP workaround** | extraction via canvas `toDataURL` (images) and `fetch(blob)→base64` / CDP `Network.getResponseBody` (video/audio); the UI “Download” buttons do not fire under automation (CSP) and are not an extraction path |

## Use cases

- **Reference assets for game and app development**: item icons, tile textures,
  menu backgrounds — in bulk from one prompt template, with a registry of what
  came from where.
- **Textures and backgrounds for print and design**: marble / plaster / linen
  surfaces, print backdrops — the generous 1:1 square crops to any format, and
  1024² already covers an A6 strip at print resolution while the example also
  accepts up to 2K.
- **Product “photography” without a studio**: an item on a neutral background,
  described in words.
- **Illustrations for articles, docs and presentations** when stock doesn't fit
  and an API bill isn't worth it.
- **Short video clips and music beds** (the “Video” / “Music” modes) — same pipeline.
- **One-request automation**: in Claude Code just ask “generate N references of X
  through my Gemini” and the skill supplies the whole recipe.

## Requirements (all of them, turnkey)

1. **A Google AI subscription** (Pro/Ultra) on the account you generate with. The
   subscription grants no API credits — the whole point is the web session.
2. **Chrome (your normal profile) signed in to Google**, with a
   **`gemini.google.com` tab open** and authorized as that account. The script
   performs no logins: it reuses the existing session. If no tab exists the
   preflight opens one, but only in an already-signed-in browser; a login prompt
   means an immediate stop with a report.
3. **browser-harness**: `pip install browser-harness` (public package,
   [browser-use/browser-harness](https://github.com/browser-use/browser-harness)).
   It runs a CDP daemon attached to your Chrome; for connection problems see its
   `install.md`.
4. **Claude Code** (or any agent that reads SKILL.md) allowed to run
   `browser-harness` in a terminal. For Claude Code, drop `SKILL.md` into
   `~/.claude/skills/gemini-imagegen-browser/` and the skill becomes available to
   a request like “generate … through Gemini”.
5. **Permissions and risks, honestly**:
   - The agent controls **your real browser with your session** — trust only your
     own prompt and your own agent, and watch the screen on the first runs.
   - The script **never types passwords or codes** and never touches any account
     other than the configured one.
   - The daily generation cap is real: roughly 1.5–4 minutes per frame. On a limit
     the script waits and continues (see Capabilities), but hammering the queue
     through a cap is pointless.
   - This **automates a web interface**: Google can change the markup — the
     recipes in SKILL.md were tuned against the UI of 2026-09-14, and the “Common
     pitfalls” section helps repair them. Automating a service may violate its
     terms of service — weigh that yourself before running it on your account.

## Quick start

```bash
pip install browser-harness
```

1. Open Chrome and confirm gemini.google.com loads without a login prompt.
2. Edit `examples/gen_frames.py`:
   - `ACCOUNT = "you@gmail.com"` — the only account allowed to generate;
   - `OFFSET_H = 0` — clock-offset correction: the UI may report the reset time in
     a different time zone — check once against the first limit message and set
     your own difference (no correction by default);
   - `JOBS = [(name, prompt), …]` — the frame queue.
3. Run it:

```bash
python examples/gen_frames.py
```

`*.jpg` (1024², JPEG q90 ≤2000 px) and `gen_registry.csv` appear next to the
script. Re-running creates only what is missing and leaves finished frames alone.

### Installing the skill into Claude Code

```bash
mkdir -p ~/.claude/skills/gemini-imagegen-browser
cp SKILL.md ~/.claude/skills/gemini-imagegen-browser/SKILL.md
```

SKILL.md contains no specific e-mail: the agent asks for your account once and
keeps it in session or project memory.

## Repository layout

```
README.md / README.en.md  — this documentation (RU / EN)
SKILL.md                  — the recipe for an agent (step by step, pitfalls, limits)
examples/gen_frames.py    — standalone image batch example; ~700 lines, deps: stdlib + Pillow + numpy
LICENSE
```

> Note: the skill's internal name is `gemini-imagegen-browser` — same as the skill
> directory and the frontmatter `name`. It intentionally differs from the
> repository name; that mismatch is not a bug to fix.

## How it works inside (brief)

1. Preflight: search the DOM for the active profile's e-mail and compare it with
   `ACCOUNT`; a different account → AccountChooser → to the configured one only,
   otherwise stop.
2. Per frame: a fresh `/app` (so the image is guaranteed to be ours), polling for
   the composer, mode selection by JS-clicking the flat menu plus chip
   verification via a shadow-piercing walk, `Input.insertText` plus text
   comparison, submitting by JS-clicking the send button, polling for readiness,
   extraction, PIL validation, registry row.
3. Limits: parse the reset date/time out of the message (including “September
   15”, `dd.mm`, and roll-overs past midnight), add `OFFSET_H` hours, sleep until
   that moment plus 2 minutes, resume the queue.

## Disclaimer

This project is not affiliated with Google. Gemini is a trademark of Google LLC.
Use it on your own account and subscription, at your own risk, and within the
service's terms of use.

## License

MIT — see [LICENSE](LICENSE).
