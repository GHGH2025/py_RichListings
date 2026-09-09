# RichListings architecture and edge-case review

**Review date:** 2026-09-07  
**Scope:** `py_RichListings` at commit `f7bf203`, with emphasis on Gmail, inbound WhatsApp, link/media discovery, image curation, Dropbox, WhatsApp, WordPress, Podio, deduplication, orchestration, security, and testability. The separate Node WhatsApp gateway and separate scraper implementation were treated as external systems; their Python-side contracts were reviewed, but their internals were not audited here.

## Executive conclusion

The current application contains a useful working prototype and several good safeguards, but it is not yet safe to treat as an unattended, lossless, exactly-once publishing system.

The biggest problem is not the AI prompts. It is workflow reliability. One Mongo document carries many loosely related statuses, jobs poll and mutate it in place, external side effects do not use durable outboxes or idempotency keys, and `status="posted"` means only that WhatsApp copy was generated. It does **not** mean that WhatsApp, WordPress, or Podio actually received the deal. This makes retry behavior, deduplication, source-email completion, and reporting inaccurate.

The highest-priority risks are:

1. Arbitrary links from untrusted messages are fetched and sometimes opened in Chromium without SSRF controls, response-size limits, or redirect validation.
2. Administrative and expensive task endpoints appear to have no authentication or authorization.
3. Extraction failures can be converted into an empty successful result, after which the source email is marked processed. Per-listing save failures are also swallowed. This can silently lose deals.
4. WhatsApp, WordPress, and webhook sends have ambiguous retry windows and no end-to-end idempotency contract. A timeout can create duplicates, while partial recipient success can be incorrectly recorded as total success.
5. All scheduled work runs on one scheduler thread. A slow OpenAI, Dropbox, Google, Podio, WordPress, or page-rendering call can delay every other job.
6. Deduplication has a race: two new copies of the same property can both pass before either becomes `posted`.
7. The 35% quota calculation does not subtract rest-of-Florida deals already admitted earlier that day, and its base counter can be double-counted after a crash or concurrent run.
8. Media filtering has bypasses and stale-folder risks, including a direct Dropbox-file path that does not pass `curate_media`, and address-based folders that retain old files.
9. Model status choices and runtime values have drifted (`sending`, `des_generated`, and `already_found` are written but are not allowed by the model definitions).
10. There is no dependable automated regression suite, dependency versions are unpinned, and no CI configuration is present.

My recommendation is to stabilize the existing application first, then migrate toward a durable workflow with immutable inputs, canonical deal identities, deterministic validation, a sandboxed media worker, and one outbox delivery record per destination and recipient.

## What the system actually does today

The code path is approximately:

```text
Gmail ───────────────┐
                     ├─> FilteredListingEmail
WhatsApp Mongo inbox ┘          |
                                v
                         OpenAI extraction
                                |
                                v
                          ParsedListing
                                |
          media discovery / S3 mirroring / 30-day dedup
                                |
                     AI business-rule decision
                                |
             region, city, quota, Dropbox gallery
                                |
                  image classification and ordering
                                |
                      WhatsApp copy generation
                                |
               ParsedListing.status = "posted"
                    /              |              \
                   v               v               v
            WhatsApp queue    WordPress jobs   Podio webhook/linking
```

Important differences from the stated goal:

- “If the deal contains any link, click it and inspect the HTML” is **not** the general behavior. Inbound WhatsApp eagerly fetches links only for a group whose name contains `jg equity direct deals` (`ingestion/whatsapp.py:294-332`). For other WhatsApp and Gmail deals, the LLM must first select one link as `other_images_source`; generic page links are explicitly discouraged by the extraction prompt (`pipeline/listing_details.py:196-228`).
- A listing can proceed with no images. Media verification marks every successfully handled record `verified` even if neither an image nor a gallery was found (`ai/media_verify.py:470-501`), and image curation sends an empty-image listing to `ready_to_post` (`ai/image_curation.py:629-640`).
- `status="posted"` is set before any channel delivers anything (`ai/whatsapp_posts.py:219-245`).
- Podio property creation is not performed directly by this application. The documented primary path is a best-effort posted-listing webhook to Podio Workflow Automation, plus separate wholesaler linking.
- Web-scraped records default to `web_publish_enabled=False`, and publication queries filter them out. No setter for that flag exists in this Python repository, so they require an external/manual database action to publish.

## What is already good

These are solid foundations worth keeping:

- Gmail messages have a unique `(account_label, gmail_message_id)` index, and parsed rows have an ingestion-level unique key (`models/__init__.py:28-36`, `101-113`).
- Gmail and WhatsApp workers use compare-and-set style claims for their immediate input records (`pipeline/process_email.py:108-115`, `ingestion/whatsapp.py:355-363`).
- There is an explicit 30-day rule and a separate 6% price-drop path with audit fields.
- The WhatsApp copy has a final URL sanitizer, not just a prompt instruction (`whatsapp/link_guard.py`).
- Image curation fails closed on per-image vision errors, and filename filters supplement vision classification.
- Web-scraped listings are publication-disabled by default.
- HTTP calls usually have a timeout, and the runner catches job-level exceptions instead of crashing the whole process.
- Pipeline metrics and AI usage tracking show good intent toward observability.
- Source-specific metadata (`input_source`, `source_website`) is retained.

Those safeguards reduce some mistakes, but they do not yet form a coherent delivery guarantee.

## Prioritized findings

