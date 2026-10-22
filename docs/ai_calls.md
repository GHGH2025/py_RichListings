# AI Calls Inventory

This document lists every **active** OpenAI call in the RichListings Python worker (`py_RichListings`). All calls use the OpenAI Python SDK via `client.chat.completions.create`. There are **no AI calls in the React frontend**.

**Last audited:** 2026-10-13 (model defaults re-verified against code; file paths updated to current layout)

---

## Summary

| Metric | Count |
|--------|------:|
| **Distinct AI functions** (one logical use case each) | **21** |
| **Source files** with active AI calls | **14** |
| **Raw `chat.completions.create` call sites** | **22** |
| **AI provider** | OpenAI only |
| **Frontend AI calls** | 0 |

> **Note on call sites vs functions:** `extract_listings_from_email_html` has two call sites (structured output + JSON fallback on error). Image curation can invoke AI many times per listing (once per image, plus ordering, plus primary verification). Buyer matching batches candidates but still counts as one function per batch.

> **Model note (2026-10-13):** most text/vision calls now default to `gpt-6-luna` (the current default for `OPENAI_MODEL` and several per-module overrides). Exceptions that are intentionally **not** on luna: the NL rules judge (`gpt-5`), the WP property description (`gpt-4.1`), the special-avail active-listing match (`gpt-4.1-mini`), buyer matching (`gpt-4o-mini`), and RC media linking (`gpt-4o-mini`). Defaults are shown as `ENV_VAR` → `fallback`; a plain value means it is hardcoded.

---

## By pipeline stage

### 1. Ingestion & extraction

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 1 | `extract_listings_from_email_html` | `pipeline/listing_details.py` | `OPENAI_MODEL` → `gpt-6-luna` | Email parse job | Parse wholesaler email HTML into structured listing JSON (structured output; falls back to JSON mode on error) |
| 2 | `ai_address_search_keys` | `ai/address_keys.py` | `gpt-6-luna` (hardcoded) | After listing save (`update_parsed_listing_address_keys`) | Generate address search-key variants for dedup / WP lookup |
| 3 | `ai_verify_media_for_listing` | `ai/media_verify.py` | `OPENAI_MODEL` → `gpt-6-luna` | Scheduled: `verify_and_fill_missing_media_for_not_processed` (every 2 min, limit 35) | Re-scan email HTML to recover missing images / gallery links |

### 2. Qualification & rules

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 4 | `judge_listing_with_english_rules` | `ai/rules_judge.py` | `OPENAI_MODEL` → `gpt-5` | Scheduled: `apply_ai_english_rules` (every 5 min, limit 50) | Pass/skip listing against YAML business rules |
| 5 | `_ai_city_in_do_not_post` | `pipeline/post_selection.py` | `gpt-6-luna` (hardcoded) | During post selection (cached per city) | Fuzzy-match listing city against do-not-post city list |

### 3. Image curation

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 6 | `classify_single_image` | `ai/image_curation.py` | `OPENAI_VISION_MODEL` → `gpt-6-luna` | Per image during curation | Vision: keep/skip non-property images (logos, flyers, etc.) |
| 7 | `order_property_images` | `ai/image_curation.py` | `OPENAI_VISION_MODEL` → `gpt-6-luna` | Once per listing after filtering | Vision: rank kept photos; pick best cover image |
| 8 | `classify_primary_image` | `ai/image_curation.py` | Caller-provided (scheduler uses `gpt-6-luna`) | Scheduled: `process_primary_image_verification` (every 2 min, limit 5) | Stricter single-model primary-image gate before posting |

**Volume note:** For a listing with *N* images, curation typically makes **N + 1** AI calls (N classifications + 1 ordering). Primary verification adds **1** more call per listing.

