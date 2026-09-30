# py_RichListings (server 01) - changes since GitHub `main` (14.09.2026)

Baseline: `GHGH2025/py_RichListings` main at `df15771c` (merge of PR #64, 14.09.2026).
Live code as of 30.09.2026 is on branch **`live-server01-20260930`**: a backup branch, not merged into main.
Scope: 35 changed files (+678 / -83 lines) and 7 new modules. Secrets are not in the repo, and this document names environment variables without their values.

---

## 1. New endpoints

| Route | Module | What it does |
|---|---|---|
| `POST /availability-email/generate` | `buyers/availability_email_gen.py` (new) | Writes the weekly "which of these are still available?" email to a wholesaler. Called by server 02 (`availabilityQueue.js`). Token-guarded. Rules are enforced in code, not only in the prompt: at most 2 questions, a rotating pool of question pairs per wholesaler, and phone numbers can never be listed as addresses. |
| `POST /listings/address-fixed` | `routes/address_fixed.py` (new) | Called by a GlobiFlow flow when someone sets Podio Properties "Address Review" (field 278194171) to *Fixed* or *Not fixable*. *Fixed* writes the new address, re-geocodes and sends the listing back through the duplicate gate and posting. *Not fixable* sets `wp_status=review_rejected`, so it never retries. |
| `POST /api/start`, `/api/stop`, `GET /api/status`, `GET /api/preview` | `routes/campaign_sms.py` (new) | Buyer SMS campaign over the Twilio 800 number, sent to the Podio "General Buyer" view (944). `/preview` only returns the audience size. |
| `GET /needs-photo` | `routes/needs_photo.py` (new) | Plain-English HTML page for the client's team (Ekta) listing deals whose images were rejected, and why (logo, banner, headshot, floor plan, map, wrong property and so on). |

## 2. Intake and extraction

- **Image extraction v2** (`pipeline/image_extract_v2.py`, new). Image URLs are now collected mechanically from the email HTML (`<img src>` in document order). Each image is assigned to the listing whose address appears most recently before it. The model no longer collects URLs; it only decides which listing is which. In the shadow run over 30,518 listings, images went from 30,365 to 45,728 (+51%), with 0 listings worse. A no-regression rule (`pick_images`) keeps the old result whenever the new one would have fewer images.
- **Masked house-number guard** (`pipeline/listing_details.py`, `_guard_masked_house_number`). If the source shows a masked number (`22**`, `13XX`), the extractor may not invent a clean one. The masked form is kept, so geocoding fails and the duplicate gate flags it instead of posting a fake address.
- **Google, step 1** (`integrations/google_formatter.py`, `pipeline/scrape_ingest.py`, `listing_details.py`). One Google call per listing: street, city and zip now come from the Geocoding response components. The paid Address Validation call was removed.
- **Scraping list** (`services/scraping_list_service.py`, `models/scraping_list.py`):
  - A pseudo-account `both` applies a sender pattern to every inbox.
  - `TRACKER_FORCE_BOTH` (env, reversible) folds acct1/acct2 choices from the tracker dropdown into `both`.
  - Patterns are de-duplicated across accounts.
  - Cache fix: each account now has its own timer. Before, one shared timer froze acct2's list.
- **Constant Contact relay addresses** (`services/direct_wholesaler_service.py`). `user@domain.ccsend.com` becomes `user@domain.com`, and `user-domain.tld@sharedN.ccsend.com` becomes `user@domain.tld`, before the wholesaler lookup. Known open issue: some pipeline records still end with `direct_wholeseller=not_found` for relay senders. The stage that writes that status does not call this function yet.

## 3. Duplicates and price drops (`pipeline/dedup.py`)

- **Cross-source duplicates.** A deal re-sent by another sender is matched against the original, including masked vs full addresses in both directions. The house number must be digit-consistent.
- **Price drops.** Any real drop (> 0%) re-activates and tags the post; the old 6% threshold is gone. An unchanged price is skipped as a plain duplicate.
- **Guard.** A drop above 50% (`PRICE_DROP_MAX_AUTO`, default 0.50) is held for review, never auto-updated, because it is almost always a misread price (e.g. "$410,00" read as 41,000). Held records appear in the review digest.

## 4. Rules

- **R6/R7 deterministic** (`ai/rules_runner.py`). A code check runs before the LLM, so the same data always gets the same verdict. "1-bath" means exactly 1 full bath, so 3/1.5 is not a 1-bath.
- `data/ai_listing_rules.yaml`: R2 price limit changed from $179,000 to $199,000 (rest of Florida, 2-bed).

## 5. Posting to WordPress

- **Gallery hold** (`pipeline/post_selection.py`). A listing whose source had a photo gallery never goes out without its Dropbox link. It is retried each run (about 10 minutes), then held for manual review.
- **Banner guard** (`integrations/wordpress/banner_guard.py`, new). Detects campaign banners and logos, so they are never sent as the featured image.
- **Address review ("Cloud A")** (`integrations/wordpress/sync_poster.py`, `buyers/matching_api.py`). A listing set to `needs_address_review` is mirrored to Podio "Address Review" = *Needs review*. After a fix it reports *Published*, or *Duplicate of <post_id>*.
- **Review digest** (`integrations/wordpress/needs_review_digest.py`). Now sent through server 02 `/internalAlert`, with no copy to the client. It includes gallery holds and the >50% price holds.
- **WordPress proxy** (`routes/wordpress_proxy.py`). Accepts the older tokens that GlobiFlow flows still carry (`WP_PROXY_ACCEPTED_TOKENS`). The call to WordPress itself always uses the server-side token.

## 6. Podio, wholesalers, special avails

- **Allan guard** (`integrations/podio/direct_wholesaler.py`). Only direct senders can create a Wholeseller item. A non-direct sender only links to an existing one.
- **Special avails** (`special_avails/inactive_processor.py`). The inactive webhook now includes `podio_item_id`, so GlobiFlow acts on the item id, not on a fuzzy address match.

## 7. Buyer emails and SMS

- **Daily deal-email cap** (`buyers/matched_process.py`, since 30.09). There is a rolling 24-hour cap on deal-email send attempts (`DEAL_EMAIL_DAILY_CAP`, default 1500, 0 = off). It exists because rich@wholesaledealfinder.ai hit Gmail's daily sending limit on 28-29.09. When the cap is reached, the rest stay queued and are retried later. A listing's batch is never cut in half.
- **SMS suppression** (`services/sms_suppression.py`, new). The STOP list: 6,410 numbers from the Twilio export of 01.09. Numbers are matched on their last 10 digits. It is the same list as `src/suppression.js` on server 02.

## 8. AI models

- Model per call, set by environment variables, default `gpt-6-luna`:
  - `WHATSAPP_POST_MODEL`
  - `WP_MAPPER_MODEL`
  - `WP_PRICE_MEDIA_MODEL`
  - `WP_PRICE_MATCH_MODEL`
  - `BUYER_DESC_MODEL`
  - `BUYER_EMAIL_BOUNCE_CHECK_MODEL`
  - `OPENAI_VISION_MODEL`
  - `OPENAI_MODEL` (in the modules that use it)
- **Temperature gate** (`observability/openai_usage.py`, `temp_kwargs`). gpt-5 and gpt-6 models reject `temperature`, so it is only sent to models that accept it.
- **Still on the old model, pending a side-by-side test:**
  - rules judge (`ai/rules_judge.py`, gpt-5)
  - property description (`ai_property_description.py`, gpt-4.1)
  - matching API (`buyers/matching_api.py`)

## 9. Operations

- **Mongo pool** (`db/mongo_engine_conn.py`): `maxPoolSize` 20 (`MONGO_MAX_POOL_SIZE`) and idle sockets released after 60 s (`MONGO_MAX_IDLE_MS`).
- **Cost tracking**: `observability/openai_pricing.py` has the gpt-6-luna price entry.

## 10. Environment variables added (names only)

`DEAL_EMAIL_DAILY_CAP`, `PRICE_DROP_MAX_AUTO`, `TRACKER_FORCE_BOTH`, `WP_PROXY_ACCEPTED_TOKENS`, `INTERNAL_ALERT_URL`, `INTERNAL_ALERT_TOKEN`, `PODIO_ADDRESS_REVIEW_FIELD_ID`, `PODIO_ADDRESS_REVIEW_NEEDS_OPT`, `MONGO_MAX_POOL_SIZE`, `MONGO_MAX_IDLE_MS`, plus the model variables in section 8.

## 11. Known open items (30.09)

- Relay senders still reach `direct_wholeseller=not_found` (section 2).
- The pipeline-metrics write fails for WhatsApp listings (`FieldDoesNotExist` on the `wa_*` fields in `ListingPipelineMetric`). So the tracker can show an old stage, e.g. "rules", after a deal is already posted.
- Addresses taken from a "Comps" section of an email: the extractor fix is being built.
- The 2-bed rule R1 should apply to single family only.
- Masked vs full dedup missed at least one case ("13XX SE 1st Way" vs "1328 SE 1st Way", Deerfield): both went live. Under review.
