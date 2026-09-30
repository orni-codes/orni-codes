#!/usr/bin/env python3
"""
scripts/generate_projects.py — PROJECTS.LIST card generator.

Source of truth split:
  project.json       -> WHICH repos to show, display name, optional local logo path.
  GitHub REST API     -> description, star count, updated_at, and the full language
                         byte breakdown for every card (fetched live, every run).

Nothing about stars/dates/languages/percentages is ever read from project.json —
see fetch_repo() below, which is the only place that data enters the program.

Usage:
    GITHUB_TOKEN=xxxx python3 scripts/generate_projects.py
    (GITHUB_TOKEN is optional locally but strongly recommended: unauthenticated
    requests are capped at 60/hour per IP; authenticated at 5000/hour. In GitHub
    Actions this is ${{ secrets.GITHUB_TOKEN }} — no PAT needed.)

Exits non-zero with a clear stderr message on any API failure. Never substitutes
fabricated data for a failed or missing endpoint.
"""
import json
import math
import os
import sys
import base64
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
PROJECT_JSON = ROOT / "project.json"
LOGOS_DIR = ROOT / "projects" / "logos"
OUT_SVG = ROOT / "projects.svg"

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN")

# ---------------------------------------------------------------- palette
BG = "#0B0F14"
CARD_BG = "#0F1520"
CARD_BORDER = "#22D3EE"
CYAN = "#22D3EE"
PURPLE_SECONDARY = "#8B5CF6"
PURPLE = "#A78BFA"
TEXT = "#E2E8F0"
MUTED = "#64748B"
FONT = ("'JetBrains Mono','Fira Code','SF Mono',ui-monospace,Menlo,Consolas,"
         "'Liberation Mono','DejaVu Sans Mono',monospace")

# Deterministic per-language colors (GitHub-linguist-ish). Anything not listed
# falls back to a rotating slot in FALLBACK_PALETTE, chosen by first-seen order
# within that card so repeats of an unknown language still stay one color.
LANG_COLORS = {
    "JavaScript": "#F7DF1E", "TypeScript": "#3178C6", "Python": "#3776AB",
    "HTML": "#E34F26", "CSS": "#1572B6", "Java": "#ED8B00", "C++": "#00599C",
    "C": "#A8B9CC", "C#": "#178600", "Dart": "#0175C2", "Kotlin": "#7F52FF",
    "Go": "#00ADD8", "Rust": "#DEA584", "Shell": "#89E051", "PHP": "#777BB4",
    "Ruby": "#CC342D", "Vue": "#41B883", "SCSS": "#C6538C", "Swift": "#F05138",
    "Dockerfile": "#384D54", "EJS": "#A91E50",
}
FALLBACK_PALETTE = ["#22D3EE", "#A78BFA", "#10B981", "#F472B6", "#FBBF24", "#60A5FA"]


def lang_color(name, order):
    if name in LANG_COLORS:
        return LANG_COLORS[name]
    return FALLBACK_PALETTE[order.index(name) % len(FALLBACK_PALETTE)]


# ---------------------------------------------------------------- GitHub API
def gh_request(path):
    url = f"{API}{path}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "orni-codes-projects-generator",
    })
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        hint = ""
        if e.code == 403 and "rate limit" in body.lower():
            hint = " (rate limited — set GITHUB_TOKEN)"
        elif e.code == 404:
            hint = " (repo not found / renamed / private?)"
        print(f"FATAL: GitHub API {e.code} for {url}{hint}\n{body[:300]}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"FATAL: network error calling {url}: {e.reason}", file=sys.stderr)
        sys.exit(1)


def fetch_repo(full_name):
    """The ONLY function that produces description/stars/updated_at/languages.
    project.json is never consulted for any of these."""
    meta = gh_request(f"/repos/{full_name}")
    langs = gh_request(f"/repos/{full_name}/languages")
    if not langs:
        print(f"FATAL: {full_name} returned no language bytes from the API — "
              f"refusing to draw a fabricated donut for it.", file=sys.stderr)
        sys.exit(1)
    total = sum(langs.values())
    breakdown = sorted(
        ({"name": k, "bytes": v, "pct": v / total * 100} for k, v in langs.items()),
        key=lambda x: -x["pct"],
    )
    drift = abs(sum(l["pct"] for l in breakdown) - 100.0)
    if drift > 0.5:
        print(f"WARNING: {full_name} language percentages sum to "
              f"{sum(l['pct'] for l in breakdown):.2f}%, not ~100%", file=sys.stderr)
    return {
        "description": (meta.get("description") or "No repository description").strip(),
        "html_url": meta["html_url"],
        "stars": meta.get("stargazers_count", 0),
        "updated_at": meta["updated_at"],
        "languages": breakdown,
    }


def relative_date(iso_ts):
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - dt).days
    if days < 1:
        return "updated today"
    if days == 1:
        return "updated 1d ago"
    if days < 7:
        return f"updated {days}d ago"
    if days < 30:
        return f"updated {days // 7}w ago"
    if days < 365:
        return f"updated {days // 30}mo ago"
    return f"updated {days // 365}y ago"