| ID | Priority | Finding | Likely result |
|---|---:|---|---|
| F-01 | P0 | Untrusted URL fetching has no network egress policy | SSRF, cloud-metadata access, internal-service probing, bandwidth/memory exhaustion |
| F-02 | P0 | Admin/task/API routes appear unauthenticated | Unauthorized sends, configuration changes, data mutation, OpenAI/Dropbox cost abuse |
| F-03 | P0 | Extraction and per-listing save failures can look successful | Deals silently disappear from the pipeline |
| F-04 | P0 | No durable/idempotent delivery state per channel and recipient | Duplicate publications or partial delivery recorded as success |
| F-05 | P0 | Single synchronous scheduler thread runs long network jobs | Queue starvation and unpredictable latency |
| F-06 | P1 | Dedup is a check-then-act query against only prior `posted` records | Concurrent duplicate deals both pass |
| F-07 | P1 | `posted` means “copy generated,” not delivered | False completion, wrong dedup history, premature Gmail forwarding |
| F-08 | P1 | Media pipeline has curation bypasses, stale folders, and no byte limits | Bad media published; resource exhaustion; old photos leak into new galleries |
| F-09 | P1 | Daily 35% quota accounting is not correct or atomic | Quota can be exceeded or good deals can be incorrectly skipped |
| F-10 | P1 | Model enums and runtime states disagree | Invalid records, validation errors, stuck jobs, misleading queries |
| F-11 | P1 | Gmail cursor is a local timestamp file with no safety overlap | Missed mail at boundaries, host replacement/state corruption problems |
| F-12 | P1 | AI is used for deterministic business rules without deterministic validation | Boundary mistakes and non-repeatable pass/skip decisions |
| F-13 | P1 | Address/list-index identities are unstable | Wrong dedup, wrong WordPress/Podio match, reparse overwrites the wrong listing |
| F-14 | P1 | Retry/dead-letter/attempt metadata is inconsistent | Infinite hot retries, permanently stuck work, weak operations |
| F-15 | P1 | Test endpoint force-passes gates and performs real Dropbox writes | False confidence plus production side effects from a “dry run” |
| F-16 | P2 | HTML, prompts, delivery payloads, phone numbers, and model outputs are printed | PII leakage and very noisy logs |
| F-17 | P2 | Dependencies are unpinned and CI/test infrastructure is absent | Non-reproducible deployments and undetected regressions |
| F-18 | P2 | Documentation materially disagrees with code | Operators act on incorrect status and dedup assumptions |

### F-01 — Link processing is an unsafe network boundary

Evidence:

- `requests.get(... allow_redirects=True)` is called on URLs taken from inbound text in `ingestion/whatsapp.py:141-162`.
- Generic gallery URLs and every discovered image are fetched in `media/dropbox_upload.py:271-349`.
- Playwright opens the destination when static HTML yields no images in `media/scrape_images.py:116-157`.
- Redirect targets are not revalidated. There is no block for loopback, RFC1918, link-local, IPv6 local ranges, `169.254.169.254`, internal DNS, or DNS rebinding.
- Downloads have no maximum bytes. Dropbox ZIPs are read fully into memory (`media/dropbox_upload.py:91-109`), individual ZIP entries are read fully, and Drive files are downloaded with no timeout or size cap (`media/dropbox_upload.py:595-629`).
- Remote filenames are joined to local paths without one consistent basename/safe-name policy (`media/dropbox_upload.py:170-178`, `617-621`). Malicious separators or traversal-like names can escape the intended temporary directory or cause cross-job collisions.

Impact:

- A seller, compromised account, or unauthenticated task caller can make the worker request internal services or cloud instance metadata.
- A large response, endless stream, decompression bomb, huge image, or large number of media candidates can exhaust memory, disk, time, OpenAI budget, or Dropbox storage.
- Browser navigation expands the attack surface beyond an ordinary HTTP client.

Required fix:

- Put URL retrieval in an isolated media-fetch service/container with no credentials and an explicit outbound proxy policy.
- Allow only `http` and `https`; reject URLs with credentials; normalize IDNs; resolve DNS; block private/reserved/link-local/loopback/multicast ranges for every redirect; pin the resolved IP for the connection or use a hardened egress proxy.
- Limit redirects, connect/read/total time, response bytes, HTML bytes, ZIP compressed and expanded bytes, entry count, recursion depth, candidate count, image pixels, and per-deal media cost.
- Revalidate every discovered media URL independently.
- Run Chromium without access to the application network, Mongo, metadata, or secrets.

### F-02 — The API is an unprotected control plane

`api_app.py` binds to `0.0.0.0` and includes CRUD, task, configuration, proxy, buyer, and webhook routers. No global auth dependency or middleware is visible (`api_app.py:93-129`). Particularly risky routes include:

- changing WhatsApp send mode (`api_app.py:163-171`);
- executing special-availability jobs;
- running the test pipeline on caller-provided HTML, including real Dropbox work (`api_app.py:212-251`);
- launching a live catch-up that reaches WordPress, WhatsApp, and webhooks (`api_app.py:254-299`);
- CRUD/import of sender and policy configuration;
- `/public/wp/create`, which accepts a caller-supplied WordPress token and proxies a mutation.

CORS is not an access-control mechanism. Restrict the service at the network layer and add authenticated principals with role-based permissions. Separate public buyer/webhook endpoints from the administrative control plane. Verify webhook signatures, add replay windows, rate limits, body-size limits, audit logs, and CSRF protection where cookie auth is used.

### F-03 — The pipeline can silently acknowledge extraction failure

`extract_listings_from_email_html()` catches both model attempts and returns `{"listings": [], "notes": ["extraction_failed...", ...]}` instead of raising (`pipeline/listing_details.py:361-396`). `upsert_parsed_listings_from_html()` catches each listing save error and prints it (`pipeline/listing_details.py:497-658`). `process_pending()` then marks the email processed regardless of count or failure notes (`pipeline/process_email.py:128-140`).

This creates three silent-loss cases:

1. OpenAI is unavailable or returns an invalid response: zero listings, source marked processed.
2. The model extracts listings but database/geocoding logic fails for all rows: zero saved, source marked processed.
3. Some of N listings fail to save: source marked processed with only a partial set.

Required behavior:

- Treat transport/model/schema failures as retryable extraction failures.
- Distinguish valid `no_deal_found` from `extract_failed` and `partial_persist`.
- Persist an extraction attempt record with input hash, prompt version, model, response hash, parsed count, saved count, errors, and retry class.
- Mark the source complete only after expected candidates are durably stored or a deliberate human/auditable no-deal decision is made.
- Send poison inputs to a dead-letter/manual-review queue after bounded retries.

### F-04/F-07 — Publication state is not a delivery state

At `ai/whatsapp_posts.py:219-245`, generating copy sets:

```text
status = posted
whatsapp_status = pending
wp_status = ready_to_process
```

It then sends a best-effort webhook. Therefore `posted` is really `content_ready` or `publication_requested`.

Consequences:

