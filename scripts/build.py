#!/usr/bin/env python3
"""Collects a year of activity across all accessible repos and renders the profile SVG cards."""
import json
import os
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("PROFILE_TOKEN") or os.environ.get("GITHUB_TOKEN")
LOGIN = os.environ.get("PROFILE_LOGIN", "FoGTeR")
TZ = ZoneInfo(os.environ.get("PROFILE_TZ", "Europe/Kyiv"))
SKIP_OWNERS = {o.strip() for o in os.environ.get("SKIP_OWNERS", "EpicGames").split(",") if o.strip()}
# private repos whose names may appear on the card; the rest are shown as "private"
SHOW_PRIVATE = {r.strip() for r in os.environ.get(
    "SHOW_PRIVATE", "FoGTeR/friday_api,FoGTeR/AIDaniel,friyaai/website,friyaai/website_backend,friyaai/main_website"
).split(",") if r.strip()}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")

FONT = "'JetBrains Mono','Fira Code',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
BG, PANEL, GRID = "#0a0a0a", "#121212", "#2a2a2a"
TEXT, MUTED = "#f2f2f2", "#8c8c8c"
CYAN, VIOLET, PINK, LIME = "#f2f2f2", "#9a9a9a", "#e5484d", "#5a5a5a"
SHADES = ["#f2f2f2", "#c4c4c4", "#9a9a9a", "#767676", "#5a5a5a", "#444444", "#333333"]


def gql(query, **variables):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"]


REPOS_Q = """
query($after: String) {
  viewer {
    repositories(first: 100, after: $after, isFork: false,
                 ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER],
                 affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner isPrivate pushedAt owner { login }
        defaultBranchRef { name }
        languages(first: 12, orderBy: {field: SIZE, direction: DESC}) {
          totalSize edges { size node { name } }
        }
      }
    }
  }
}"""

HISTORY_Q = """
query($owner: String!, $name: String!, $since: GitTimestamp!, $author: ID!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { target { ... on Commit {
      history(first: 100, after: $after, since: $since, author: {id: $author}) {
        pageInfo { hasNextPage endCursor }
        nodes { oid authoredDate }
      }
    } } }
  }
}"""

