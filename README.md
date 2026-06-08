# Cricket API for EchoKill

Fork of `tarun7r/Cricket-API`, modernized for a Docker-hosted EchoKill cricket-score microservice.

The original repo scraped Cricbuzz with static BeautifulSoup selectors that no longer matched the site. This fork uses the existing CamoFox/Camoufox browser service to render Cricbuzz and extract live-score/schedule data from the rendered DOM.

## Endpoints

- `GET /health` — service + CamoFox health
- `GET /live` — fast live/recent/upcoming Cricbuzz match list from Cricbuzz HTML, with CamoFox fallback
  - `?details=1` enables slower CamoFox detail-page fetches for top matches
  - `?limit=3` controls how many match detail pages are visited when details are enabled
- `GET /schedule` — upcoming schedule parsed from Cricbuzz schedule page
- `GET /players/<name>` — intentionally disabled for now; EchoKill integration only needs live/schedule

## Docker

```bash
docker compose up -d --build
curl http://localhost:8012/health
curl http://localhost:8012/live
```

Environment:

- `CRICKET_API_PORT` default `8012`
- `CAMOFOX_BASE_URL` default `http://192.168.40.86:9377`
- `CACHE_TTL_SECONDS` default `60`
- `LIVE_DETAIL_LIMIT` default `5`

## Notes

This is an unofficial scraper for personal/home-dashboard use. Cricbuzz markup can change; keep this as a separate microservice so EchoKill remains insulated from scraper breakage.
