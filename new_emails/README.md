# New emails list

An independent sender-driven pipeline. It scans both `acct1` and `acct2` inboxes
using their existing Gmail credentials, without legacy allow/skip lists, cron
registration, or legacy Gmail cursor files. It accepts direct mail and explicit
forwarded `From:` headers. Each message becomes a durable MongoDB job.

## Run

From `py_RichListings`, install the existing `requirements.txt`, configure the
existing MongoDB, OpenAI, media, WordPress and WhatsApp integrations, and run:

```sh
python -m new_emails.worker
```

The first worker start seeds Ethan, Michelle, John and Ivan without overwriting edited
templates. The API also supports `POST /api/new-emails-list/seed-templates`.
Run one worker process per deployment to serialize publication across inboxes.
Run it as its own supervised service, alongside the existing API server.

Environment options:

| Variable | Default / purpose |
| --- | --- |
| `NEW_EMAILS_POLL_SECONDS` | `60`, delay between scans |
| `NEW_EMAILS_BATCH_SIZE` | `20`, jobs per drain |
| `NEW_EMAILS_LOOKBACK_SECONDS` | `86400`, initial inbox lookback |
| `NEW_EMAILS_OPENAI_MODEL` | Existing `OPENAI_MODEL`, otherwise `gpt-6-luna` |
| `NEW_EMAILS_PODIO_WEBHOOK_URL` | Falls back to `POSTED_LISTING_WEBHOOK_URL`; required for posted deals |
| `NEW_EMAILS_BROWSER_CHANNEL` | Empty uses Playwright Chromium; `msedge` or `chrome` uses an installed browser |

John's linked pages require JavaScript. The existing runtime requirements include
Playwright. For a Linux worker using its bundled Chromium, provision the browser
and system dependencies during deployment:

```sh
python -m playwright install --with-deps chromium
```

Browser rendering is headless and only used when John's page returns a JavaScript
shell. It waits for loaded property text and pricing before passing the rendered
HTML to the AI extractor. Failed rendering retries the job instead of extracting
from an empty app shell.

This worker performs live publication. Preview endpoints run navigation and AI
extraction but do not write listings or publish to WhatsApp, Podio or WordPress.

## Storage and boundaries

- `new_emails_list`: unique sender email, handler key, prompt and version,
  button labels, nested detail labels, navigation bounds, active flag. There is
  no per-account sender restriction.
- `new_email_jobs`: unique `(account_label, message_id)`, frozen template
  snapshot, bodies, stage results, attempts, retry time and processing lease.
- `new_email_cursors`: independent per-account successful scan timestamps.
- Existing `parsed_listings`: downstream field contract and shared posted-deal
  history. IDs use `new_email_<account>_<gmail id>`; `input_source=new_email`.
  Legacy unscoped processing/publishing queues exclude these records.
- Existing `filtered_listing_emails`: source references marked processed, with
  forwarding disabled. The new worker does not enqueue them for legacy parsing.

## Low-level design

```text
Gmail adapter -> durable job -> handler registry -> navigation strategy
                                  |                     |
                           template snapshot      inline email + linked pages
                                                        |
                                              AI extraction adapter
                                                        |
                                             sender acceptance policy
                                                        |
                                      property merge + source provenance
                                                        |
                                  media -> copied dedup -> copied YAML rules
                                                        |
                        price-drop activation -> selection -> image validation
                                                        |
                              ad copy -> durable Podio -> WordPress -> WhatsApp
```

`SenderHandler` is the strategy contract. `HandlerRegistry` resolves a handler
key; adding a handler does not change the orchestrator. `AIExtractor` and the
page fetcher are replaceable adapters, injected into `workflow.extract_properties`
for offline testing. The workflow visits the email itself, every matching button
and nested detail buttons, using breadth-first traversal and cycle detection.
It extracts all deals on each page, not only the first listing. Tracking
redirects are followed; relative links use the final destination URL.