SEARCH_Q = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { createdAt } ... on Issue { createdAt } }
  }
}"""


def paged(query, path, **variables):
    after = None
    while True:
        data = gql(query, after=after, **variables)
        node = data
        for key in path:
            node = node and node.get(key)
        if not node:
            return
        yield from node["nodes"]
        if not node["pageInfo"]["hasNextPage"]:
            return
        after = node["pageInfo"]["endCursor"]


def search_dates(q):
    return [datetime.fromisoformat(n["createdAt"].replace("Z", "+00:00"))
            for n in paged(SEARCH_Q, ["search"], q=q) if n]


def collect():
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=365)
    viewer = gql("{ viewer { id login createdAt } }")["viewer"]

    repos = [r for r in paged(REPOS_Q, ["viewer", "repositories"])
             if r["owner"]["login"] not in SKIP_OWNERS and r["defaultBranchRef"]
             and r["pushedAt"] and r["pushedAt"] >= since.isoformat()[:19]]

    seen, commits, per_repo, langs = set(), [], Counter(), defaultdict(float)
    for r in repos:
        owner, name = r["nameWithOwner"].split("/")
        n = 0
        for c in paged(HISTORY_Q, ["repository", "defaultBranchRef", "target", "history"],
                       owner=owner, name=name, since=since.isoformat(), author=viewer["id"]):
            if c["oid"] in seen:
                continue
            seen.add(c["oid"])
            commits.append(datetime.fromisoformat(c["authoredDate"].replace("Z", "+00:00")))
            n += 1
        if not n:
            continue
        per_repo[r["nameWithOwner"]] = n
        total = r["languages"]["totalSize"] or 1
        for e in r["languages"]["edges"]:
            langs[e["node"]["name"]] += n * e["size"] / total

    day = since.date().isoformat()
    prs = search_dates(f"author:{LOGIN} type:pr created:>={day}")
    issues = search_dates(f"author:{LOGIN} type:issue created:>={day}")
    reviews = search_dates(f"reviewed-by:{LOGIN} -author:{LOGIN} type:pr updated:>={day}")

    daily = Counter()
    for d in commits + prs + issues + reviews:
        daily[d.astimezone(TZ).date()] += 1

    private = {r["nameWithOwner"] for r in repos if r["isPrivate"]}
    return {
        "now": now, "since": since, "daily": daily,
        "commits": commits, "prs": len(prs), "issues": len(issues), "reviews": len(reviews),
        "per_repo": per_repo, "private": private, "langs": langs,
        "member_since": viewer["createdAt"][:4],
    }


def streaks(daily, today):
    cur = 0
    d = today if daily.get(today) else today - timedelta(days=1)
    while daily.get(d):
        cur += 1
        d -= timedelta(days=1)
    best = run = 0
    prev = None
    for d in sorted(daily):
        run = run + 1 if prev and d - prev == timedelta(days=1) else 1
        best = max(best, run)
        prev = d
    return cur, best


def svg(w, h, body, defs=""):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="{FONT}">
<defs>
  <linearGradient id="beam"><stop offset="0" stop-color="{TEXT}"/></linearGradient>
  <pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="{GRID}" stroke-width=".5" opacity=".35"/></pattern>
  {defs}
</defs>
<style>
  text {{ fill: {TEXT}; }}
  .muted {{ fill: {MUTED}; }}
  .k {{ font-size: 11px; font-weight: 700; letter-spacing: .2em; fill: {MUTED}; }}
  @keyframes fill {{ from {{ transform: scaleX(0); }} to {{ transform: scaleX(1); }} }}
  @keyframes grow {{ from {{ transform: scaleY(0); }} to {{ transform: scaleY(1); }} }}
  @keyframes fade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
  @keyframes pulse {{ 0%,100% {{ opacity: .25; }} 50% {{ opacity: 1; }} }}
  @keyframes flow {{ to {{ stroke-dashoffset: -120; }} }}
  .bar {{ transform-box: fill-box; transform-origin: left; animation: fill 1.4s cubic-bezier(.2,.8,.2,1) both; }}
  .col {{ transform-box: fill-box; transform-origin: bottom; animation: grow 1.2s cubic-bezier(.2,.8,.2,1) both; }}
  .in {{ animation: fade .8s ease both; }}
  .pulse {{ animation: pulse 2.6s ease-in-out infinite; }}
  .flow {{ stroke-dasharray: 3 117; animation: flow 2.4s linear infinite; }}
</style>
<rect width="{w}" height="{h}" fill="{BG}"/>
<rect width="{w}" height="{h}" fill="url(#grid)"/>
<rect x="1" y="1" width="{w - 2}" height="{h - 2}" fill="none" stroke="{TEXT}" stroke-width="2"/>
<rect x="0" y="0" width="10" height="{h}" fill="{TEXT}"/>
{body}
</svg>
"""


def stamp(data, w, h):
    t = data["now"].astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    return f'<text x="{w - 18}" y="{h - 12}" text-anchor="end" font-size="9" class="muted" opacity=".7">sync {t} · Kyiv</text>'