- Dedup uses this record as posted history even if all destinations fail (`pipeline/dedup.py:10-14`).
- source Gmail forwarding treats it as terminal (`ingestion/forward_completed.py:584-610`).
- scraper outcomes are marked posted (`pipeline/scrape_ingest.py:355-383`).
- metrics overstate successful delivery.

The solution is not another status value on `ParsedListing`. Create separate delivery records:

```text
PublicationIntent(deal_version_id, destination, recipient, payload_hash,
                  idempotency_key, state, attempts, next_attempt_at,
                  provider_id, last_error, acknowledged_at, reconciled_at)
```

A deal is `publication_complete` only according to an explicit policy, for example:

- required: WordPress acknowledged;
- required: Podio acknowledged/reconciled;
- required: at least one configured WhatsApp group acknowledged;
- optional: secondary WhatsApp DMs.

Keep per-recipient outcomes. Today DM delivery returns success when **any** number succeeds (`whatsapp/sender.py:133-157`) and then marks the listing sent. Group parsing checks only the first result entry (`whatsapp/sender.py:94-117`). Failed recipients are lost.

### F-05 — One slow job blocks the entire schedule

The `schedule` loop calls all jobs on the main thread (`server_runner.py:353-372`). Jobs perform synchronous calls and loops. Examples include:

- an OpenAI client timeout of 800 seconds (`pipeline/listing_details.py:35-39`, `ai/rules_judge.py:155-159`);
- up to 200 post-selection candidates, each potentially invoking AI and media downloads;
- sequential per-image vision calls plus an ordering call;
- Dropbox/Drive/Playwright requests;
- explicit sleeps in the WhatsApp queue.

The per-job lock in `safe_scheduled_job` does not solve this. Scheduled jobs do not overlap on the one scheduler thread anyway, and the lock does not coordinate manual API background tasks or a second process.

Use durable queues with separate worker pools and concurrency limits by resource:

- ingestion;
- extraction/normalization;
- rules/dedup;
- media fetch/vision;
- each publisher.

Every work item should use an atomic lease with `lease_owner`, `lease_expires_at`, attempt count, and heartbeat. A slow media job must not stop Gmail polling or delivery retries.

### F-06/F-13 — Deduplication and identity are not atomic

Dedup searches only records already in `status="posted"` (`pipeline/dedup.py:113-148`). Two copies of the same property arriving together are both `verified`; neither sees the other, so both advance.

Identity issues include:

- A parsed email listing is identified by its model-produced ordinal `list_index`. If re-extraction changes order, an existing row can be overwritten with a different property (`pipeline/listing_details.py:563-642`). Extra rows from an earlier extraction are not retired.
- Exact address matching is sensitive to units, abbreviations, masked house numbers, typos, and geocoder mistakes.
- A broad or incorrect Google result may join different properties. Conversely, condos at one building need unit-aware identities.
- Scraper `RawListing` has both globally unique `listing_id` and `(source, listing_id)` indexes, so two providers using the same ID collide (`models/scraper_listings.py:9-15`). `FilteredListing` has no source/listing unique index.

Recommended identity hierarchy:

1. source identity: `(source, source_account, source_message_id, source_item_key)`;
2. canonical property identity: normalized address plus unit, or verified parcel/APN when available;
3. deal/version identity: property + seller + asking price + material terms + source timestamp;
4. content identity: SHA-256 of normalized evidence for replay detection.

Use a unique reservation/decision record for `(canonical_property_id, policy_window)` so concurrent candidates cannot both pass. For masked/ambiguous addresses, route to review rather than forcing a confident merge.

### F-08 — Media filtering is incomplete and can be bypassed

Concrete issues:

- A single-file Dropbox link calls `upload_to_dropbox(final_local_path, dropbox_folder)` without passing `curate_media` or `listing_id` (`media/dropbox_upload.py:182-185`). That path bypasses vision curation.
- Videos are allowed in galleries but are never content-classified. Decide explicitly whether videos are allowed and how they are screened.
- Dropbox folders are derived mainly from address (`pipeline/post_selection.py:565-578`). Reusing an address reuses a folder. Old files that are not overwritten remain visible, so a later deal can inherit stale or previously rejected media.
- `handle_Link()` deduplicates return values with `set`, losing order (`media/dropbox_upload.py:729`).
- An HTML page with any static candidates never gets rendered (`media/scrape_images.py:184-188`). If the static candidates are only a logo or Open Graph brand image, property photos injected by JavaScript are missed.
- CSS backgrounds, `<picture><source>`, JSON-LD, API-fed galleries, authentication/cookie pages, HEIC/AVIF, and signed-URL expiry are not covered.
- Vision ordering output is trusted as a list and not verified to be an exact subset/permutation of approved URLs (`ai/image_curation.py:389-417`, `542-565`).
- `images_s3` is populated from the mirror result even when mirroring failed and the result is still an external URL (`ai/media_verify.py:138-160`). Its name therefore overstates provenance.
- Initial S3 mirroring occurs before image curation, so logos/headshots can be copied into S3 even if later removed.

Target behavior:

1. Discover candidates and record provenance: page URL, DOM selector/source, listing association, fetch time.
2. Fetch once through the sandbox, hash bytes, MIME-sniff, decode, enforce limits, strip dangerous metadata if desired, and store a private quarantine object.
3. Deduplicate by content hash/perceptual hash.
4. Apply deterministic filters: dimensions, aspect ratio, corruption, filename hints, exact/near duplicates.
5. Apply vision classification with explicit labels and confidence.
6. If confidence is borderline or every candidate is rejected, use manual review or a clearly defined text-only policy.
7. Publish only approved derivatives to a unique versioned folder such as `/PropertyListings/<property-id>/<deal-version-id>/`.
8. Generate the Dropbox link only after the final folder is complete. Never reuse a mutable folder as the identity of a publication.

### F-09 — The daily quota can be wrong

`rest_cap = floor(0.35 * final_base_count)` is recomputed each run, but previously admitted rest-of-Florida listings are not subtracted (`pipeline/post_selection.py:491-510`). If the base is 100, each run can admit up to 35 more rest listings. The base counter is also updated before candidates change status, using read-then-write rather than an atomic increment (`pipeline/post_selection.py:234-273`, `494-505`). A crash or concurrent catch-up can count the same non-rest candidates twice.

