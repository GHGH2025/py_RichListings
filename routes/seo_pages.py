"""SEO pages feed for the tracker tabs "Seo address" / "Seo URLs" (Rich 06.10).

Source: ~/seo2_backups/seo_pages_log.csv on a2hosting - every publish or weekly refresh of a joinbuyerslist.com
SEO page appends one line: date,action,page_id,url,sheet (actions: updated_v2 = rollout, refreshed = weekly job).
It is copied to this server hourly by ~/seo_part2/feed_pull.sh into SEO_PAGES_LOG
(default ~/seo_part2/feed/seo_pages_log.csv). Read-only, no secrets, public URLs only - same exposure as /api/scraping-list.

  GET /api/seo-pages?since=2026-10-01&action=refreshed&url_contains=miami&limit=500   rows, newest first
  GET /api/seo-pages/summary                                                          totals, per action, per day
"""
import csv
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["seo-pages"])
LOG = Path(os.getenv("SEO_PAGES_LOG", os.path.expanduser("~/seo_part2/feed/seo_pages_log.csv")))


class SeoPageRow(BaseModel):
  at: str          # "2026-10-07 08:23:12" UTC
  action: str      # updated_v2 | refreshed
  page_id: int     # WordPress page ID on joinbuyerslist.com
  url: str
  sheet: str       # tab of Rich's "SEO & ALL PAGES SHEET"


def _rows() -> List[SeoPageRow]:
  if not LOG.exists():
    return []
  out: List[SeoPageRow] = []
  with LOG.open(encoding="utf-8", newline="") as f:
    for r in csv.reader(f):
      if len(r) < 4 or len(r[0]) < 10:
        continue
      try:
        pid = int(r[2])
      except ValueError:
        continue
      out.append(SeoPageRow(at=r[0], action=r[1], page_id=pid, url=r[3], sheet=r[4] if len(r) > 4 else ""))
  return out


@router.get("/seo-pages", response_model=List[SeoPageRow])
def list_seo_pages(
  since: Optional[str] = Query(None, description="YYYY-MM-DD, inclusive"),
  action: Optional[str] = Query(None, description="updated_v2 or refreshed"),
  url_contains: Optional[str] = None,
  limit: int = Query(500, ge=1, le=5000),
):
  rows = _rows()
  if since:
    rows = [r for r in rows if r.at[:10] >= since]
  if action:
    rows = [r for r in rows if r.action == action]
  if url_contains:
    needle = url_contains.lower()
    rows = [r for r in rows if needle in r.url.lower()]
  rows.sort(key=lambda r: r.at, reverse=True)
  return rows[:limit]


@router.get("/seo-pages/summary")
def seo_pages_summary() -> Dict:
  rows = _rows()
  by_day: Dict[str, Counter] = defaultdict(Counter)
  by_action: Counter = Counter()
  pages = set()
  for r in rows:
    by_day[r.at[:10]][r.action] += 1
    by_action[r.action] += 1
    pages.add(r.page_id)
  feed_updated = (
    datetime.fromtimestamp(LOG.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if LOG.exists() else None
  )
  return {
    "total_rows": len(rows),
    "distinct_pages": len(pages),
    "by_action": dict(by_action),
    "by_day": {d: dict(c) for d, c in sorted(by_day.items())},
    "last_at": max((r.at for r in rows), default=None),
    "feed_updated_utc": feed_updated,
  }