def header(data):
    w, h = 900, 230
    layers = [3, 5, 6, 5, 2]
    x0, dx = 540, 82
    nodes = []
    for i, n in enumerate(layers):
        gap = 170 / max(n - 1, 1) if n > 1 else 0
        y0 = 115 - gap * (n - 1) / 2
        nodes.append([(x0 + i * dx, y0 + j * gap) for j in range(n)])
    edges, k = [], 0
    for a, b in zip(nodes, nodes[1:]):
        for (x1, y1) in a:
            for (x2, y2) in b:
                edges.append(f'<line x1="{x1}" y1="{y1:.1f}" x2="{x2}" y2="{y2:.1f}" stroke="{GRID}" stroke-width="1"/>')
                if (k * 7) % 5 == 0:
                    edges.append(f'<line x1="{x1}" y1="{y1:.1f}" x2="{x2}" y2="{y2:.1f}" stroke="url(#beam)" stroke-width="1.6" '
                                 f'class="flow" style="animation-delay:-{(k * 0.37) % 2.4:.2f}s"/>')
                k += 1
    dots = []
    for i, layer in enumerate(nodes):
        c = PINK if i == len(nodes) - 1 else TEXT
        for j, (x, y) in enumerate(layer):
            dots.append(f'<rect x="{x - 6}" y="{y - 6:.1f}" width="12" height="12" fill="{BG}" stroke="{c}" stroke-width="1.5"/>'
                        f'<rect x="{x - 2.5}" y="{y - 2.5:.1f}" width="5" height="5" fill="{c}" class="pulse" '
                        f'style="animation-delay:{(i * 0.4 + j * 0.23) % 2.6:.2f}s"/>')
    total = sum(data["daily"].values())
    body = f"""
<g>{''.join(edges)}</g><g>{''.join(dots)}</g>
<g class="in">
  <text x="40" y="56" class="k">BACKEND / AI INFRASTRUCTURE</text>
  <text x="37" y="118" font-size="64" font-weight="800" letter-spacing="-2">FOGTER</text>
  <rect x="40" y="134" width="64" height="4" fill="{PINK}"/>
  <text x="40" y="166" font-size="13">Building Friya — AI assistant with real tools.</text>
  <text x="40" y="200" font-size="11" class="muted">{total:,} CONTRIBUTIONS / 365 DAYS — ON GITHUB SINCE {data['member_since']}</text>
</g>"""
    return svg(w, h, body)


def stats(data):
    w, h = 900, 300
    total = sum(data["daily"].values())
    cur, best = streaks(data["daily"], data["now"].astimezone(TZ).date())
    active = sum(1 for v in data["daily"].values() if v)
    kinds = [("COMMITS", len(data["commits"]), CYAN), ("PULL REQUESTS", data["prs"], VIOLET),
             ("CODE REVIEW", data["reviews"], PINK), ("ISSUES", data["issues"], LIME)]
    denom = sum(v for _, v, _ in kinds) or 1

    tiles = [("CONTRIBUTIONS", f"{total:,}"), ("CURRENT STREAK", f"{cur}d"),
             ("LONGEST STREAK", f"{best}d"), ("ACTIVE DAYS", f"{active}")]
    parts = []
    for i, (k, v) in enumerate(tiles):
        x = 28 + i * 214
        parts.append(f'<g class="in" style="animation-delay:{i * .12:.2f}s">'
                     f'<rect x="{x}" y="24" width="200" height="78" fill="{PANEL}" stroke="{GRID}" stroke-width="1.5"/>'
                     f'<rect x="{x}" y="24" width="200" height="3" fill="{PINK if i == 0 else TEXT}"/>'
                     f'<text x="{x + 16}" y="48" class="k">{k}</text>'
                     f'<text x="{x + 16}" y="86" font-size="30" font-weight="800">{v}</text></g>')

    parts.append(f'<text x="28" y="138" class="k">ACTIVITY BREAKDOWN</text>')
    for i, (k, v, c) in enumerate(kinds):
        y = 156 + i * 30
        pct = v / denom
        bw = max(4, 300 * pct)
        parts.append(f'<text x="28" y="{y + 11}" font-size="11" class="muted">{k}</text>'
                     f'<rect x="150" y="{y}" width="300" height="12" fill="{PANEL}" stroke="{GRID}"/>'
                     f'<rect x="150" y="{y}" width="{bw:.1f}" height="12" fill="{c}" class="bar" '
                     f'style="animation-delay:{.3 + i * .15:.2f}s"/>'
                     f'<rect x="{148 + bw:.1f}" y="{y - 3}" width="4" height="18" fill="{TEXT}" class="in" '
                     f'style="animation-delay:{1.4 + i * .15:.2f}s"/>'
                     f'<text x="465" y="{y + 11}" font-size="11">{pct * 100:.0f}% <tspan class="muted">· {v:,}</tspan></text>')

    parts.append(f'<text x="560" y="138" class="k">TOP REPOSITORIES</text>')
    top = data["per_repo"].most_common(5)
    rmax = top[0][1] if top else 1
    for i, (name, n) in enumerate(top):
        y = 156 + i * 24
        shown = name if name not in data["private"] or name in SHOW_PRIVATE else f"{name.split('/')[0]}/<private>"
        parts.append(f'<text x="560" y="{y + 10}" font-size="11">{escape(shown)}</text>'
                     f'<rect x="560" y="{y + 14}" width="{300 * n / rmax:.1f}" height="3" fill="{PINK if i == 0 else VIOLET}" class="bar" '
                     f'style="animation-delay:{.4 + i * .1:.2f}s"/>'
                     f'<text x="870" y="{y + 10}" font-size="10" text-anchor="end" class="muted">{n:,}</text>')
    rest = len(data["per_repo"]) - len(top)
    if rest > 0:
        parts.append(f'<text x="560" y="{156 + 5 * 24 + 6}" font-size="10" class="muted">+ {rest} more repositories</text>')
    parts.append(stamp(data, w, h))
    return svg(w, h, "\n".join(parts))