Model the quota as atomic daily counters or reservations:

```text
QuotaDay(day, timezone, admitted_non_rest, admitted_rest, version)
QuotaReservation(deal_id, bucket, day, state)
```

Use a transaction or atomic conditional update. Clarify the business formula: “rest must be no more than 35% of non-rest” is different from “rest must be no more than 35% of all posts.” Also decide whether overflow is deferred until later that day or permanently skipped; current code permanently marks it `skipped_quota`.

### F-10 — Status schema drift is already present

The model allows WhatsApp `pending|failed|sent`, but the sender writes `sending` (`models/__init__.py:184-186`, `whatsapp/sender.py:247-264`). The model allows WordPress `ready_to_process|keys_generated|description_generated|posted|failed`, but runtime writes `des_generated` and `already_found` (`models/__init__.py:163-165`, `integrations/wordpress/ai_property_description.py:286-288`, `integrations/wordpress/sync_poster.py:310-317`).

The same schema drift exists for media audit data: `skipped_images` is declared as a `DictField`, while curation writes a list of `{url, reason}` objects (`models/__init__.py:132-135`, `ai/image_curation.py:649-666`).

Depending on the MongoEngine update path, this can either raise during updates or store values normal model validation rejects later. Replace string literals with typed enums/constants, validate state transitions in one place, add migrations, and test every transition against the actual database mapper.

There is also no stale recovery for outbound WhatsApp `sending`. A crash after provider delivery but before the final database update leaves it forever outside the queue; blindly resetting it risks a duplicate. This requires provider idempotency/reconciliation, not just a timer.

### F-11 — Gmail cursor/checkpoint handling needs reconciliation

Gmail polling stores `last_run_epoch` in a local `accounts/<label>/state.json` and searches an exact timestamp window (`ingestion/gmail.py:210-226`, `286-307`). Risks:

- exclusive/inclusive second-boundary behavior;
- message arrival/indexing delay after the window closes;
- corrupt or partially written JSON;
- host replacement, two replicas, or state rollback;
- Gmail API partial failure or rate limiting;
- messages not in Inbox, depending on business intent;
- OAuth refresh/token-file races.

Use a durable account cursor in the database and an overlap window (for example, re-read the last 10–30 minutes) while relying on message-ID uniqueness. For higher assurance, use Gmail History API/watch semantics plus periodic reconciliation. Write cursor updates transactionally only after all page IDs in the range are durably recorded. Track gaps and last successful reconciliation.

### F-12 — Deterministic rules should not depend on an LLM verdict

Price, bed, bath, property type, area, HOA, location, and threshold rules can be represented as code or a declarative rules DSL. Today the full decision is delegated to a model (`ai/rules_judge.py:207-294`). Missing data is designed to fail open and continue, which may publish a deal that could not actually be evaluated.

Use AI for evidence extraction and ambiguous classification, then apply deterministic rules to typed facts. Every fact should include:

```text
value, unit, source_quote/span, source_message_id, confidence, extractor_version
```

Use three outcomes per rule: `PASS`, `FAIL`, `UNKNOWN`. The policy must explicitly decide whether `UNKNOWN` blocks, passes, or enters review. Do not silently treat it as pass.

Boundary truth tables must cover:

- exactly $250,000 versus over $250,000;
- exactly $600 HOA and HOA + assessment;
- 2 beds versus 2.5 or a den;
- 3/1 versus 3/1.5;
- exactly 900 sqft;
- exactly 5,000 sqft and exactly 0.12 acre;
- conflicting sqft/acre values;
- condos with water view only versus ocean access;
- teardown/redevelopment exceptions;
- missing or ambiguous region/property type.

### F-14 — Retry behavior is mostly “poll it again forever”

Some errors leave a row in the same state forever; some become terminal immediately; some write only a text reason. There is no consistent `attempts`, `next_attempt_at`, error class, maximum attempts, or dead-letter state for the main pipeline and publishers.

Adopt a shared retry policy:

- retry only transient classes (timeouts, 429, selected 5xx);
- exponential backoff with full jitter;
- honor `Retry-After`;
- cap attempts and total elapsed time;
- no automatic retry for validation, policy, authentication, or unsafe URL failures;
- dead-letter plus operator action and replay tooling;
- circuit breakers per provider;
- per-provider concurrency and budget controls.

Do not automatically retry POST unless the downstream API accepts an idempotency key. `whatsapp/sender.py` configures POST retries (`51-61`) without showing such a key.

### F-15/F-17 — The current test surface does not protect the pipeline

The only media “test” is a `main()` self-check with bare assertions, so `unittest discover` does not execute it as a test. `pipeline/test_pipeline.py` is an operational dry-run harness, not an isolated test; it force-passes rejected deals and performs real Dropbox uploads. It is exposed through an unauthenticated-looking endpoint.

The repository has:

- 98 Python files and roughly 28,368 lines;
- no `pyproject.toml`, pytest configuration, CI workflow, container definition, or lock file;
- an unpinned `requirements.txt`;
- no checked-in `.env.example` or machine-checkable configuration schema.

On the review workstation, all Python files parsed successfully with `ast`, but runtime imports and test discovery failed because dependencies such as `mongoengine` and `requests` were not installed. That is an environment limitation, not a passing test result.

Build hermetic tests with fake providers. A test must never reach live Gmail, WhatsApp, Dropbox, S3, OpenAI, WordPress, or Podio unless it is an explicitly gated integration test using a sandbox account.

### F-16/F-18 — Logs and docs are not a reliable operational interface

Examples of sensitive/noisy output:

- full Gmail `html_ai` (`ingestion/gmail.py:384-395`);
- model rule response (`ai/rules_judge.py:290-293`);
- WhatsApp gateway URL, recipient payload, copy, and image URL (`whatsapp/sender.py:133-150`, `183-195`);
- media URLs, Dropbox paths, and listing blobs in multiple workers.

Use structured logs with `trace_id`, `source_id`, `deal_id`, stage, attempt, duration, outcome, and safe error code. Redact email addresses, phone numbers, auth query parameters, signed URLs, tokens, message bodies, and property contact details. Never log full provider responses by default.

Documentation drift examples:

