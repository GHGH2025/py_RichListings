# Web scraper ingestion

## Flow

The scraper scheduler runs both supported sites headlessly:

1. Florida Off Market is collected by its existing job.
2. Rezzie is collected from the buyer dashboard; every accessible `Details`
   action is resolved to a property URL and then fully extracted.
3. Each complete listing is stored in MongoDB `raw` with `source_website` for
   source/audit purposes only.
4. The scraper applies a cross-source, 30-day address gate before inserting a
   `filtered` record. Any address created in `filtered` or `parsed_listings`
   within the last 30 days is ignored; older or unseen addresses are queued.
5. `pipeline.scrape_ingest.process_pending_scraped_listings()` consumes only
   pending records from MongoDB `filtered`; it never ingests directly from
   `raw`. It routes website data separately from email and WhatsApp
   parsing, then creates `FilteredListingEmail` and `ParsedListing` records.
6. Existing rules, media/Dropbox, posting, WordPress, buyer matching, and
   telemetry jobs process the resulting `ParsedListing` normally. New web
   records are created with `web_publish_enabled=true`, so they follow the
   same WordPress, Podio, and WhatsApp path as email and WhatsApp listings
   after rules and media.

## Publication toggle

Web records use `ParsedListing.web_publish_enabled`. New scrapes ingest with
it on. Set it to `false` on a record to keep that listing in the pipeline
for review without external publication. Email and WhatsApp records are not
subject to this web-only gate.

```python
from models import ParsedListing
ParsedListing.objects(input_source="web", address="123 Main St").update(
    set__web_publish_enabled=False
)
```

The Deals Details metrics page includes a `Web scraper` view and shows the
source website and publication state in each listing's details.

## Source telemetry

Web records carry:

- `input_source: web`
- `source_website: florida_off_market` or `rezzie`
- `source_listing_id` on raw records

The same source fields are persisted to filtered records, parsed listings,
source-email audit records, and pipeline metrics.

## Wholesalers

Rezzie provides structured seller contact information. The ingestor upserts
that contact in `direct_wholesalers`, using email as its key. The existing
Podio direct-wholesaler worker searches Podio by that email; when absent, it
creates the minimal Wholesalers app item with the email field before linking a
matching Podio property.

For web listings only, a usable wholesaler email is mandatory. Listings where
the Rezzie seller email is missing, or where Florida Off Market AI enrichment
cannot find one, are rejected before entering the listing pipeline. Email and
WhatsApp ingestion is unchanged: their sender may be an ordinary account and
does not need to be a wholesaler.

Florida Off Market is also kept as a distinct web source. Its existing scraper
currently supplies wholesaler name data; contact enrichment remains fail-open
when no email is present, so a listing never fails solely because a wholesaler
cannot be identified.

## Operations

The scheduler runs headlessly on EC2 `rich-ai` as systemd
`richie-web-scraper.service` (`/home/ubuntu/apps/web_scraper`). Host, logs,
and health-check commands: [docs/ec2-notes/scraper.md](../../docs/ec2-notes/scraper.md).

The Python server runner already consumes pending filtered web listings once
per minute through `process_pending_scraped_listings`. New web listings
ingest with publication on.

Never run the Podio direct-wholesaler worker against production credentials
until the email-only Wholesalers app creation behavior has been approved.