# ---------------------------------------------------------------- SVG helpers
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))


def char_w(font_size, mono=True):
    # Empirically measured against the actual rendered font stack (headless
    # Chromium, JetBrains Mono fallback chain) at 11.5px: ~6.93px/char =>
    # ratio ~0.60-0.62. Everything in this SVG uses the same monospace stack,
    # so there is no non-mono case in practice; the small margin (0.62 vs the
    # measured 0.602) is intentional headroom against font-substitution drift.
    return font_size * (0.62 if mono else 0.56)


def wrap_text(text, max_px, font_size, max_lines=2):
    cw = char_w(font_size, mono=True)
    max_chars = max(6, int(max_px / cw))
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) > max_chars and cur:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines - 1 and cur:
                # last allowed line: fit as much as possible then ellipsize
                remaining = " ".join([cur] + words[words.index(w) + 1:])
                if len(remaining) > max_chars:
                    cur = remaining[:max_chars - 1].rstrip() + "…"
                else:
                    cur = remaining
                lines.append(cur)
                return lines
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines[:max_lines]


def monogram(cx, cy, r, letter, color):
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
            f'stroke-width="1.6" stroke-opacity=".6"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{r - 6}" fill="{color}" fill-opacity=".12"/>'
            f'<text x="{cx}" y="{cy + r * 0.34}" text-anchor="middle" '
            f'font-size="{r * 1.1:.1f}" font-weight="700" fill="{color}" '
            f'font-family="{FONT}">{esc(letter)}</text>')


def logo_or_fallback(cx, cy, r, project):
    """Try a local logo file first; fall back to a generated SVG monogram.
    Never downloads or invents an image.

    project.json's "logo" path (e.g. "logos/counselx.png") is resolved
    relative to the REPO ROOT (main/logos/counselx.png) — not relative to
    any "projects/" subfolder. The logo file lives on `main`, next to
    project.json; only the generated projects.svg output goes to the
    separate `projects` branch. Embedding as base64 (below) is what makes
    the final SVG work regardless of which branch it's viewed from — the
    source PNG on `main` is read once, at generation time, and never
    referenced by path in the output.
    """
    logo_rel = project.get("logo")
    if logo_rel:
        candidate = ROOT / logo_rel
        if candidate.is_file():
            data = base64.b64encode(candidate.read_bytes()).decode()
            ext = candidate.suffix.lstrip(".").lower()
            mime = "png" if ext == "png" else ("jpeg" if ext in ("jpg", "jpeg") else "svg+xml")
            size = r * 2
            return (f'<clipPath id="clip-{cx}-{cy}"><circle cx="{cx}" cy="{cy}" r="{r}"/></clipPath>'
                    f'<image href="data:image/{mime};base64,{data}" x="{cx - r}" y="{cy - r}" '
                    f'width="{size}" height="{size}" clip-path="url(#clip-{cx}-{cy})" '
                    f'preserveAspectRatio="xMidYMid slice"/>'
                    f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{CYAN}" stroke-opacity=".5"/>')
    letter = next((c for c in project["name"] if c.isalnum()), "?").upper()
    return monogram(cx, cy, r, letter, PURPLE)


def donut(cx, cy, r, stroke_w, languages, order):
    circumference = 2 * math.pi * r
    segs, cum = [], 0.0
    top_n = languages[:3]
    for lang in top_n:
        dash = (lang["pct"] / 100) * circumference
        offset = -(cum / 100) * circumference
        color = lang_color(lang["name"], order)
        segs.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
            f'stroke-width="{stroke_w}" stroke-dasharray="{dash:.2f} {circumference - dash:.2f}" '
            f'stroke-dashoffset="{offset:.2f}" stroke-linecap="butt" '
            f'transform="rotate(-90 {cx} {cy})"/>'
        )
        cum += lang["pct"]
    track = (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{TEXT}" '
             f'stroke-opacity=".06" stroke-width="{stroke_w}"/>')
    center_pct = round(languages[0]["pct"])
    center = (f'<text x="{cx}" y="{cy + 4}" text-anchor="middle" font-size="13" '
              f'font-weight="700" fill="{TEXT}" font-family="{FONT}">{center_pct}%</text>')
    return track + "".join(segs) + center