- `docs/dedup_30_day.md` says skipped/intermediate statuses count as history, while code uses only `posted`.
- documentation calls the WordPress stage `description_generated`, while code writes `des_generated`.
- architecture docs refer to historical filenames that are no longer the runtime modules.

Generate state diagrams and configuration references from code where possible, and make doc consistency part of CI.

## Edge-case catalogue

The following cases should become fixtures and explicit product policies. “Review” means the system should stop safely and expose the evidence to an operator, not silently discard the deal.

### Inbound Gmail

| Edge case | Current risk | Recommended behavior |
|---|---|---|
| Message arrives at the exact cursor second or is indexed late | Missed deal | Cursor overlap plus message-ID idempotency |
| State file missing/corrupt/rolled back | Miss or replay | Durable cursor, checksum, reconciliation |
| Two worker replicas poll one account | cursor race, duplicate work | distributed lease per Gmail account |
| Sender uses alias, plus-address, display-name spoofing, shared mailer domain | false allow/deny | verified sender policy using normalized envelope/header evidence; exact domain rules, not unrestricted substring |
| Allow list is empty because Mongo is unavailable/misconfigured | all senders may pass | fail closed or explicit “accept all” config; alert loudly |
| Message in Promotions/Spam/Archive but not Inbox | missed deal | explicit label policy and reconciliation query |
| Large `text/html` body stored behind Gmail `attachmentId` | empty extraction | fetch text attachment bodies with size cap |
| Forwarded `.eml`, nested `message/rfc822`, calendar/vendor MIME type | no body | recursive MIME handling and fixture tests |
| Multiple HTML parts where the largest is a disclaimer/history | wrong body | understand multipart/alternative and forwarded sections; keep all evidence with part metadata |
| Plain text contains `<`, `&`, or malformed encoding | altered content | charset-aware decoding and canonical text representation |
| Very large newsletter/history | token overflow/cost | deterministic content cap by sections; store full raw separately |
| One email contains N deals plus a global signature/gallery | cross-listing attribution | DOM/section segmentation before extraction; provenance per field/image |
| Reply chain repeats yesterday’s deals | duplicates or wrong terms | strip quoted history while retaining it as evidence; canonical deal/version identity |
| Model returns zero listings | treated as success today | distinguish no-deal, low-confidence, and extraction failure |
| Reparse returns listings in a different order | rows swapped/overwritten | stable source item key/content hash, not list index |

### Inbound WhatsApp

| Edge case | Current risk | Recommended behavior |
|---|---|---|
| Seller sends text, then photos as separate messages | separate incomplete deals | correlation window by group/sender/reply/album; operator-visible grouping |
| Photo-only message | parser lacks address/terms | attach to nearby caption/replied message or review |
| Album has caption only on first item | orphan media | group by WhatsApp album/message metadata |
| Edited message changes price/link | stale deal | ingest revisions as immutable source versions and reevaluate before publication |
| Deleted/revoked message | stale publication | record revocation; configurable hold period/retraction workflow |
| Quoted/replied message contains the deal | text omitted or duplicated | preserve reply context with provenance |
| Forwarded chain or repeated blast | duplicates | source/content identity plus canonical-property reservation |
| Reaction/system message/location/contact/document | false deal or error | explicit supported message-type allowlist |
| Group renamed | JG-specific behavior changes | use stable group ID/config, never display-name substring |
| Seller phone/JID changes or seller-email mapping is stale | wrong wholesaler | versioned identity mapping and review on mismatch |
| Bot’s own outbound message is recaptured | loop | origin marker/message ID suppression |
| One message has several properties and several links | wrong page/image association | segment into candidates; attach link via proximity/evidence |
| Link fetch partly succeeds, then one link fails | whole message becomes error despite saved children | child-level states and independent retries |
| Old message enters `processing` now | stale reset uses original timestamp | record `processing_started_at`/lease expiration |

### Link discovery and page retrieval

| Edge case | Recommended policy |
|---|---|
| `http://127.0.0.1`, `localhost`, private IPv4/IPv6, metadata IP, internal hostname | reject before request and on every redirect/DNS resolution |
| Public URL redirects to private address | reject redirect target |
| DNS rebinding | resolve through controlled proxy/pin address |
| URL contains userinfo, Unicode lookalike host, nonstandard port | normalize and enforce policy |
| Infinite/long redirect chain | hard redirect limit |
| Tracking shortener wraps unsubscribe/login link | classify link intent and final origin; do not treat as gallery automatically |
| 200 response is login page, consent page, WAF challenge, PDF, JSON, or error HTML | type/signature detection and explicit outcome |
| Signed link expires between extraction and media job | refresh from source if possible, otherwise review/retry before deadline |
| Page needs cookie/session or JavaScript | sandboxed browser with strict limits; never application credentials |
| Static HTML has only logo but JS has property photos | classify candidates before deciding render fallback |
| Photos in CSS, `<picture>`, JSON-LD, embedded state, or API response | pluggable extractors with fixture coverage |
| Pagination/infinite scroll | capped page/scroll/request budget |
| robots/terms prohibit scraping | source policy and legal review |

### Media and image classification

| Edge case | Recommended policy |
|---|---|
| Logo/headshot filename is neutral | vision/content check, not filename only |
| Property photo includes a small brokerage watermark | define allowed watermark coverage threshold |
| Flyer contains a large property photo plus price/logo/text | define reject/derive policy; preferably reject original rather than crop without approval |
| Family/tenant/person appears incidentally | privacy policy and confidence threshold |
| Street map, parcel map, survey, rendering, floor plan | explicit per-property-type allowed classes |
| Vacant land aerial looks like a generic map | require parcel/property evidence or review |
| Wrong property photo from adjacent email section | use listing association evidence, not just “is a house” |
| Duplicate bytes under different URLs/query strings | SHA-256 dedup |
| Near-duplicate/resized/cropped image | perceptual hash dedup |
| Corrupt/truncated image or MIME spoof | decode and MIME-sniff before storage/model call |
| Huge dimensions/decompression bomb | pixel and byte limits before decode |
| EXIF orientation/GPS/author metadata | normalize orientation; strip sensitive metadata from published derivative |
| HEIC/AVIF/SVG/TIFF/animated GIF | explicit supported formats and safe transcoding |
| Transparent logo saved as PNG | content classifier should reject |
| Vision provider times out | retry transiently; do not permanently reject the deal |
| All images rejected | explicit text-only versus review policy |
| First image later expires | publish owned immutable derivative, not third-party URL |
| Existing Dropbox folder contains old files | unique versioned folder or clean manifest-driven publish |
| Gallery contains video with logo/person | video classification or videos disabled |

