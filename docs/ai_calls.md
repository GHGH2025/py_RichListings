# AI Calls Inventory

This document lists every **active** OpenAI call in the RichListings Python worker (`py_RichListings`). All calls use the OpenAI Python SDK via `client.chat.completions.create`. There are **no AI calls in the React frontend**.

**Last audited:** 2026-06-23

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

---

## By pipeline stage

### 1. Ingestion & extraction

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 1 | `extract_listings_from_email_html` | `listingDetails.py` | `OPENAI_MODEL` → `gpt-4.1-mini` | Email parse job | Parse wholesaler email HTML into structured listing JSON (structured output; falls back to JSON mode on error) |
| 2 | `ai_address_search_keys` | `ai_address_search_keys.py` | `OPENAI_MODEL` → `gpt-5.1` | After listing save (`update_parsed_listing_address_keys`) | Generate address search-key variants for dedup / WP lookup |
| 3 | `ai_verify_media_for_listing` | `ai_media_verify.py` | `OPENAI_MODEL` → `gpt-4.1` | Scheduled: `verify_and_fill_missing_media_for_not_processed` (every 2 min, limit 35) | Re-scan email HTML to recover missing images / gallery links |

### 2. Qualification & rules

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 4 | `judge_listing_with_english_rules` | `ai_nl_rules_judge.py` | `OPENAI_MODEL` → `gpt-5` | Scheduled: `apply_ai_english_rules` (every 5 min, limit 50) | Pass/skip listing against YAML business rules |
| 5 | `_ai_city_in_do_not_post` | `post_selection.py` | `gpt-4o-mini` (hardcoded) | During post selection (cached per city) | Fuzzy-match listing city against do-not-post city list |

### 3. Image curation

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 6 | `classify_single_image` | `image_curation.py` | `OPENAI_VISION_MODEL` → `gpt-4.1-mini` | Per image during curation | Vision: keep/skip non-property images (logos, flyers, etc.) |
| 7 | `order_property_images` | `image_curation.py` | `OPENAI_VISION_MODEL` → `gpt-4.1-mini` | Once per listing after filtering | Vision: rank kept photos; pick best cover image |
| 8 | `classify_primary_image` | `image_curation.py` | Caller-provided (scheduler uses `gpt-5.1`) + `gpt-5-mini` | Scheduled: `process_primary_image_verification` (every 2 min, limit 5) | Stricter dual-model primary-image gate before posting |

**Volume note:** For a listing with *N* images, curation typically makes **N + 1** AI calls (N classifications + 1 ordering). Primary verification adds **2** more calls per listing (main model + `gpt-5-mini`).

### 4. Content generation (WhatsApp & WordPress)

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 9 | `_compose_post` | `ai_make_whatsapp_posts.py` | `OPENAI_MODEL` → `gpt-4o-mini` | Scheduled: `make_whatsapp_posts_from_ready_to_post` (every 2 min, limit 5) | Generate WhatsApp ad copy from listing + rules file |
| 10 | `ai_build_wp_payload_catalog_first` | `wp_ai_mapper_catalog_first.py` | `OPENAI_MODEL` → `gpt-4.1-mini` | Scheduled: `ai_build_wp_payload_for_posted` (every 3 min, limit 5) | Map listing to WP taxonomy (region, country deals, property type) |
| 11 | `ai_build_wp_property_description_for_listing` | `wp_ai_property_description.py` | `OPENAI_MODEL` → `gpt-4.1` | Scheduled: `ai_build_wp_property_description_for_posted` (every 3 min, limit 5) | Generate HTML property description for WordPress |

### 5. WordPress maintenance & linking

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 12 | `ai_verify_same_listing` | `wp_price_red_pic_links.py` | `gpt-4.1-mini` | Scheduled: `process_wp_price_and_media_updates` (every 2 min, limit 5) | Confirm WP post address matches our listing key (price/reduction updates) |

### 6. RingCentral media linking

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 13 | `ai_extract` | `rc_media_linker.py` | `OPENAI_MODEL` → `gpt-4o-mini` | RC webhook / `process_direct_wholeseller_batch` (every 3 min) | Extract photo URL, street number, address from buyer/seller SMS dialog |
| 14 | `ai_verify_same` | `rc_media_linker.py` | `gpt-4o-mini` | During RC media link flow | Verify extracted address matches WP record |
| 15 | `_ai_guard_media_url` | `rc_media_linker.py` | `OPENAI_MODEL` → `gpt-4o-mini` | When URL passes host/MIME heuristics but needs context check | Accept/reject URL as property media based on recent dialog |

