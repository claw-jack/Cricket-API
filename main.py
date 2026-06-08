import html as html_lib
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

CAMOFOX_BASE_URL = os.getenv("CAMOFOX_BASE_URL", "http://192.168.40.86:9377").rstrip("/")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "45"))
LIVE_DETAIL_LIMIT = int(os.getenv("LIVE_DETAIL_LIMIT", "5"))
CRICBUZZ_LIVE_URL = "https://www.cricbuzz.com/cricket-match/live-scores"
CRICBUZZ_SCHEDULE_URL = "https://www.cricbuzz.com/cricket-schedule/upcoming-series/international"

_cache: dict[str, tuple[float, Any]] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cached(key: str, producer: Callable[[], Any], ttl: int = CACHE_TTL_SECONDS) -> Any:
    now = time.time()
    if key in _cache:
        ts, value = _cache[key]
        if now - ts < ttl:
            return value
    value = producer()
    _cache[key] = (now, value)
    return value


class CamoFoxClient:
    def __init__(self, base_url: str = CAMOFOX_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = f"cricket-api-{uuid.uuid4().hex[:10]}"
        self.session_key = self.user_id
        self.session = requests.Session()
        self.tab_id: str | None = None

    def _payload(self, **extra: Any) -> dict[str, Any]:
        payload = {"userId": self.user_id, "sessionKey": self.session_key}
        payload.update(extra)
        return payload

    def create_tab(self) -> str:
        response = self.session.post(f"{self.base_url}/tabs", json=self._payload(), timeout=30)
        response.raise_for_status()
        self.tab_id = response.json()["tabId"]
        return self.tab_id

    def navigate(self, url: str, wait_seconds: float = 3.0) -> None:
        tab = self.tab_id or self.create_tab()
        response = self.session.post(
            f"{self.base_url}/tabs/{tab}/navigate",
            json=self._payload(url=url),
            timeout=60,
        )
        response.raise_for_status()
        time.sleep(wait_seconds)

    def evaluate(self, expression: str) -> Any:
        tab = self.tab_id or self.create_tab()
        response = self.session.post(
            f"{self.base_url}/tabs/{tab}/evaluate",
            json=self._payload(expression=expression),
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        return body.get("result", body)

    def close(self) -> None:
        try:
            self.session.delete(f"{self.base_url}/sessions/{self.user_id}", timeout=10)
        except Exception:
            pass


LIVE_LINKS_JS = r"""
(() => {
  const seen = new Map();
  for (const a of Array.from(document.querySelectorAll('a[href*="/live-cricket-scores/"]'))) {
    const href = a.href;
    const text = (a.innerText || '').trim().replace(/\s+/g, ' ');
    if (!href || !text) continue;
    const match = href.match(/live-cricket-scores\/(\d+)\/([^?#]+)/);
    if (!match) continue;
    const current = seen.get(href);
    // Prefer richer non-ticker labels such as "India vs Afghanistan One-off Test" over "IND vs AFG - Stumps".
    if (!current || text.length > current.text.length) {
      seen.set(href, {id: match[1], slug: match[2], href, text});
    }
  }
  return Array.from(seen.values()).slice(0, 60);
})()
"""

DETAIL_JS = r"""
(() => ({
  title: document.title,
  h1: Array.from(document.querySelectorAll('h1,h2,h3')).map(e => e.innerText.trim()).filter(Boolean).slice(0, 5),
  text: document.body.innerText.slice(0, 7000)
}))()
"""

SCHEDULE_JS = r"""
(() => ({
  title: document.title,
  text: document.body.innerText.slice(0, 12000)
}))()
"""


def parse_detail(match: dict[str, str], detail: dict[str, Any]) -> dict[str, Any]:
    text = detail.get("text") or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = detail.get("title") or ""

    score_title = html_lib.unescape(title.split("|")[0].strip())
    if "live cricket stream" in score_title.lower() and not re.search(r"\b[A-Z][A-Z0-9]{1,5}\s+\d", score_title):
        score_title = ""
    status = None
    scan_lines = lines
    headings = detail.get("h1") or []
    if headings:
        try:
            heading_idx = lines.index(headings[0])
            # Skip the global Cricbuzz ticker/header so one match cannot inherit another match's status.
            scan_lines = lines[heading_idx + 1 :]
        except ValueError:
            pass
    for line in scan_lines:
        lowered = line.lower()
        if any(token in lowered for token in ["stumps", "won by", "trail", "lead", "need", "innings", "delayed", "abandoned", "starts"]):
            if len(line) > 8 and not line.startswith("MATCHES"):
                status = line
                break

    venue = None
    start_time = None
    series = None
    for i, line in enumerate(lines):
        if line.startswith("Series:"):
            series = line.replace("Series:", "").strip()
        elif line.startswith("Venue:"):
            venue = line.replace("Venue:", "").strip()
        elif line.startswith("Date & Time:"):
            start_time = line.replace("Date & Time:", "").strip()
        elif line == "Series:" and i + 1 < len(lines):
            series = lines[i + 1]
        elif line == "Venue:" and i + 1 < len(lines):
            venue = lines[i + 1]
        elif line == "Date & Time:" and i + 1 < len(lines):
            start_time = lines[i + 1]

    teams = []
    m = re.search(r"/live-cricket-scores/\d+/([a-z0-9]+)-vs-([a-z0-9]+)-", match.get("href", ""))
    if m:
        teams = [m.group(1).upper(), m.group(2).upper()]

    return {
        **match,
        "matchTitle": (detail.get("h1") or [match.get("text")])[0],
        "score": score_title or None,
        "status": status or match.get("text"),
        "series": series,
        "venue": venue,
        "startTime": start_time,
        "teams": teams,
    }


def normalize_match_link(href: str, text: str) -> dict[str, str] | None:
    href = href.replace("&amp;", "&")
    if href.startswith("/"):
        href = f"https://www.cricbuzz.com{href}"
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    match = re.search(r"live-cricket-scores/(\d+)/([^?#\"]+)", href)
    if not match or not text:
        return None
    return {"id": match.group(1), "slug": match.group(2), "href": href, "text": text}


def _http_text(url: str, *, timeout: float = 12) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.text


def scrape_live_fast() -> dict[str, Any]:
    html = _http_text(CRICBUZZ_LIVE_URL)
    seen: dict[str, dict[str, str]] = {}
    for href, label in re.findall(r'href="([^"]*live-cricket-scores/[^"]+)"[^>]*>(.*?)</a>', html, re.S):
        item = normalize_match_link(href, label)
        if not item:
            continue
        key = item["id"]
        current = seen.get(key)
        if not current or len(item["text"]) > len(current["text"]):
            seen[key] = item
    matches = list(seen.values())[:80]
    return {"source": "cricbuzz/http", "fetchedAt": utc_now(), "count": len(matches), "matches": matches}


def _team_aliases(team: str) -> list[str]:
    normalized = team.strip().lower()
    if normalized in {"england", "england men", "england-men", "eng"}:
        return ["england"]
    if normalized in {"nepal", "nepal men", "nepal-men", "nep"}:
        return ["nepal"]
    return [normalized]


def _matches_team_filter(match: dict[str, Any], teams: list[str]) -> bool:
    if not teams:
        return True
    text = str(match.get("text") or match.get("matchTitle") or "")
    slug = str(match.get("slug") or "")
    haystack = f"{text} {slug} {match.get('score') or ''} {match.get('status') or ''}".lower()
    if any(excluded in haystack for excluded in ("england women", "engw", "england lions", "enga", " nepal women", "nepw")):
        # England/Nepal filters mean senior men's national sides for EchoKill.
        if any(team.strip().lower() in {"england", "england men", "eng", "nepal", "nepal men", "nep"} for team in teams):
            return False
    for team in teams:
        for alias in _team_aliases(team):
            if re.search(rf"(^|\b){re.escape(alias)}\s+vs\b|\bvs\s+{re.escape(alias)}(\b|$)", text.lower()):
                return True
            if alias in haystack:
                return True
    return False


def _strip_html_text(raw_html: str) -> str:
    import html as html_lib

    raw_html = re.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=re.S | re.I)
    raw_html = re.sub(r"<style[^>]*>.*?</style>", " ", raw_html, flags=re.S | re.I)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "\n", raw_html))
    return re.sub(r"\n{2,}", "\n", text)