### Parsing, normalization, and facts

| Edge case | Recommended policy |
|---|---|
| Asking price, ARV, rehab, rent, and assignment fee appear together | typed fact extraction with labeled source spans; deterministic selection |
| “Starting at,” price range, per-unit price, package price | represent semantics; do not coerce to one misleading float |
| `$250k`, `$250,000.00`, `250`, commas/locale mistakes | deterministic money parser with plausibility range |
| 2/1, 2/1.5, 2+den, studio, unknown | typed bed/bath representation and rule truth table |
| HOA is quarterly/annual or has special assessment | normalize period; preserve original amount and unit |
| Lot sqft and acres disagree | conversion tolerance; review conflict |
| Address is masked (`12XX`) or missing unit | low-confidence property identity; do not geocode as exact premise |
| Condo building address with multiple units | unit is part of identity |
| Corner lot/two addresses/APN only | aliases plus parcel identity |
| City crosses counties or mailing city differs | geocoder evidence and confidence; no LLM-only county guess |
| Google returns route/locality rather than premise | reject as canonical exact property match |
| Source says a non-Florida property | deterministic jurisdiction gate |
| Prompt-injection text inside email/page | treat content as data, validate every result, never allow it to influence tools/policy |

### Deduplication and deal versions

| Edge case | Product decision required |
|---|---|
| Same property from two wholesalers | dedup globally, prefer one, or publish seller-specific versions? |
| Same address but different condo/unit | never merge without unit evidence |
| Multi-family units at same address | one property deal or several unit deals? |
| Bundle includes several addresses | split, keep portfolio identity, or review |
| Price drop is exactly 6% | code currently passes; preserve as explicit boundary test |
| Prior asking price was wrong | allow correction workflow without triggering false price drop |
| Price rises, then drops 6% from the inflated price | compare against last valid published price, lowest price, or original? |
| Material terms change without 6% drop | decide whether it is a new publishable version |
| Prior “posted” deal never delivered | should not start dedup window; current code does |
| Two duplicates arrive simultaneously | atomic canonical-property reservation |
| 30-day boundary and DST | choose business timezone and exact inclusive rule |

### Rules and selection

| Edge case | Recommended behavior |
|---|---|
| Required rule fact missing | `UNKNOWN` and review/explicit policy, not implicit pass |
| Rule config fails to load | fail closed for publication and alert; current do-not-post list fails open |
| Do-not-post list changes during process lifetime | versioned config reload/invalidation |
| AI city matcher fails | deterministic alias table/geocoder; do not silently publish if prohibition is safety-critical |
| City spelling maps to two places | state/county-aware matching |
| Quota changes after overflow deal was skipped | defer queue and reevaluate if intended |
| Manual override | require actor, reason, before/after decision, expiry, and audit log |
| Rules change while deal is midway | pin `policy_version` per decision and define reevaluation policy |

### Delivery and cross-system consistency

| Edge case | Recommended behavior |
|---|---|
| Provider commits send but response times out | reconcile by idempotency key/provider message ID before retry |
| One of five WhatsApp recipients fails | per-recipient record; retry only failures |
| One of several group results fails | inspect every result, not first only |
| Node gateway accepted but WhatsApp later rejects | distinguish accepted, sent, delivered, read, failed if callbacks exist |
| WordPress search returns another similar address | match canonical external ID, not first fuzzy result |
| WordPress create succeeds but response is lost | idempotency key and reconciliation lookup |
| Podio webhook returns 2xx but flow later fails | callback/reconciliation against Podio item |
| WordPress succeeds, Podio fails, WhatsApp succeeds | visible partial state and targeted retry; never collapse to one `posted` status |
| Content changes after one channel publishes | immutable publication version and update workflow |
| Dropbox link permissions change | link health reconciliation before/after publication |
| Deal is withdrawn after publication | retraction/private/update workflow for every channel |
| Destination credentials expire | circuit breaker, alert, no hot retry |

## Clean-slate architecture

### Design principles

1. Preserve raw evidence immutably.
2. Make every stage idempotent and replayable.
3. Separate “deal decision” from “delivery to a destination.”
4. Use deterministic code for deterministic policy.
5. Treat AI output as untrusted proposed facts/classifications.
6. Treat URLs and files as hostile.
7. Represent ambiguity explicitly; do not force binary confidence.
8. Use owned/versioned media for publication.
9. Make partial success visible and recoverable.
10. Require a durable audit trail for automatic and manual decisions.

### Proposed component flow

```text
                          ┌──────────────────────────┐
Gmail watch/poller ──────>│                          │
WhatsApp adapter ────────>│ Immutable Source Inbox  │
Web scraper adapter ─────>│ + deduped source events │
                          └────────────┬─────────────┘
                                       v
                          ┌──────────────────────────┐
                          │ Correlate and segment    │
                          │ messages into candidates│
                          └────────────┬─────────────┘
                                       v
                          ┌──────────────────────────┐
                          │ Extract proposed facts   │
                          │ + evidence + confidence  │
                          └────────────┬─────────────┘
                                       v
                          ┌──────────────────────────┐
                          │ Normalize and validate   │
                          │ address/money/units/etc. │
                          └────────────┬─────────────┘
                                       v
                    ┌──────────────────┴─────────────────┐
                    v                                    v
       ┌────────────────────────┐          ┌────────────────────────┐
       │ Sandboxed media worker │          │ Canonical identity +   │
       │ discover/fetch/classify│          │ atomic dedup reservation│
       └────────────┬───────────┘          └────────────┬───────────┘
                    └──────────────────┬─────────────────┘
                                       v
                          ┌──────────────────────────┐
                          │ Deterministic rule engine│
                          │ PASS / FAIL / REVIEW     │
                          └────────────┬─────────────┘
                                       v
                          ┌──────────────────────────┐
                          │ Publication plan/version │
                          └────────────┬─────────────┘
                                       v
                          ┌──────────────────────────┐
                          │ Transactional outbox     │
                          └──────┬────────┬──────────┘
                                 v        v          v
                           WhatsApp   WordPress    Podio
                              |           |          |
                              └──── acknowledgements─┘
                                       |
                                       v
                               Reconciler + alerts
```