### 7. Special availability (Podio / Google Sheets)

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 16 | `ai_match_active_to_unique` | `special_avails.py` | `OPENAI_MATCH_MODEL` → `gpt-4.1-mini` | Scheduled: `process_one_special_avail_with_active_listings` (every 3 min) | Match Podio active address to deduped candidate list |
| 17 | `ai_match_address_in_sheet` | `special_avails.py` | `MANNY_MATCH_MODEL` → `gpt-5-mini` | Scheduled: `process_one_special_avail_matching` (every 5 min) | Fuzzy-match Podio address inside wholesaler Google Sheet text |

### 8. Buyer matching & notifications

| # | Function | File | Default model | Trigger | Purpose |
|---|----------|------|---------------|---------|---------|
| 18 | `call_ai_type_matcher` | `buyer_matching_api.py` | `MATCHER_MODEL` → `gpt-4o-mini` | `POST /match` + buyer matching cron | Semantic property-type match (buyer `selected_types` vs listing) |
| 19 | `call_ai_matcher` | `buyer_matching_api.py` | `MATCHER_MODEL` → `gpt-4o-mini` | `POST /match` + buyer matching cron | Classify special preference labels as PRESENT / ABSENT / UNKNOWN |
| 20 | `ai_build_buyer_sms_description_for_listing` | `matched_buyers_process.py` | `DEFAULT_BUYER_DESC_MODEL` → `gpt-4.1` | Scheduled: `process_pending_buyer_descriptions` (every 5 min, limit 5) | Short SMS body for matched buyer notification |
| 21 | `ai_build_buyer_email_description_for_listing` | `matched_buyers_process.py` | `DEFAULT_BUYER_DESC_MODEL` → `gpt-4.1` | Same job as above (2 calls per listing) | Short email body for matched buyer notification |

**Volume note:** Buyer matching batches candidates (`MATCHER_AI_BATCH_SIZE`, default **8**), so one `/match` run can make **multiple** type-matcher and preference-matcher calls. Description generation makes **2** calls per matched listing (SMS + email).

---

## Scheduled jobs that invoke AI

From `server_runner.py`:

| Interval | Job | AI function(s) |
|----------|-----|------------------|
| Every 1 min | Email → listing parse | `extract_listings_from_email_html`, `ai_address_search_keys` |
| Every 2 min | Media verify | `ai_verify_media_for_listing` |
| Every 2 min | Image curation | `classify_single_image`, `order_property_images` |
| Every 2 min | Primary image check | `classify_primary_image` × 2 models |
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

| Variable | Used by |
|----------|---------|
| `OPENAI_API_KEY` | All modules (required) |
| `OPENAI_MODEL` | Most modules (defaults vary by file) |
| `OPENAI_VISION_MODEL` | `image_curation.py` |
| `OPENAI_MATCH_MODEL` | `special_avails.py` → `ai_match_active_to_unique` |
| `MATCHER_MODEL` | `buyer_matching_api.py` |
| `MATCHER_AI_BATCH_SIZE` | Buyer matching batch size (default 8) |

---

## Commented / inactive AI code

These exist in the repo but are **not** executed in the current flow:

| Location | Notes |
|----------|-------|
| `buyer_matching_api.py` ~709–744 | Older commented `call_ai_type_matcher` implementation |
| `ai_nl_rules_judge.py` ~1–140 | Legacy LangChain `ChatOpenAI` path (replaced by direct OpenAI SDK) |
| `image_curation.py` ~481–503 | Old single-shot multimodal curation (replaced by per-image + order flow) |
| `rc_media_linker.py` ~903+ | Duplicate commented copy of RC linker AI helpers |
| `ai_media_verify.py` | Older commented batch orchestrator (active path uses `verify_and_fill_missing_media_for_not_processed`) |

---

## Related docs

- [Architecture](./architecture.md) — full pipeline overview
- [Media verification](./media_verify.md)
- [Image curation](./image_curation.md)
- [Post selection](./post_selection.md)
- [WhatsApp ad generation](./whatsapp_ad_generation.md)
- [WordPress](./wordpress.md)
- [Special availability](./special_avail_list.md)