def fetch_match_detail_http(match: dict[str, str]) -> dict[str, Any]:
    raw = _http_text(match["href"], timeout=15)
    title_match = re.search(r"<title>(.*?)</title>", raw, re.S | re.I)
    h1s = [re.sub(r"\s+", " ", _strip_html_text(value)).strip() for value in re.findall(r"<h[123][^>]*>(.*?)</h[123]>", raw, re.S | re.I)]
    return parse_detail(match, {"title": re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else "", "h1": [h for h in h1s if h], "text": _strip_html_text(raw)})


def scrape_live(include_details: bool = True, detail_limit: int = LIVE_DETAIL_LIMIT, teams: list[str] | None = None) -> dict[str, Any]:
    teams = teams or []
    if not include_details:
        try:
            payload = scrape_live_fast()
            matches = [match for match in payload["matches"] if _matches_team_filter(match, teams)]
            return {**payload, "count": len(matches), "matches": matches, "teamFilters": teams}
        except Exception:
            # Fall back to CamoFox if direct Cricbuzz HTML ever blocks or changes.
            pass

    # Prefer direct detail-page HTML for score parsing; it is far faster than browsering every match.
    try:
        payload = scrape_live_fast()
        base_matches = [match for match in payload["matches"] if _matches_team_filter(match, teams)]
        detailed = [fetch_match_detail_http(match) for match in base_matches[:detail_limit]]
        matches = detailed + base_matches[detail_limit:]
        return {**payload, "source": "cricbuzz/http-detail", "count": len(matches), "matches": matches, "teamFilters": teams}
    except Exception:
        pass

    client = CamoFoxClient()
    try:
        client.navigate(CRICBUZZ_LIVE_URL, wait_seconds=3)
        matches = client.evaluate(LIVE_LINKS_JS)
        matches = [match for match in matches if _matches_team_filter(match, teams)]
        if include_details:
            detailed: list[dict[str, Any]] = []
            for match in matches[:detail_limit]:
                try:
                    client.navigate(match["href"], wait_seconds=1.5)
                    detail = client.evaluate(DETAIL_JS)
                    detailed.append(parse_detail(match, detail))
                except Exception as exc:
                    detailed.append({**match, "error": str(exc)})
            matches = detailed + matches[detail_limit:]
        return {"source": "cricbuzz/camofox", "fetchedAt": utc_now(), "count": len(matches), "matches": matches, "teamFilters": teams}
    finally:
        client.close()