def calendar(data):
    today = data["now"].astimezone(TZ).date()
    start = today - timedelta(days=364)
    start -= timedelta(days=(start.weekday() + 1) % 7)  # weeks start on Sunday like GitHub
    weeks = (today - start).days // 7 + 1
    cell, oy, w = 15, 64, 900
    ox = w - 28 - weeks * cell
    h = oy + 7 * cell + 40
    vals = sorted(v for v in data["daily"].values() if v)
    q = [vals[int(len(vals) * f)] for f in (.25, .5, .75)] if vals else [1, 2, 3]
    levels = ["#1c1c1c", "#3d3d3d", "#737373", "#bdbdbd", TEXT]
    parts = [f'<text x="20" y="28" class="k">CONTRIBUTIONS / 365 DAYS</text>']
    for r, lbl in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        parts.append(f'<text x="{ox - 34}" y="{oy + r * cell + 4}" font-size="9" class="muted">{lbl}</text>')
    month = None
    for wk in range(weeks):
        d0 = start + timedelta(days=wk * 7)
        if d0.month != month and d0.day <= 7:
            month = d0.month
            parts.append(f'<text x="{ox + wk * cell - 4}" y="{oy - 12}" font-size="9" class="muted">{d0.strftime("%b")}</text>')
        for wd in range(7):
            d = d0 + timedelta(days=wd)
            if d > today:
                continue
            v = data["daily"].get(d, 0)
            lv = 0 if not v else 1 + sum(v > t for t in q)
            c = levels[lv]
            x, y = ox + wk * cell + 5, oy + wd * cell
            delay = (wk * 0.035 + wd * 0.02)
            cls = "in"
            dly = f"{delay:.2f}"
            parts.append(f'<rect x="{x - 6}" y="{y - 6}" width="12" height="12" fill="{PINK if d == today and v else c}" class="{cls}" '
                         f'style="animation-delay:{dly}s"><title>{d.isoformat()}: {v}</title></rect>')
    lx = w - 190
    parts.append(f'<text x="{lx}" y="{h - 14}" font-size="9" class="muted">less</text>')
    for i, c in enumerate(levels):
        parts.append(f'<rect x="{lx + 31 + i * 14}" y="{h - 22}" width="10" height="10" fill="{c}"/>')
    parts.append(f'<text x="{lx + 36 + 5 * 14}" y="{h - 14}" font-size="9" class="muted">more</text>')
    parts.append(f'<text x="20" y="{h - 14}" font-size="9" class="muted">{sum(data["daily"].values()):,} contributions · '
                 f'{len(data["per_repo"])} repositories</text>')
    return svg(w, h, "\n".join(parts))