Each sender has one dedicated file containing its prompt, selectors, default
configuration and custom scraping/filtering/transport behavior:

```text
new_emails/
  senders/
    ethan.py
    michelle.py
    john.py
    ivan.py
  registry.py       # HandlerRegistry([EthanHandler(), MichelleHandler(), ...])
  navigation.py     # Shared traversal primitives
  workflow.py       # Shared extraction/merge orchestration
  config.py         # Generic registry-driven seeding; compatibility exports
```

For future senders, add a module in `senders/`, pass its handler instance into
the constructor list in `registry.py`, and add a fixture/test. No prompt or
sender-specific branches belong in the global workflow, worker or API.
`workflow.extract_properties(..., handler_registry=...)` also accepts an
injected registry for isolated debugging. Existing handler keys and edited
database prompts are preserved by the refactor. Sender-specific configuration
validation and incomplete-card reconciliation are optional handler hooks.
See `AGENTS.md` in this package for the maintained convention.

Navigation bounds raise an error instead of silently truncating deals. HTTP
404/410 pages retain available inline cards and produce explicit warnings.
Transient errors abort extraction and retry the job. Sources from inline and
detail pages merge by normalized address/location; unknown values never erase
known facts and detail facts take precedence. Masked street addresses also use
property characteristics to reduce accidental merging.

The policy files are independent copies of `pipeline/dedup.py`,
`ai/rules_runner.py`, and `data/ai_listing_rules.yaml`, stored in this package
and `data/new_email_listing_rules.yaml`. The new dedup copy requires **at least
6%** price reduction; the legacy implementation currently accepts any positive
drop. The existing greater-than-50% review hold is preserved. Historical
comparison includes posted deals from both pipelines.

The orchestrator claims jobs atomically and uses a two-hour renewable stage
lease. Jobs retry with exponential backoff, up to five attempts, then remain
visible as failed. Extraction is checkpointed before publishing. On retry,
status-driven downstream stages revisit only records that still need work.
Podio success is persisted per listing. Its webhook uses a stable idempotency
key, but the receiver must honor it to eliminate duplicates if the worker dies
between remote success and the local acknowledgement. WhatsApp and WordPress
retain their existing integration retry semantics. Address-review and media
holds are recorded outcomes, not reasons to fabricate missing information.

## Templates

| Sender | Handler | Behavior |
| --- | --- | --- |
| `ethan.mcauliffe@exprealty.com` | `ethan_v1` | Requires Fixers, 2 to 4, 5 plus; follows category/detail links; accepts only missing or masked house numbers |
| `michelle@stellarholdingsllc.ccsend.com` | `michelle_v1` | Extracts inline deals and follows every Get More Info link; accepts full addresses |
| `john@wholesalejax.com` | `john_v1` | Follows every Click for more about [address] button, renders JavaScript detail pages and extracts their HTML |
| `ivan-equitypro.com@shared1.ccsend.com` | `ivan_v1` | Extracts inline investment facts and follows Property Details; enriches city-only cards with their street addresses |

Michelle's supplied email has seven buttons and repeated Miami / Royal Palm
Estates cards. All seven links are visited; repeated properties merge afterwards.
A read-only check found one Deerfield Beach detail page returning 404; its inline
card supplies the available facts. The working Royal Palm Estates detail page
has a newer $179,900 price than the email's $199,900. These observations are
template validation, not a claim of completed live AI extraction/publication.

Prompts are configured per sender rather than fine-tuning a model. Existing jobs
retain their prompt snapshot; new jobs receive subsequent template revisions.
Register a new strategy for genuinely different navigation or acceptance rules;
use `button_pages_v1` for standard anchor-based templates. Pages requiring
JavaScript or authentication need a dedicated transport/handler. The supplied
Michelle links expose server-rendered HTML. John uses a dedicated headless
Playwright transport for JavaScript pages. His supplied email contains five
primary fix-and-flip cards and a separate PadSplit section. The template targets
the five primary cards, excluding the unrelated CLICK HERE PadSplit buttons.
All five linked detail pages were verified to load with property text and cash
prices using headless rendering; no live AI extraction or publication was run.