def parse_schedule_text(text: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    events: list[dict[str, Any]] = []
    current_date = None
    current_series = None
    start = 0
    for marker in ["MON,", "TUE,", "WED,", "THU,", "FRI,", "SAT,", "SUN,"]:
        for i, line in enumerate(lines):
            if line.startswith(marker):
                start = i
                break
        if start:
            break
    i = start
    while i < len(lines):
        line = lines[i]
        if re.match(r"^(MON|TUE|WED|THU|FRI|SAT|SUN),", line):
            current_date = line
        elif i + 3 < len(lines) and " vs " in lines[i + 1]:
            current_series = line
        elif " vs " in line:
            event = {
                "date": current_date,
                "series": current_series,
                "match": line,
                "venue": lines[i + 1] if i + 1 < len(lines) else None,
                "localTime": lines[i + 2] if i + 2 < len(lines) else None,
                "gmtTime": lines[i + 3] if i + 3 < len(lines) else None,
            }
            events.append(event)
            i += 3
        i += 1
    return events[:80]


def scrape_schedule() -> dict[str, Any]:
    client = CamoFoxClient()
    try:
        client.navigate(CRICBUZZ_SCHEDULE_URL, wait_seconds=4)
        result = client.evaluate(SCHEDULE_JS)
        events = parse_schedule_text(result.get("text", ""))
        return {"source": "cricbuzz/camofox", "fetchedAt": utc_now(), "count": len(events), "matches": events}
    finally:
        client.close()


@app.route("/health")
def health() -> Any:
    try:
        camofox = requests.get(f"{CAMOFOX_BASE_URL}/health", timeout=5).json()
    except Exception as exc:
        camofox = {"ok": False, "error": str(exc)}
    return jsonify({"ok": bool(camofox.get("ok")), "camofox": camofox, "cacheKeys": list(_cache.keys())})


@app.route("/live")
def live_matches() -> Any:
    # Fast default for browser/EchoKill consumers: the rendered Cricbuzz list is enough
    # to prove liveness and avoids multiple slow detail-page browser navigations.
    # Ask for details explicitly with /live?details=1&limit=1.
    include_details = request.args.get("details", "0") in {"1", "true", "yes"}
    detail_limit = int(request.args.get("limit", str(LIVE_DETAIL_LIMIT if include_details else 0)))
    teams = [part.strip() for part in request.args.get("teams", "").split(",") if part.strip()]
    key = f"live:{include_details}:{detail_limit}:{','.join(sorted(teams)).lower()}"
    return jsonify(cached(key, lambda: scrape_live(include_details=include_details, detail_limit=detail_limit, teams=teams)))


@app.route("/schedule")
def schedule() -> Any:
    return jsonify(cached("schedule", scrape_schedule, ttl=max(CACHE_TTL_SECONDS, 300)))


@app.route("/players/<player_name>")
def get_player(player_name: str) -> Any:
    return jsonify({
        "error": "player_stats_not_implemented",
        "message": "This fork currently prioritizes live scores/schedules for EchoKill. Player stats can be re-added with a stable provider later.",
        "player": player_name,
    }), 501


@app.route("/")
def website() -> Any:
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=os.getenv("FLASK_DEBUG") == "1")