### Suggested records

#### `SourceEnvelope`

- `id`, `source_type`, `source_account`, `source_message_id`, `source_revision`
- `received_at`, `source_created_at`, `sender_identity`, `conversation/thread/group`
- immutable raw body/media references and `content_hash`
- normalized headers and source metadata
- ingestion cursor/batch ID
- correlation status and audit timestamps

Unique key: `(source_type, source_account, source_message_id, source_revision)`.

#### `DealCandidate`

- stable candidate ID independent of model array order
- source-envelope IDs and source section/span
- extracted fact proposals with evidence/confidence
- extraction model, prompt, schema, and code versions
- normalization outcome and validation errors

#### `PropertyIdentity`

- normalized address components including unit
- verified geocoder fields and precision
- parcel/APN where available
- aliases and confidence
- no exact canonical identity when the address is masked/ambiguous

#### `DealVersion`

- property identity, seller identity, asking price/terms
- evidence set and content hash
- `policy_version`, decision, reason codes, unknown facts
- prior version link and price-change calculation
- immutable approved-media manifest

#### `MediaAsset`

- source URL (encrypted/redacted in logs), final URL, fetch provenance
- byte hash, perceptual hash, MIME, dimensions, size
- quarantine object key and approved derivative key
- classifier labels/confidence/version and reviewer action
- association confidence to a particular deal candidate

#### `PublicationIntent`

- deal version, destination, recipient/site/workspace
- immutable payload and payload hash
- deterministic idempotency key
- state: `pending|leased|accepted|confirmed|failed_retryable|failed_terminal|cancelled`
- attempts, lease, next retry, provider response/message/item ID
- reconciliation timestamps and errors

#### `AuditEvent`

Append-only event with actor/service, action, object, old/new state, reason, trace ID, and timestamp. Do not rely on mutable `updated_at` as history.

### State model

Use separate state machines rather than one giant status:

```text
Source:       RECEIVED -> CORRELATED -> EXTRACTED -> COMPLETE
                                      \-> RETRY / DEAD_LETTER / NO_DEAL

Candidate:    EXTRACTED -> NORMALIZED -> IDENTITY_RESOLVED
                         \-> REVIEW / REJECTED

Decision:     PENDING -> PASS / FAIL / REVIEW

Media:        DISCOVERED -> FETCHED -> QUARANTINED -> APPROVED / REJECTED

Publication:  PENDING -> LEASED -> ACCEPTED -> CONFIRMED
                                \-> RETRYABLE / TERMINAL / CANCELLED
```

Do not use a terminal decision status to represent external delivery.

### Deterministic rule engine

Represent rules as versioned structured configuration rather than English-only text. Example:

```yaml
id: R1
when:
  all:
    - bedrooms: {eq: 2}
    - price_usd: {gt: 250000}
    - region: {eq: south_florida_tri_county}
unless:
  is_on_water: {eq: true}
on_unknown: review
result: fail
```

The UI/report should show exactly which fact and evidence caused a decision. An LLM may propose `is_on_water`, but deterministic validation and confidence policy determine whether that fact is usable.

### URL/media service boundary

The media service should receive only a URL and correlation ID—not database, Dropbox, AWS, OpenAI, Gmail, Podio, or WordPress credentials. It returns a manifest of safe fetched objects. Another worker classifies quarantine objects, and a publisher copies only approved derivatives into the final store.

For Dropbox, build a temporary versioned folder, verify its manifest, then publish/share it. This prevents incomplete galleries from being visible during upload and eliminates stale-file inheritance.

### Delivery guarantees

Exactly-once delivery cannot be guaranteed solely by this worker when providers do not support idempotency. Aim for:

- exactly-once **intent creation** in the local database;
- at-least-once delivery attempts;
- downstream idempotency key where supported;
- provider-ID reconciliation where available;
- human-visible ambiguous state when the provider outcome cannot be proven.

Never translate an ambiguous timeout directly into either success or an immediate blind retry.

## Testing strategy

### Unit tests

- Gmail MIME trees and cursor windows.
- WhatsApp correlation/grouping and every message type.
- URL normalization, redirect/IP policy, and malicious URL corpus.
- money, address, unit, bed/bath, HOA, acreage, and region normalization.
- every rule boundary and `UNKNOWN` branch.
- canonical identity and dedup windows, including units/masked addresses.
- image candidate extraction from stored HTML fixtures.
- status-transition validation and retry classification.
- WhatsApp URL redaction and payload creation.

### Contract tests

Record sanitized provider fixtures for:

- Gmail API message/list/history responses;
- Node WhatsApp inbound schema and send responses, including partial results;
- Dropbox, S3, Google address/geocode;
- WordPress create/search/update;
- Podio API and Workflow Automation callbacks;
- OpenAI structured outputs and malformed/refusal cases.

Python and Node must share a versioned JSON Schema for `whatsapp_tracked_messages`; do not duplicate it informally in two ODMs.

### Integration tests

Run disposable Mongo and fake HTTP providers. Verify crash points after every transition:

1. before claim;
2. after claim;
3. after external provider accepted;
4. before local acknowledgement;
5. after partial recipient results;
6. during retry/reconciliation.

The invariant is that a replay neither loses a deal nor creates a second publication intent.

### Golden-set/evaluation tests

Create a redacted corpus of real historical emails, WhatsApp messages, and gallery pages with human labels:

- expected candidates and property association;
- expected typed facts/evidence;
- pass/fail/review decision;
- accepted/rejected image labels and best primary image;
- expected ad fields.

Measure precision/recall and false-publication rate per sender/source and extractor version. Promote a new prompt/model only if it passes thresholds and shadow comparison.

### Security tests

- SSRF payloads including redirect chains, decimal/hex IPs, IPv6, userinfo, IDNs, and DNS rebinding simulation.
- ZIP bombs, path traversal filenames, huge content length, chunked endless response, decompression bombs, SVG/script payloads.
- unauthenticated/unauthorized route tests and webhook replay/signature tests.
- log-redaction tests for tokens, signed query strings, phone numbers, and emails.