def legend(x, y, languages, order, line_h=14):
    out = []
    for i, lang in enumerate(languages[:3]):
        ly = y + i * line_h
        color = lang_color(lang["name"], order)
        pct = lang["pct"]
        pct_s = f"{pct:.0f}%" if pct >= 10 else f"{pct:.1f}%"
        out.append(
            f'<circle cx="{x}" cy="{ly - 4}" r="3" fill="{color}"/>'
            f'<text x="{x + 10}" y="{ly}" font-size="10.5" fill="{MUTED}" '
            f'font-family="{FONT}">{esc(lang["name"])} {pct_s}</text>'
        )
    return "".join(out)


def pills(x, y, languages, order, max_w, h=20):
    out, cx = [], x
    for lang in languages[:4]:
        label = lang["name"]
        w = len(label) * char_w(11) + 20
        if cx + w > x + max_w:
            break
        color = lang_color(label, order)
        out.append(
            f'<rect x="{cx:.1f}" y="{y}" width="{w:.1f}" height="{h}" rx="{h/2}" '
            f'fill="{color}" fill-opacity=".14" stroke="{color}" stroke-opacity=".5"/>'
            f'<text x="{cx + w/2:.1f}" y="{y + h/2 + 3.5:.1f}" text-anchor="middle" '
            f'font-size="10.5" fill="{color}" font-family="{FONT}">{esc(label)}</text>'
        )
        cx += w + 6
    return "".join(out)


# ---------------------------------------------------------------- card
CARD_W, CARD_H = 570, 176
GAP = 20
STRIP_H = 28
PAD = 16