def languages(data):
    w, h = 440, 300
    total = sum(data["langs"].values()) or 1
    top = sorted(data["langs"].items(), key=lambda kv: -kv[1])[:7]
    shade = {name: SHADES[i] for i, (name, _) in enumerate(top)}
    parts = [f'<text x="24" y="34" class="k">LANGUAGES</text>']
    x = 24
    for i, (name, v) in enumerate(top):
        bw = 392 * v / total
        parts.append(f'<rect x="{x:.1f}" y="50" width="{bw:.1f}" height="10" fill="{shade[name]}" class="bar" '
                     f'style="animation-delay:{i * .12:.2f}s"/>')
        x += bw
    parts.append(f'<rect x="24" y="50" width="392" height="10" fill="none" stroke="{GRID}"/>')
    for i, (name, v) in enumerate(top):
        y = 88 + i * 26
        c = shade[name]
        parts.append(f'<g class="in" style="animation-delay:{.3 + i * .1:.2f}s">'
                     f'<rect x="27" y="{y - 9}" width="10" height="10" fill="{c}"/>'
                     f'<text x="46" y="{y}" font-size="12">{escape(name)}</text>'
                     f'<text x="416" y="{y}" font-size="12" text-anchor="end">{v / total * 100:.1f}%</text>'
                     f'<rect x="160" y="{y - 7}" width="190" height="5" fill="{PANEL}"/>'
                     f'<rect x="160" y="{y - 7}" width="{max(2, 190 * v / top[0][1]):.1f}" height="5" fill="{c}" class="bar" '
                     f'style="animation-delay:{.5 + i * .1:.2f}s"/></g>')
    parts.append(stamp(data, w, h))
    return svg(w, h, "\n".join(parts))


def rhythm(data):
    w, h = 440, 300
    hours = Counter(d.astimezone(TZ).hour for d in data["commits"])
    days = Counter(d.astimezone(TZ).weekday() for d in data["commits"])
    n = len(data["commits"]) or 1
    peak_h = max(range(24), key=lambda x: hours[x]) if hours else 0
    peak_d = max(range(7), key=lambda x: days[x]) if days else 0
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    parts = [f'<text x="24" y="34" class="k">COMMIT HOURS / WEEKDAYS</text>',
             f'<text x="416" y="34" font-size="10" text-anchor="end" class="muted">peak {peak_h:02d}:00 · {names[peak_d]}</text>']
    hmax = max(hours.values() or [1])
    for hr in range(24):
        bh = 86 * hours[hr] / hmax
        x = 26 + hr * 16.4
        c = PINK if hr == peak_h else (VIOLET if hr < 6 or hr >= 22 else TEXT)
        parts.append(f'<rect x="{x:.1f}" y="{138 - bh:.1f}" width="12" height="{max(bh, 1.5):.1f}" fill="{c}" '
                     f'opacity="{.45 + .55 * hours[hr] / hmax:.2f}" class="col" style="animation-delay:{hr * .04:.2f}s"/>')
        if hr % 6 == 0:
            parts.append(f'<text x="{x:.1f}" y="154" font-size="9" class="muted">{hr:02d}</text>')
    dmax = max(days.values() or [1])
    for i in range(7):
        y = 178 + i * 14
        parts.append(f'<text x="24" y="{y + 8}" font-size="9" class="muted">{names[i]}</text>'
                     f'<rect x="60" y="{y}" width="{max(2, 300 * days[i] / dmax):.1f}" height="8" '
                     f'fill="{PINK if i == peak_d else VIOLET}" class="bar" '
                     f'style="animation-delay:{.4 + i * .08:.2f}s"/>'
                     f'<text x="416" y="{y + 8}" font-size="9" text-anchor="end">{days[i] / n * 100:.0f}%</text>')
    parts.append(stamp(data, w, h))
    return svg(w, h, "\n".join(parts))


def main():
    if not TOKEN:
        sys.exit("PROFILE_TOKEN is not set")
    data = collect()
    if not data["commits"]:
        sys.exit("no commits visible to this token, keeping the previous cards")
    os.makedirs(OUT, exist_ok=True)
    for name, fn in (("header", header), ("stats", stats), ("calendar", calendar),
                     ("languages", languages), ("rhythm", rhythm)):
        with open(os.path.join(OUT, f"{name}.svg"), "w", encoding="utf-8") as f:
            f.write(fn(data))
    print(f"{sum(data['daily'].values())} contributions, {len(data['commits'])} commits, "
          f"{len(data['per_repo'])} repos")


if __name__ == "__main__":
    main()