### 4. Content generation (WhatsApp & WordPress)

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 9 | `_compose_post` | `ai/whatsapp_posts.py` | `WHATSAPP_POST_MODEL` → `gpt-6-luna` | Scheduled: `make_whatsapp_posts_from_ready_to_post` (every 2 min, limit 5) | Generate WhatsApp ad copy from listing + rules file |
| 10 | `ai_build_wp_payload_catalog_first` | `integrations/wordpress/ai_mapper.py` | `WP_MAPPER_MODEL` → `gpt-6-luna` | Scheduled: `ai_build_wp_payload_for_posted` (every 3 min, limit 5) | Map listing to WP taxonomy (region, country deals, property type) |
| 11 | `ai_build_wp_property_description_for_listing` | `integrations/wordpress/ai_property_description.py` | `OPENAI_MODEL` → `gpt-4.1` | Scheduled: `ai_build_wp_property_description_for_posted` (every 3 min, limit 5) | Generate HTML property description for WordPress |

### 5. WordPress maintenance & linking

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 12 | `ai_verify_same_listing` | `integrations/wordpress/price_media_updates.py` | `WP_PRICE_MEDIA_MODEL` → `gpt-6-luna` | Scheduled: `process_wp_price_and_media_updates` (every 2 min, limit 5) | Confirm WP post address matches our listing key (price/reduction updates) |

### 6. RingCentral media linking

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 13 | `ai_extract` | `media/rc_linker.py` | `OPENAI_MODEL` → `gpt-4o-mini` | RC webhook / `process_direct_wholeseller_batch` (every 3 min) | Extract photo URL, street number, address from buyer/seller SMS dialog |
| 14 | `ai_verify_same` | `media/rc_linker.py` | `gpt-4o-mini` | During RC media link flow | Verify extracted address matches WP record |
| 15 | `_ai_guard_media_url` | `media/rc_linker.py` | `OPENAI_MODEL` → `gpt-4o-mini` | When URL passes host/MIME heuristics but needs context check | Accept/reject URL as property media based on recent dialog |

### 7. Special availability (Podio / Google Sheets)

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 16 | `ai_match_active_to_unique` | `special_avails/processor.py` | `OPENAI_MATCH_MODEL` → `gpt-4.1-mini` | Scheduled: `process_one_special_avail_with_active_listings` (every 3 min) | Match Podio active address to deduped candidate list |
| 17 | `ai_match_address_in_sheet` | `special_avails/processor.py` | `MANNY_MATCH_MODEL` → `gpt-6-luna` | Scheduled: `process_one_special_avail_matching` (every 5 min) | Fuzzy-match Podio address inside wholesaler Google Sheet text |

### 8. Buyer matching & notifications

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 18 | `call_ai_type_matcher` | `buyers/matching_api.py` | `MATCHER_MODEL` → `gpt-4o-mini` | `POST /match` + buyer matching cron | Semantic property-type match (buyer `selected_types` vs listing) |
| 19 | `call_ai_matcher` | `buyers/matching_api.py` | `MATCHER_MODEL` → `gpt-4o-mini` | `POST /match` + buyer matching cron | Classify special preference labels as PRESENT / ABSENT / UNKNOWN |
| 20 | `ai_build_buyer_sms_description_for_listing` | `buyers/matched_process.py` | `DEFAULT_BUYER_DESC_MODEL` → `gpt-6-luna` | Scheduled: `process_pending_buyer_descriptions` (every 5 min, limit 5) | Short SMS body for matched buyer notification |
| 21 | `ai_build_buyer_email_description_for_listing` | `buyers/matched_process.py` | `DEFAULT_BUYER_DESC_MODEL` → `gpt-6-luna` | Same job as above (2 calls per listing) | Short email body for matched buyer notification |

**Volume note:** Buyer matching batches candidates (`MATCHER_AI_BATCH_SIZE`, default **8**), so one `/match` run can make **multiple** type-matcher and preference-matcher calls. Description generation makes **2** calls per matched listing (SMS + email).

---

## Scheduled jobs that invoke AI

From `server_runner.py`:

