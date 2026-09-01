# RichListings

Web-scraped listings are ingested separately from email and WhatsApp, then
join the normal listing pipeline as `ParsedListing` records. The complete
source, 30-day deduplication, wholesaler, and telemetry design is documented
in [docs/web_scraper_ingestion.md](docs/web_scraper_ingestion.md).