def build_card(x, y, project, data):
    order = [l["name"] for l in data["languages"]]
    repo_path = project["repo"]
    strip = (
        f'<line x1="{x}" x2="{x + CARD_W}" y1="{y + STRIP_H}" y2="{y + STRIP_H}" '
        f'stroke="{CYAN}" stroke-opacity=".18"/>'
        f'<circle cx="{x + 9}" cy="{y + STRIP_H/2 + 1}" r="2.6" fill="{PURPLE_SECONDARY}"/>'
        f'<text x="{x + 20}" y="{y + STRIP_H/2 + 4.5}" font-size="11" fill="{MUTED}" '
        f'font-family="{FONT}">{esc(repo_path)}</text>'
        f'<circle cx="{x + CARD_W - 14}" cy="{y + STRIP_H/2 + 1}" r="3" fill="{CYAN}" fill-opacity=".5"/>'
    )

    logo_cx, logo_cy, logo_r = x + PAD + 20, y + STRIP_H + PAD + 20, 20
    logo_svg = logo_or_fallback(logo_cx, logo_cy, logo_r, project)

    text_x = logo_cx + logo_r + 12
    name_y = y + STRIP_H + PAD + 16
    name = (f'<text x="{text_x}" y="{name_y}" font-size="17" font-weight="700" '
            f'fill="{TEXT}" font-family="{FONT}">{esc(project["name"])}</text>')

    legend_w, donut_r = 96, 30
    donut_cx = x + CARD_W - PAD - donut_r
    donut_cy = y + STRIP_H + (CARD_H - STRIP_H) / 2
    legend_x = donut_cx - donut_r - 14 - legend_w

    desc_max_w = legend_x - 10 - text_x
    desc_lines = wrap_text(data["description"], desc_max_w, 11.5, max_lines=2)
    desc_svg = "".join(
        f'<text x="{text_x}" y="{name_y + 18 + i*14}" font-size="11.5" fill="{MUTED}" '
        f'font-family="{FONT}">{esc(line)}</text>'
        for i, line in enumerate(desc_lines)
    )

    tags_y = name_y + 18 + len(desc_lines) * 14 + 6
    tags_svg = pills(text_x, tags_y, data["languages"], order, desc_max_w)

    meta_y = y + CARD_H - PAD
    star_s = f'★ {data["stars"]}'
    meta_svg = (
        f'<text x="{x + PAD}" y="{meta_y}" font-size="11" fill="{PURPLE}" '
        f'font-family="{FONT}">{esc(star_s)}</text>'
        f'<text x="{x + PAD + len(star_s)*char_w(11) + 14}" y="{meta_y}" font-size="11" '
        f'fill="{MUTED}" font-family="{FONT}">{esc(relative_date(data["updated_at"]))}</text>'
    )

    donut_svg = donut(donut_cx, donut_cy, donut_r, 7, data["languages"], order)
    legend_svg = legend(legend_x, donut_cy - 14, data["languages"], order)

    card_bg = (
        f'<rect x="{x}" y="{y}" width="{CARD_W}" height="{CARD_H}" rx="12" '
        f'fill="{CARD_BG}" stroke="{CARD_BORDER}" stroke-opacity=".28"/>'
    )

    body = (card_bg + strip + logo_svg + name + desc_svg + tags_svg
            + meta_svg + donut_svg + legend_svg)

    return (
        f'<a href="{esc(data["html_url"])}" target="_blank" rel="noopener">{body}</a>'
    )


# ---------------------------------------------------------------- assemble
def build_svg(projects):
    n = len(projects)
    cols = 2
    rows = math.ceil(n / cols)
    margin = 24
    header_h = 46
    total_w = margin * 2 + cols * CARD_W + (cols - 1) * GAP
    total_h = margin + header_h + rows * CARD_H + (rows - 1) * GAP + margin

    title = "PROJECTS.LIST"
    title_w = len(title) * char_w(15) * 1.08  # *1.08 for letter-spacing="1"
    header = (
        f'<text x="{margin}" y="{margin + 18}" font-size="15" font-weight="700" '
        f'fill="{CYAN}" font-family="{FONT}" letter-spacing="1">{title}</text>'
        f'<text x="{margin + title_w + 16:.1f}" y="{margin + 18}" font-size="12" '
        f'fill="{MUTED}" font-family="{FONT}">./projects.sh --all</text>'
        f'<line x1="{margin}" x2="{total_w - margin}" y1="{margin + 30}" y2="{margin + 30}" '
        f'stroke="{CYAN}" stroke-opacity=".15"/>'
    )

    cards = []
    for i, (project, data) in enumerate(projects):
        row, col = divmod(i, cols)
        x = margin + col * (CARD_W + GAP)
        y = margin + header_h + row * (CARD_H + GAP)
        cards.append(build_card(x, y, project, data))

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{total_w}" height="{total_h}" viewBox="0 0 {total_w} {total_h}" '
        f'role="img" aria-label="Orni\'s project showcase">'
        f'<rect width="{total_w}" height="{total_h}" fill="{BG}"/>'
        f'{header}{"".join(cards)}</svg>'
    )


def main():
    projects = json.loads(PROJECT_JSON.read_text())
    resolved = []
    for p in projects:
        print(f"Fetching live data for {p['repo']} ...", file=sys.stderr)
        data = fetch_repo(p["repo"])
        resolved.append((p, data))

    svg = build_svg(resolved)
    OUT_SVG.write_text(svg)
    print(f"Wrote {OUT_SVG} ({len(svg)} bytes, {len(resolved)} cards)", file=sys.stderr)


if __name__ == "__main__":
    main()