## Observability and operational objectives

Suggested service-level indicators:

- ingestion lag by source/account;
- oldest age and count per queue/state;
- extraction success, no-deal, partial, retry, dead-letter rates;
- percent of candidates entering manual review;
- dedup decisions and suspected duplicate escapes;
- image discovery/approval/no-image rate by sender/host;
- per-provider attempt, success, ambiguous, terminal-failure, and latency rates;
- end-to-end time from source receipt to required destinations confirmed;
- publication completeness: all required destinations confirmed;
- AI token/cost and media bytes per deal/source;
- quota reservations and daily admitted counts.

Alerts should be symptom-based: cursor stale, queue oldest-age breach, dead letters, auth failures, provider circuit open, ambiguous sends, publication incomplete, media-cost spike, and no deals from a historically active source.

## Migration plan

### Phase 0 — Immediate containment (1–2 days)

1. Put the FastAPI service behind network restrictions and authentication. Disable or restrict live catch-up, test-pipeline, configuration, CRUD/import, OpenAI ping, and WordPress proxy endpoints.
2. Disable generic inbound URL fetching until an SSRF/IP/redirect policy and byte caps exist. At minimum, reject all private/reserved targets and cap all downloads.
3. Stop printing full email HTML, WhatsApp payloads, phone numbers, post copy, model responses, and signed URLs.
4. Add required environment validation at startup and a readiness endpoint that checks configuration without exposing secrets.
5. Fix status enum drift and add a stale/ambiguous WhatsApp send policy.
6. Change extraction so failures/partial saves do not mark a source processed.
7. Pass `curate_media` through the Dropbox single-file path; either classify videos or disable them.

### Phase 1 — Reliability corrections (week 1)

1. Rename current `posted` semantics to `content_ready`/`publication_pending` in code and reporting.
2. Add publication-intent/outbox records per destination and WhatsApp recipient.
3. Add idempotency keys to calls/gateway contracts and store provider IDs.
4. Add attempt counts, backoff, next-attempt time, leases, and dead-letter states.
5. Move Gmail cursor to Mongo with an overlap window and reconciliation.
6. Make source extraction completion depend on durable expected/saved counts.
7. Correct quota accounting with atomic reservations and prior-rest subtraction.
8. Add a canonical identity/reservation step before dedup pass.

### Phase 2 — Accuracy and media hardening (weeks 2–3)

1. Segment source content before LLM extraction and retain evidence spans.
2. Implement typed deterministic normalizers and rules with `UNKNOWN`.
3. Add confidence-based review for ambiguous addresses, rule facts, and cross-listing media.
4. Isolate URL/browser fetching and introduce quarantine, hashes, pixel/byte limits, manifest-driven storage, and unique deal-version Dropbox folders.
5. Validate model outputs as subsets/enums/ranges; pin prompt/model/schema versions.

### Phase 3 — Worker separation and reconciliation (weeks 3–5)

1. Replace the in-process scheduler with durable queues and separate worker pools.
2. Add provider circuit breakers and per-provider concurrency/rate limits.
3. Implement WordPress, Podio, WhatsApp, Dropbox-link, and Gmail-cursor reconcilers.
4. Define withdrawal/update/retraction workflows.

### Phase 4 — Quality gate and rollout

1. Build the golden corpus and CI test suite.
2. Run the new decision path in shadow mode against production inputs without publishing.
3. Compare decisions and media manifests with the existing system and human review.
4. Canary selected senders/groups, with a rapid kill switch.
5. Migrate remaining sources only after loss, duplicate, and false-publication objectives are met.

## Recommended first backlog

In strict order:

1. **SECURITY:** authenticate/restrict API and sandbox/disable unsafe URL fetching.
2. **DATA LOSS:** make extraction failures and partial writes retryable/auditable.
3. **DELIVERY:** introduce per-destination/per-recipient outbox records and idempotency keys.
4. **STATE:** correct enum drift and stop using `posted` as content-generation state.
5. **SCHEDULING:** move long media/AI/publisher work off the single scheduler thread.
6. **DEDUP:** stable candidate/property/deal identities plus atomic reservation.
7. **MEDIA:** fix curation bypasses, limits, quarantine, and versioned Dropbox manifests.
8. **QUOTA:** transactional daily reservation accounting.
9. **RULES:** deterministic typed rule engine with `UNKNOWN`/review.
10. **QUALITY:** hermetic tests, pinned dependencies, CI, configuration schema, and doc validation.

## Product decisions that engineering should not guess

Before implementing the target system, owners should answer:

1. Must a deal have at least one approved property image, or is text-only publication allowed?
2. Are videos allowed? If so, what branding/person/content rules apply?
3. Is a small watermark permitted, and what percentage/placement is acceptable?
4. What exactly makes publication complete: all WhatsApp recipients, one group, WordPress, Podio, or a configurable subset?
5. If one destination fails, should successful destinations remain live or be rolled back?
6. Does the 30-day clock start at decision, first successful destination, or completion of all required destinations?
7. How should the same property from two different wholesalers be handled?
8. Which price is the price-drop baseline: last confirmed publication, original price, or lowest historical price?
9. Are quota-overflow deals permanently rejected or deferred for later reevaluation?
10. Which unknown/missing rule facts require manual review versus fail open/fail closed?
11. What is the hold/review period before automatic publication, especially for edits or withdrawals?
12. What retention/privacy policy applies to raw Gmail/WhatsApp content, phone numbers, contacts, source URLs, and OpenAI requests?

## Final assessment

The project should not be rewritten all at once. Its source adapters, extraction schema, media heuristics, channel clients, and metrics can be reused. The right move is to put reliable boundaries around them:

- immutable input and evidence;
- explicit `FAILED` versus `NO_DEAL` versus `REVIEW`;
- atomic identity/dedup/quota decisions;
- hostile-link isolation;
- manifest-based approved media;
- durable per-destination outboxes;
- provider reconciliation;
- deterministic rule evaluation;
- test fixtures and operational visibility.

Once those are present, prompt tuning becomes a measurable accuracy improvement rather than a workaround for workflow uncertainty.