Ivan's handler supports both a single-property email and the eight-card digest
in the supplied Downtown Sanford / Hunters Creek / Riverview email. Both formats
use the same `ivan.py` and stable `ivan_v1` handler key, now with prompt version 2.
Every Property Details link is attempted; 404/410 responses and EquityPro's
"this one is no longer available" notice retain the inline card and record a
navigation warning. Unavailable pages are not sent to AI as property facts.
No unrelated property or supplied layout-reference URL is substituted for a
missing deal. Locked addresses remain unknown; existing downstream address
gates can hold/skip them for publication while the captured facts remain stored.

Available property pages use their main content. Rent/Flip panels describe one
deal; asking price, ARV, rehab budget, rental projections and hypothetical
new-build figures remain distinct. The public reference layout hides the
address behind login, and the handler does not attempt to unlock it.

Ivan's Property Details page was verified to load. His template distinguishes
the $177,500 asking price from $352,000 ARV and the optional adjacent-lot scenario.
Its direct contact is `ivan@equitypro.com`, distinct from the bulk sender address.
The handler can use a browser's certificate store when the local Python CA store
lacks an issuer; TLS verification stays enabled.

Seeding upgrades Ivan's exact unedited version-1 default prompt to version 2.
User-edited database prompts and snapshots on already queued jobs are preserved.

## API

Alam Wali (`alam@spectrumpropertygroup.com`) has a dedicated `senders/alam.py`
handler, `alam_v1`. It follows Additional Information through tracking redirects,
renders the Baseline page, opens View all N photos, and waits for the complete
gallery. The supplied Ormond Beach page was checked read-only: 39 photos,
$184,900 purchase price, $35,000 rehab, and $265,000 ARV. The current 1.5 baths
remain distinct from a proposed two-full-bath conversion. A missing street
address remains unknown; existing publication rules still apply.

Sender-collected gallery URLs are uploaded by the independent pipeline's
`galleries` stage before media/rules processing. All collected photos go into
one listing-specific Dropbox folder; each successful photo upload is checkpointed.
A failed upload stops later stages and retries the remaining photos. The final
folder link is saved only after the whole gallery uploads. Preview extracts
details/photo URLs without uploading to Dropbox or publishing. No live AI
extraction, Dropbox write, or publication was run during template verification.

Mounted by `api_app.py` under `/api/new-emails-list`:

| Method | Path | Action |
| --- | --- | --- |
| GET / POST | empty suffix | List / create template |
| GET / PUT / DELETE | `/{entry_id}` | Read / replace and increment version / delete |
| GET | `/handlers` | Registered strategy keys |
| POST | `/seed-templates` | Seed defaults without overwriting edits |
| POST | `/{entry_id}/preview` | JSON `{ "html": "..." }`; linked-page extraction without publication |
| GET | `/jobs?status=failed&limit=50` | Stage results, extraction warnings and errors |
| POST | `/jobs/{job_id}/retry` | Retry failed or waiting jobs using their frozen template |

To disable a default template across worker restarts, set `active=false` instead
of deleting it. The seed operation restores deleted default templates.

## Verification

Install `mongomock` for the offline test suite in addition to runtime dependencies:

```sh
python -m unittest discover -s tests -p test_new_emails.py -v
python scripts/verify_new_email_links.py path/to/template.eml
python scripts/verify_new_email_links.py path/to/john.eml --button-label "Click for more" --prefix --handler john_v1
```

The tests use mocked extraction/publication and an in-memory database. They cover
all seven buttons, nested links and cycles, redirected relative URLs, inline
deals, merging, missing-number rules, forwarding, both inboxes, failed scan
cursors, leases/retries, prompt versions, Podio acknowledgement and the 6% gate.
The second command only opens detail pages; it does not use AI or publish.