| Interval | Job | AI function(s) |
|----------|-----|------------------|
| Every 1 min | Email → listing parse | `extract_listings_from_email_html`, `ai_address_search_keys` |
| Every 2 min | Media verify | `ai_verify_media_for_listing` |
| Every 2 min | Image curation | `classify_single_image`, `order_property_images` |
| Every 2 min | Primary image check | `classify_primary_image` |
| Every 2 min | WhatsApp ad gen | `_compose_post` |
| Every 2 min | WP price/media updates | `ai_verify_same_listing` |
| Every 3 min | WP taxonomy | `ai_build_wp_payload_catalog_first` |
| Every 3 min | WP description | `ai_build_wp_property_description_for_listing` |
| Every 3 min | Direct wholesaler RC linking | `ai_extract`, `ai_verify_same`, `_ai_guard_media_url` |
| Every 3 min | Special avail (active listings) | `ai_match_active_to_unique` |
| Every 5 min | NL rules judge | `judge_listing_with_english_rules` |
| Every 5 min | Post selection | `_ai_city_in_do_not_post` (conditional, cached) |
| Every 5 min | Special avail (sheet match) | `ai_match_address_in_sheet` |
| Every 5 min | Buyer descriptions | SMS + email description builders |
| Configurable | Buyer matching cron | `call_ai_type_matcher`, `call_ai_matcher` |

Post selection and RC media linking also run outside the scheduler when their respective flows execute.

---

## Environment variables

| Variable | Used by | Default |
|----------|---------|---------|
| `OPENAI_API_KEY` | All modules (required) | - |
| `OPENAI_MODEL` | `pipeline/listing_details.py`, `ai/media_verify.py`, `ai/rules_judge.py`, `media/rc_linker.py` | varies: `gpt-6-luna` (parse, media verify), `gpt-5` (judge), `gpt-4o-mini` (rc linker) |
| `OPENAI_VISION_MODEL` | `ai/image_curation.py` | `gpt-6-luna` |
| `WHATSAPP_POST_MODEL` | `ai/whatsapp_posts.py` | `gpt-6-luna` |
| `WP_MAPPER_MODEL` | `integrations/wordpress/ai_mapper.py` | `gpt-6-luna` |
| `WP_PRICE_MEDIA_MODEL` / `WP_PRICE_MATCH_MODEL` | `integrations/wordpress/price_media_updates.py` | `gpt-6-luna` |
| `OPENAI_MATCH_MODEL` | `special_avails/processor.py` → `ai_match_active_to_unique` | `gpt-4.1-mini` |
| `MANNY_MATCH_MODEL` | `special_avails/processor.py` → `ai_match_address_in_sheet` | `gpt-6-luna` (hardcoded) |
| `MATCHER_MODEL` | `buyers/matching_api.py` | `gpt-4o-mini` |
| `BUYER_DESC_MODEL` | `buyers/matched_process.py` → `DEFAULT_BUYER_DESC_MODEL` | `gpt-6-luna` |
| `BUYER_EMAIL_BOUNCE_CHECK_MODEL` | `buyers/email_bounce_check.py` | `gpt-6-luna` |
| `AVAILABILITY_EMAIL_MODEL` | `buyers/availability_email_gen.py` | `gpt-6-luna` |
| `MATCHER_AI_BATCH_SIZE` | Buyer matching batch size | 8 |

> **Temperature note:** modules that call luna/gpt-5/gpt-6 omit the `temperature` parameter (those families reject a non-default temperature), via a `startswith(("gpt-5", "gpt-6"))` guard in `listing_details.py`, `media_verify.py`, `image_curation.py`, and `special_avails/processor.py`.

---

## Commented / inactive AI code

These exist in the repo but are **not** executed in the current flow:

| Location | Notes |
|----------|-------|
| `buyers/matching_api.py` ~709–744 | Older commented `call_ai_type_matcher` implementation |
| `ai/rules_judge.py` ~1–140 | Legacy LangChain `ChatOpenAI` path (replaced by direct OpenAI SDK) |
| `ai/image_curation.py` ~481–503 | Old single-shot multimodal curation (replaced by per-image + order flow) |
| `media/rc_linker.py` ~903+ | Duplicate commented copy of RC linker AI helpers |
| `ai/media_verify.py` | Older commented batch orchestrator (active path uses `verify_and_fill_missing_media_for_not_processed`) |

---

## Related docs

- [Architecture](./architecture.md) — full pipeline overview
- [Media verification](./media_verify.md)
- [Image curation](./image_curation.md)
- [Post selection](./post_selection.md)
- [WhatsApp ad generation](./whatsapp_ad_generation.md)
- [WordPress](./wordpress.md)
- [Special availability](./special_avail_list.md)
