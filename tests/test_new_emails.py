"""Offline regression tests: navigation, merging, policies, durable jobs and CRUD."""
import ast
import base64
from datetime import datetime, timedelta
from pathlib import Path
import sys
import types
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from new_emails.handlers import button_links, missing_house_number, registry
from new_emails.workflow import extract_properties
from new_emails.sender_match import matches_sender
from new_emails.config import ETHAN_EMAIL, MICHELLE_EMAIL, JOHN_EMAIL, IVAN_EMAIL, seed_templates

FIXTURE = (Path(__file__).parent / "fixtures/michelle_email.html").read_text(encoding="utf-8")
JOHN_FIXTURE = (Path(__file__).parent / "fixtures/john_email.html").read_text(encoding="utf-8")
IVAN_FIXTURE = (Path(__file__).parent / "fixtures/ivan_email.html").read_text(encoding="utf-8")
IVAN_DIGEST = (Path(__file__).parent / "fixtures/ivan_digest_email.html").read_text(encoding="utf-8")
EQUITYPRO_PAGE = (Path(__file__).parent / "fixtures/equitypro_property.html").read_text(encoding="utf-8")


def config(**values):
    return SimpleNamespace(handler_key="michelle_v1", button_labels=["Get More Info"],
                           prompt="fixture prompt", max_pages=100, max_depth=3,
                           detail_button_labels=["Get More Info", "View More Details"], **values)


class NavigationTests(unittest.TestCase):
    def test_alam_additional_information_and_full_gallery(self):
        from new_emails.navigation import Page
        from new_emails.extraction import FetchedHTML
        handler = registry.get("alam_v1")
        email = (ROOT / "tests/fixtures/alam_email.html").read_text(encoding="utf8")
        html = (ROOT / "tests/fixtures/alam_property.html").read_text(encoding="utf8")
        self.assertEqual(handler.select_links(email, handler.button_labels),
                         [("Additional Information", "https://example.com/alam/details")])
        page = Page("Additional Information", "https://example.com/alam/details", html)
        photos = handler.gallery_images(page)
        self.assertEqual(len(photos), 39)
        self.assertEqual(photos[0], "https://fixture.cdn.bubble.io/f1/IMG_1.jpeg")
        self.assertNotIn("logo", " ".join(photos))
        with patch("new_emails.browser_transport.fetch_rendered_page", return_value=FetchedHTML(html, page.url)) as render:
            result = handler.fetch_content("https://example.com/tracking", Mock())
        self.assertEqual(result.url, page.url)
        self.assertEqual(render.call_args.kwargs["prepare_page"], handler.prepare_page)
        self.assertIn("1 full and", handler.prompt)
        self.assertIn("Do not invent a street address", handler.prompt)

    def test_alam_merges_inline_and_detail_and_requires_gallery(self):
        from new_emails.navigation import Page
        from new_emails.extraction import FetchedHTML
        handler = registry.get("alam_v1")
        email = (ROOT / "tests/fixtures/alam_email.html").read_text(encoding="utf8")
        html = (ROOT / "tests/fixtures/alam_property.html").read_text(encoding="utf8")
        cfg = SimpleNamespace(**handler.default_config())
        extractor = Mock()
        extractor.extract.side_effect = [
            [{"source_title": "Off Market Ormond Beach Block Flip", "address": None, "list_price_usd": 184900}],
            [{"source_title": "Off Market Ormond Beach Block Flip", "address": "123 Example St", "estimated_arv": 265000}]]
        with patch("new_emails.browser_transport.fetch_rendered_page", return_value=FetchedHTML(html,"https://example.com/alam/details")):
            rows = extract_properties(email, cfg, extractor=extractor)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["address"], "123 Example St")
        self.assertEqual(rows[0]["list_price_usd"], 184900)
        self.assertEqual(len(rows[0]["new_email_gallery_images"]), 39)
        self.assertTrue(rows[0]["new_email_gallery_required"])
        with self.assertRaisesRegex(ValueError, "no photos"):
            handler.enrich_listing({}, Page("Additional Information", "https://example.com", "<p>Empty gallery</p>"))

    def test_alam_opens_gallery_and_waits_for_advertised_photo_count(self):
        handler = registry.get("alam_v1")
        page = Mock()
        page.get_by_text.return_value.first.inner_text.return_value = "View all 39 photos"
        handler.prepare_page(page)
        page.get_by_text.return_value.first.click.assert_called_once()
        self.assertEqual(page.wait_for_function.call_args.kwargs["arg"], 39)

    def test_ivan_both_formats_and_all_eight_detail_buttons(self):
        handler = registry.get("ivan_v1")
        self.assertEqual(handler.detect_format(IVAN_FIXTURE), "single_property")
        self.assertEqual(handler.detect_format(IVAN_DIGEST), "multi_property_digest")
        cfg = SimpleNamespace(**handler.default_config())
        fetch = Mock(return_value="<main>Available property facts</main>")
        pages = list(handler.pages(IVAN_DIGEST, cfg, fetch))
        self.assertEqual(len(pages), 9)
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         [f"https://example.com/ivan/{i}" for i in range(1, 9)])

    def test_ivan_unavailable_digest_retains_all_eight_inline_deals(self):
        cfg = SimpleNamespace(**registry.get("ivan_v1").default_config())
        titles = ["Cheap Brevard Condo", "Downtown Sanford Bungalow", "Lady Lake Flip Opportunity",
                  "Hunters Creek Pool Flip", "Riverview Flip", "10K Lake County Lot", "Bartow Block Flip", "Orlando Villa Near Metrowest"]
        prices = [97500, 88900, 177500, 342500, 170000, 9900, 159900, 165000]
        arvs = [160000, 190000, 352000, 510000, 305000, 270000, 275000, 250000]
        cards = [{"source_title": title, "address": None, "list_price_usd": price, "estimated_arv": arv}
                 for title, price, arv in zip(titles, prices, arvs)]
        extractor = Mock()
        extractor.extract.return_value = cards
        rows = extract_properties(IVAN_DIGEST, cfg,
                fetch=lambda _: "<main><h1>This property is no longer available</h1></main>", extractor=extractor)
        self.assertEqual(len(rows), 8)
        self.assertEqual(len(rows.navigation_errors), 8)
        extractor.extract.assert_called_once()
        self.assertEqual([row["list_price_usd"] for row in rows], prices)
        self.assertEqual([row["estimated_arv"] for row in rows], arvs)

    def test_ivan_http404_retains_digest_cards(self):
        cfg = SimpleNamespace(**registry.get("ivan_v1").default_config())
        extractor = Mock()
        extractor.extract.return_value = [{"source_title": "Cheap Brevard Condo", "list_price_usd": 97500}]
        fetch = Mock(side_effect=HTTPError("https://example.com/deleted", 404, "gone", {}, None))
        rows = extract_properties(IVAN_DIGEST, cfg, fetch=fetch, extractor=extractor)
        self.assertEqual(len(rows.navigation_errors), 8)
        self.assertEqual(len(rows), 1)
        self.assertEqual(fetch.call_count, 8)

    def test_ivan_reference_page_is_available_with_address_locked(self):
        handler = registry.get("ivan_v1")
        self.assertFalse(handler.unavailable(EQUITYPRO_PAGE))
        html = handler.fetch_content("https://equitypro.example.com/property", lambda _: EQUITYPRO_PAGE)
        self.assertIn("Investor Price $425,000", html)
        self.assertIn("Login or register to unlock", html)
        self.assertNotIn("Upcoming workshop", html)
        self.assertIn("Rent and Flip panels repeat the same property", handler.prompt)
        self.assertIn("address\nmust remain null", handler.prompt)

    def test_ivan_locked_address_detail_enriches_matching_card_without_duplicate(self):
        cfg = SimpleNamespace(**registry.get("ivan_v1").default_config())
        facts = {"bedrooms": 4, "list_price_usd": 177500, "estimated_arv": 352000}
        extractor = Mock()
        extractor.extract.side_effect = [[{"source_title": "Lady Lake Flip Opportunity", "address": None, **facts}],
                                         [{"source_title": "Property page title", "address": None, "living_area_sqft": 2221, **facts}]]
        rows = extract_properties(IVAN_FIXTURE, cfg, fetch=lambda _: EQUITYPRO_PAGE, extractor=extractor)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["address"])
        self.assertEqual(rows[0]["living_area_sqft"], 2221)

    def test_ivan_actual_unavailable_notice_is_not_sent_to_ai(self):
        cfg = SimpleNamespace(**registry.get("ivan_v1").default_config())
        notice = '<main>Thank you for your interest! Our properties move quickly and this one is no longer available. Please visit our properties page for all available deals.</main>'
        pages = list(registry.get("ivan_v1").pages(IVAN_FIXTURE, cfg, lambda _: notice))
        self.assertIsNone(pages[0].error)
        self.assertIn("unavailable", pages[1].error)

    def test_ivan_matching_title_merges_updated_price_and_locked_address(self):
        from new_emails.navigation import Page
        existing = {("inline",): {"source_title": "Downtown Sanford Bungalow", "address": None, "list_price_usd": 88900}}
        detail = {"source_title": "Downtown Sanford Bungalow", "address": None, "list_price_usd": 85000}
        key = registry.get("ivan_v1").reconcile_key(detail,
                Page("Property Details", "https://example.com/detail", '<h1>Downtown Sanford Bungalow</h1>'), existing, ("new",))
        self.assertEqual(key, ("inline",))

    def test_ivan_extracts_email_and_only_property_details_link(self):
        cfg = SimpleNamespace(**registry.get("ivan_v1").default_config())
        fetch = Mock(return_value="<h1>Lady Lake property details</h1>")
        pages = list(registry.get("ivan_v1").pages(IVAN_FIXTURE, cfg, fetch))
        self.assertEqual(len(pages), 2)
        fetch.assert_called_once_with("https://example.com/ivan/lady-lake")
        self.assertTrue(matches_sender("Rich <rich@example.com>", "", IVAN_FIXTURE, IVAN_EMAIL))
        self.assertIn("$177,500", cfg.prompt)
        self.assertIn("$352,000", cfg.prompt)
        self.assertIn("ivan@equitypro.com", cfg.prompt)

    def test_ivan_keeps_asking_price_distinct_from_arv_through_workflow(self):
        cfg = SimpleNamespace(**registry.get("ivan_v1").default_config())
        extractor = Mock()
        extractor.extract.side_effect = [[], [{"address": "123 Lady Lake St", "city": "Lady Lake", "state": "FL",
                                               "list_price_usd": 177500, "estimated_arv": 352000}]]
        rows = extract_properties(IVAN_FIXTURE, cfg, fetch=lambda _: "<main>Property HTML</main>", extractor=extractor)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["list_price_usd"], 177500)
        self.assertEqual(rows[0]["estimated_arv"], 352000)
        self.assertEqual(extractor.extract.call_args_list[1].args[0].html, "<main>Property HTML</main>")

    def test_registry_accepts_sender_instances_and_uses_their_own_files(self):
        from new_emails.registry import HandlerRegistry
        from new_emails.senders.ethan import EthanHandler
        from new_emails.senders.michelle import MichelleHandler
        from new_emails.senders.john import JohnHandler
        from new_emails.senders.ivan import IvanHandler
        handlers = [EthanHandler(), MichelleHandler(), JohnHandler(), IvanHandler()]
        injected = HandlerRegistry(handlers)
        self.assertEqual(len(injected.defaults()), 4)
        for handler in handlers:
            self.assertIs(injected.get(handler.handler_key), handler)
            self.assertTrue(type(handler).__module__.startswith("new_emails.senders."))
            self.assertEqual(handler.default_config()["prompt"], handler.prompt)
        with self.assertRaisesRegex(ValueError, "already registered"):
            HandlerRegistry([handlers[0], handlers[0]])

    def test_ivan_certificate_fallback_keeps_browser_tls_verification(self):
        import ssl
        from urllib.error import URLError
        handler = registry.get("ivan_v1")
        fetch = Mock(side_effect=URLError(ssl.SSLCertVerificationError("missing local issuer")))
        with patch("new_emails.browser_transport.fetch_rendered_page", return_value="rendered details") as browser:
            self.assertEqual(handler.fetch_content("https://example.com/details", fetch), "rendered details")
        browser.assert_called_once_with("https://example.com/details", require_price=False)

    def test_ivan_enriches_addressless_email_card_without_duplicate_deal(self):
        cfg = SimpleNamespace(**registry.get("ivan_v1").default_config())
        facts = {"city": "Lady Lake", "state": "FL", "list_price_usd": 177500,
                 "estimated_arv": 352000, "bedrooms": 4, "living_area_sqft": 2221}
        extractor = Mock()
        extractor.extract.side_effect = [[{"source_title": "Lady Lake Flip Opportunity", "address": None, **facts}],
                                         [{"source_title": "Actual street address", "address": "123 Lake St", **facts}]]
        rows = extract_properties(IVAN_FIXTURE, cfg, fetch=lambda _: "Property details", extractor=extractor)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["address"], "123 Lake St")
        self.assertEqual(rows[0]["new_email_sources"], ["email", "https://example.com/ivan/lady-lake"])

    def test_ivan_does_not_merge_ambiguous_addressless_cards(self):
        from new_emails.navigation import Page
        card = {"address": None, "bedrooms": 4, "living_area_sqft": 2221}
        details = {"address": "123 Lake St", "bedrooms": 4, "living_area_sqft": 2221}
        fallback = ("resolved property",)
        key = registry.get("ivan_v1").reconcile_key(details, Page("Property Details", "", ""),
                                                   {("card1",): card, ("card2",): dict(card)}, fallback)
        self.assertEqual(key, fallback)

    def test_john_follows_all_five_address_suffixed_buttons(self):
        cfg = config()
        cfg.handler_key, cfg.button_labels = "john_v1", ["Click for more"]
        fetch = Mock(return_value="<p>Property details</p>")
        pages = list(registry.get("john_v1").pages(JOHN_FIXTURE, cfg, fetch))
        self.assertEqual(len(pages), 6)
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         [f"https://example.com/jax/{i}" for i in range(1, 6)])
        self.assertTrue(matches_sender("Rich <rich@example.com>", "", JOHN_FIXTURE, JOHN_EMAIL))

    def test_john_renders_javascript_shell_before_ai_extraction(self):
        from new_emails.extraction import FetchedHTML
        cfg = config()
        cfg.handler_key, cfg.button_labels = "john_v1", ["Click for more"]
        shell = FetchedHTML('<div id="root"></div><noscript>You need to enable JavaScript to run this app.</noscript>',
                            "https://wholesalejax.example.com/deal/123")
        extractor = Mock()
        extractor.extract.side_effect = [[], [{"address": "10884 Krugerrand Ln", "city": "Jacksonville", "list_price_usd": 200000}]]
        rendered = FetchedHTML('<h1>10884 Krugerrand Ln</h1><p>Asking price $200,000</p>', shell.url)
        with patch("new_emails.browser_transport.fetch_rendered_page", return_value=rendered) as browser:
            rows = extract_properties('<a href="https://tracking.example.com/click">Click for more about 10884 Krugerrand Ln</a>',
                                      cfg, fetch=Mock(return_value=shell), extractor=extractor)
        browser.assert_called_once_with(shell.url)
        self.assertEqual(extractor.extract.call_args_list[1].args[0].html, rendered)
        self.assertEqual(rows[0]["list_price_usd"], 200000)

    def test_john_all_five_detail_html_pages_reach_extractor(self):
        cfg = config()
        cfg.handler_key, cfg.button_labels = "john_v1", ["Click for more"]
        addresses = ["10884 Krugerrand Ln", "5820 Porsche Rd", "4040 Green St", "9102 6th Ave", "2418 Vernon St"]
        extractor = Mock()
        extractor.extract.side_effect = [[]] + [[{"address": address, "city": "Jacksonville"}] for address in addresses]
        rows = extract_properties(JOHN_FIXTURE, cfg, fetch=lambda url: f"<main>HTML for {url}</main>", extractor=extractor)
        self.assertEqual(len(rows), 5)
        self.assertEqual(extractor.extract.call_count, 6)
        self.assertEqual([row["address"] for row in rows], addresses)
        for index, call in enumerate(extractor.extract.call_args_list[1:], 1):
            self.assertIn(f"https://example.com/jax/{index}", call.args[0].html)

    def test_loaded_property_with_noscript_notice_is_not_an_empty_shell(self):
        from new_emails.browser_transport import is_javascript_shell
        html = ('<div id="root">10884 Krugerrand Ln OUR CASH PRICE $209,900 '
                + 'Loaded property description. ' * 20 + '</div>'
                + '<noscript>You need to enable JavaScript to run this app.</noscript>')
        self.assertFalse(is_javascript_shell(html))

    def test_all_seven_info_links_ignore_offer_and_footer(self):
        calls = []
        pages = list(registry.get("michelle_v1").pages(FIXTURE, config(), lambda url: calls.append(url) or "<p>Details</p>"))
        self.assertEqual(len(calls), 7)
        self.assertEqual(len(pages), 8)
        self.assertEqual(pages[0].label, "email")
        self.assertEqual(calls, [f"https://example.com/property/{i}" for i in range(1, 8)])

    def test_nested_pages_relative_links_and_cycles(self):
        fetch = Mock(side_effect=lambda url: {
            "https://example.com/category": '<a href="/details">View More Details</a>',
            "https://example.com/details": '<a href="/category">View More Details</a>',
        }[url])
        pages = list(registry.get("michelle_v1").pages(
            '<a href="https://example.com/category">Get More Info</a>', config(), fetch))
        self.assertEqual(len(pages), 3)
        self.assertEqual(fetch.call_count, 2)

    def test_navigation_limit_raises_instead_of_truncating(self):
        cfg = config()
        cfg.max_pages = 2
        with self.assertRaisesRegex(ValueError, "Navigation limit"):
            list(registry.get("michelle_v1").pages(FIXTURE, cfg, lambda _: "details"))

    def test_inline_only_email_is_processed(self):
        pages = list(registry.get("michelle_v1").pages("<p>Inline deal</p>", config(), Mock()))
        self.assertEqual(len(pages), 1)

    def test_full_and_masked_house_numbers(self):
        for address in ["Dickson st fort Pierce", "5th Avenue", "31XX Dickson", "*** Dickson", "_ Dickson"]:
            self.assertTrue(missing_house_number(address), address)
        for address in ["315 Dickson", "315A Dickson", "315-317 Dickson", "#315 Dickson", "", None]:
            self.assertFalse(missing_house_number(address), address)

    def test_ethan_requires_categories_and_filters_full_numbers(self):
        cfg = config()
        cfg.handler_key = "ethan_v1"
        cfg.button_labels = ["Fixers", "2 to 4", "5 plus"]
        with self.assertRaisesRegex(ValueError, "Missing email buttons"):
            list(registry.get("ethan_v1").pages("", cfg, Mock()))
        html = ''.join(f'<a href="https://example.com/{i}">{label}</a>' for i, label in enumerate(cfg.button_labels))
        extractor = Mock()
        extractor.extract.return_value = [{"address": "315 Dickson"}, {"address": "Dickson St", "city": "Fort Pierce"}]
        rows = extract_properties(html, cfg, fetch=lambda _: "details", extractor=extractor)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["address"], "Dickson St")

    def test_multiple_deals_inline_and_inside_merge_non_null_details(self):
        extractor = Mock()
        extractor.extract.side_effect = [
            [{"address": "1133 NW 79 TER", "city": "Miami", "state": "FL", "list_price_usd": 879900},
             {"address": "6094 Forest Hill Blvd", "city": "West Palm Beach", "list_price_usd": 89900}],
            [{"address": "1133 NW 79th Terrace", "city": "Miami", "state": "FL", "list_price_usd": None, "unit_count": 7},
             {"address": "777 New St", "city": "Miami", "list_price_usd": 500000}],
        ]
        rows = extract_properties('<a href="https://example.com/detail">Get More Info</a>',
                                  config(), fetch=lambda _: "details", extractor=extractor)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["list_price_usd"], 879900)
        self.assertEqual(rows[0]["unit_count"], 7)
        self.assertEqual(rows[0]["new_email_sources"], ["email", "https://example.com/detail"])

    def test_forwarded_sender_matches_but_body_mentions_do_not(self):
        self.assertTrue(matches_sender("Rich <rich@example.com>", "", FIXTURE, MICHELLE_EMAIL))
        self.assertTrue(matches_sender(MICHELLE_EMAIL, "", "", MICHELLE_EMAIL))
        self.assertFalse(matches_sender("other@example.com", f"Contact {MICHELLE_EMAIL}", "", MICHELLE_EMAIL))

    def test_broken_detail_page_retains_email_deal_and_records_warning(self):
        extractor = Mock()
        extractor.extract.return_value = [{"address": "1328 SE 1st Way", "city": "Deerfield Beach", "list_price_usd": 399900}]
        fetch = Mock(side_effect=HTTPError("https://example.com/deleted", 404, "Not found", {}, None))
        rows = extract_properties('<a href="https://example.com/deleted">Get More Info</a>',
                                  config(), fetch=fetch, extractor=extractor)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["list_price_usd"], 399900)
        self.assertEqual(len(rows.navigation_errors), 1)
        self.assertEqual(extractor.extract.call_count, 1)

    def test_transient_detail_page_failure_is_not_silently_ignored(self):
        fetch = Mock(side_effect=HTTPError("https://example.com/details", 503, "Unavailable", {}, None))
        with self.assertRaises(HTTPError):
            list(registry.get("michelle_v1").pages('<a href="https://example.com/details">Get More Info</a>', config(), fetch))

    def test_relative_links_use_redirect_destination(self):
        from new_emails.extraction import FetchedHTML
        calls = []
        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                return FetchedHTML('<a href="/detail">View More Details</a>', "https://property.example.com/category")
            return "details"
        list(registry.get("michelle_v1").pages('<a href="https://tracking.example.com/click">Get More Info</a>', config(), fetch))
        self.assertEqual(calls[1], "https://property.example.com/detail")


class DatabaseTests(unittest.TestCase):
    def test_required_gallery_retries_missing_photo_and_checkpoints_completed_uploads(self):
        from new_emails.gallery import publish_galleries
        from new_emails.pipeline import persist_properties
        job = self.job()
        photos = ["https://example.com/photo1.jpeg", "https://example.com/photo2.jpeg"]
        persist_properties(job, [{"source_title": "Ormond Beach Flip", "new_email_gallery_required": True,
                                  "new_email_gallery_images": photos, "images": photos}])
        upload = Mock(side_effect=[["https://dropbox.com/gallery"], []])
        with patch("new_emails.extraction.validate_public_url"):
            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                publish_galleries(gmail_message_id=job.pipeline_message_id, upload=upload)
            listing = self.Listing.objects.get()
            self.assertFalse(listing.other_images_dropbox_link)
            self.assertEqual(listing.complete_info["new_email_gallery_uploaded"], photos[:1])
            upload = Mock(return_value=["https://dropbox.com/gallery"])
            result = publish_galleries(gmail_message_id=job.pipeline_message_id, upload=upload)
            self.assertEqual(result["uploaded"], 1)
            self.assertEqual(upload.call_args.args[0], photos[1:])
            self.assertFalse(upload.call_args.kwargs["curate_media"])
            self.assertEqual(self.Listing.objects.get().other_images_dropbox_link, "https://dropbox.com/gallery")
            self.assertEqual(publish_galleries(gmail_message_id=job.pipeline_message_id, upload=upload)["uploaded"], 0)
            upload.assert_called_once()

    def test_gallery_upload_failure_stops_publishing(self):
        from new_emails.pipeline import process_one
        self.job()
        downstream = Mock()
        result = process_one(extract=lambda *_: [], stages_factory=lambda: [
            ("galleries", Mock(side_effect=RuntimeError("Gallery photo upload failed")), {}),
            ("publisher", downstream, {})])
        self.assertEqual(result["status"], "retry")
        downstream.assert_not_called()

    @classmethod
    def setUpClass(cls):
        import mongoengine, mongomock
        mongoengine.connect("new_email_tests", host="mongodb://localhost", mongo_client_class=mongomock.MongoClient)
        from models import ParsedListing, FilteredListingEmail
        from new_emails.models import NewEmailsList, NewEmailJob, NewEmailCursor
        cls.Listing, cls.Email = ParsedListing, FilteredListingEmail
        cls.Config, cls.Job, cls.Cursor = NewEmailsList, NewEmailJob, NewEmailCursor

    def setUp(self):
        for model in (self.Listing, self.Email, self.Config, self.Job, self.Cursor):
            model.drop_collection()

    def job(self, **kwargs):
        seed_templates()
        from new_emails.ingestion import snapshot
        cfg = self.Config.objects(sender_email=MICHELLE_EMAIL).get()
        return self.Job(account_label="acct1", message_id="message", pipeline_message_id="new_email_acct1_message",
                        sender_email=MICHELLE_EMAIL, html=FIXTURE, config_snapshot=snapshot(cfg), **kwargs).save()

    def test_seed_is_idempotent_and_preserves_prompt_edits(self):
        seed_templates()
        cfg = self.Config.objects(sender_email=MICHELLE_EMAIL).get()
        cfg.update(set__prompt="Edited")
        seed_templates()
        self.assertEqual(self.Config.objects.count(), 5)
        self.assertEqual(self.Config.objects(sender_email=MICHELLE_EMAIL).get().prompt, "Edited")

    def test_ivan_unedited_default_prompt_upgrade_preserves_queued_snapshot(self):
        from new_emails.senders.ivan import LEGACY_AI_PROMPT, AI_PROMPT
        old = self.Config(sender_email=IVAN_EMAIL, handler_key="ivan_v1", prompt=LEGACY_AI_PROMPT,
                          prompt_version=1, button_labels=["Property Details"]).save()
        from new_emails.ingestion import snapshot
        job = self.Job(account_label="acct1", message_id="ivan-old", pipeline_message_id="new_email_acct1_ivan-old",
                       sender_email=IVAN_EMAIL, config_snapshot=snapshot(old)).save()
        seed_templates()
        old.reload()
        job.reload()
        self.assertEqual(old.prompt_version, 2)
        self.assertEqual(old.prompt, AI_PROMPT)
        self.assertEqual(job.config_snapshot["prompt"], LEGACY_AI_PROMPT)

    def test_ivan_edited_prompt_is_not_overwritten_by_upgrade(self):
        self.Config(sender_email=IVAN_EMAIL, handler_key="ivan_v1", prompt="Custom Ivan instructions",
                    prompt_version=1, button_labels=["Property Details"]).save()
        seed_templates()
        self.assertEqual(self.Config.objects(sender_email=IVAN_EMAIL).get().prompt, "Custom Ivan instructions")

    def test_config_snapshot_does_not_change_after_edit(self):
        job = self.job()
        self.Config.objects(sender_email=MICHELLE_EMAIL).update(set__prompt="New prompt", inc__prompt_version=1)
        self.assertNotEqual(job.config_snapshot["prompt"], "New prompt")
        self.assertEqual(job.config_snapshot["prompt_version"], 1)

    def test_durable_failure_retry_does_not_repeat_extraction(self):
        from new_emails.pipeline import process_one
        from models import ParsedListing
        job = self.job()
        extractor = Mock(return_value=[{"address": "123 Main St", "city": "Miami", "list_price_usd": 400000}])
        failing = Mock(side_effect=RuntimeError("temporary publisher outage"))
        result = process_one(extract=extractor, stages_factory=lambda: [("publisher", failing, {})])
        self.assertEqual(result["status"], "retry")
        self.assertEqual(ParsedListing.objects.count(), 1)
        self.Job.objects(id=job.id).update(set__next_attempt_at=datetime.utcnow())
        def succeed(**kwargs):
            ParsedListing.objects(gmail_message_id=kwargs["gmail_message_id"]).update(
                set__status="posted", set__whatsapp_status="sent", set__wp_status="posted",
                set__new_email_podio_sent_at=datetime.utcnow())
            return {"posted": 1}
        result = process_one(extract=extractor, stages_factory=lambda: [("publisher", succeed, {})])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(extractor.call_count, 1)
        self.assertEqual(ParsedListing.objects.count(), 1)
        self.assertIsNone(process_one(extract=extractor, stages_factory=lambda: []))

    def test_claim_does_not_take_active_lease(self):
        from new_emails.pipeline import process_one
        self.job(status="processing", lease_until=datetime.utcnow() + timedelta(hours=1))
        self.assertIsNone(process_one(stages_factory=lambda: []))

    def test_expired_lease_is_recovered(self):
        from new_emails.pipeline import process_one
        self.job(status="processing", lease_until=datetime.utcnow() - timedelta(seconds=1))
        result = process_one(extract=lambda *_: [], stages_factory=lambda: [])
        self.assertEqual(result["status"], "completed")

    def test_queue_scope_excludes_new_pipeline_unless_explicit(self):
        from new_emails.isolation import scope_queue
        from new_emails.pipeline import persist_properties
        job = self.job()
        persist_properties(job, [{"address": "123 Main St"}])
        self.assertEqual(scope_queue(self.Listing.objects).count(), 0)
        self.assertEqual(scope_queue(self.Listing.objects, job.pipeline_message_id).count(), 1)

    def test_six_percent_boundary_and_posted_history(self):
        from new_emails.pipeline import persist_properties
        from new_emails.dedup import process_not_processed_with_duplicate_rule
        job = self.job()
        persist_properties(job, [{"address": "123 Main St", "city": "Miami", "zip": "33150", "list_price_usd": 100000}])
        prior = self.Listing.objects.get()
        prior.update(set__status="posted", set__skipped_or_posted_at=datetime.utcnow())
        for number, price, status in [(1, 95000, "skipped"), (2, 94000, "processed"), (3, 40000, "price_drop_review")]:
            mid = f"new_email_acct2_{number}"
            current = self.Listing(account_label="acct2", gmail_message_id=mid, list_index=1, source_email=prior.source_email,
                                   address="123 Main St", city="Miami", zip="33150", price=price, status="verified").save()
            result = process_not_processed_with_duplicate_rule(gmail_message_id=mid)
            current.reload()
            self.assertEqual(current.status, status)
            self.assertEqual(result["checked"], 1)

    def test_api_validation_versions_and_no_account_selector(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.new_emails_list import router
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        response = client.post("/api/new-emails-list/seed-templates")
        self.assertEqual(response.status_code, 200)
        entries = response.json()
        self.assertEqual(len(entries), 5)
        payload = {"sender_email": "new@example.com", "handler_key": "michelle_v1", "prompt": "Extract all", "button_labels": ["Get More Info"]}
        response = client.post("/api/new-emails-list", json=payload)
        self.assertEqual(response.status_code, 201)
        entry = response.json()
        self.assertEqual(entry["accounts"], ["acct1", "acct2"])
        self.assertEqual(client.post("/api/new-emails-list", json=payload).status_code, 409)
        payload["prompt"] = "Updated"
        response = client.put(f'/api/new-emails-list/{entry["id"]}', json=payload)
        self.assertEqual(response.json()["prompt_version"], 2)
        payload["handler_key"] = "unknown"
        self.assertEqual(client.post("/api/new-emails-list", json=payload).status_code, 422)

    def test_both_inboxes_forwarding_and_idempotent_scans(self):
        from new_emails.ingestion import scan_account
        seed_templates()
        gmail = types.ModuleType("ingestion.gmail")
        gmail._gmail_service = Mock()
        gmail._gmail_search = Mock(side_effect=lambda service, query, only_inbox:
                                   ["forwarded"] if MICHELLE_EMAIL in query else [])
        gmail._get_message = Mock(return_value={"payload": {"headers": [
            {"name": "From", "value": "Rich <rich@example.com>"},
            {"name": "Subject", "value": "Fwd: Deals"}]}})
        gmail._header = lambda headers, name: next((h["value"] for h in headers if h["name"] == name), None)
        gmail._decode_body = Mock(return_value=("", FIXTURE))
        with patch.dict(sys.modules, {"ingestion.gmail": gmail}):
            for label in ("acct1", "acct2"):
                result = scan_account(label, service=object(), now_epoch=1000000)
                self.assertEqual(result["queued"], 1)
                self.assertEqual(scan_account(label, service=object(), now_epoch=1000060)["queued"], 0)
        self.assertEqual(self.Job.objects.count(), 2)
        self.assertEqual(self.Cursor.objects.count(), 2)
        self.assertEqual({j.pipeline_message_id for j in self.Job.objects},
                         {"new_email_acct1_forwarded", "new_email_acct2_forwarded"})
        self.assertTrue(all(call.kwargs["only_inbox"] for call in gmail._gmail_search.call_args_list))

    def test_failed_gmail_scan_does_not_advance_cursor(self):
        from new_emails.ingestion import scan_account
        seed_templates()
        gmail = types.ModuleType("ingestion.gmail")
        gmail._gmail_service = Mock()
        gmail._gmail_search = Mock(side_effect=RuntimeError("account unavailable"))
        gmail._get_message = Mock()
        gmail._header = Mock()
        gmail._decode_body = Mock()
        with patch.dict(sys.modules, {"ingestion.gmail": gmail}):
            with self.assertRaisesRegex(RuntimeError, "account unavailable"):
                scan_account("acct2", service=object(), now_epoch=1000000)
        self.assertEqual(self.Cursor.objects.count(), 0)

    def test_podio_failure_retries_and_success_is_not_resent(self):
        from new_emails.pipeline import persist_properties
        from new_emails.publishing import publish_podio
        job = self.job()
        persist_properties(job, [{"address": "123 Main St"}])
        self.Listing.objects.update(set__status="posted")
        serializer = types.ModuleType("ai.whatsapp_posts")
        serializer._serialize_listing_full = lambda listing: {"id": str(listing.id)}
        with patch.dict(sys.modules, {"ai.whatsapp_posts": serializer}), patch.dict(
                "os.environ", {"NEW_EMAILS_PODIO_WEBHOOK_URL": "https://example.com/podio"}), patch("requests.post") as post:
            post.return_value.raise_for_status.side_effect = RuntimeError("503")
            with self.assertRaises(RuntimeError):
                publish_podio(gmail_message_id=job.pipeline_message_id)
            self.assertIsNone(self.Listing.objects.get().new_email_podio_sent_at)
            post.return_value.raise_for_status.side_effect = None
            self.assertEqual(publish_podio(gmail_message_id=job.pipeline_message_id)["sent"], 1)
            self.assertEqual(publish_podio(gmail_message_id=job.pipeline_message_id)["sent"], 0)
            self.assertEqual(post.call_count, 2)

    def test_price_drop_failure_stops_publication_and_retries(self):
        from new_emails.pipeline import process_one
        self.job()
        downstream = Mock()
        result = process_one(extract=lambda *_: [], stages_factory=lambda: [
            ("price_drop", lambda **_: {"failed": 1}, {}), ("publication", downstream, {})])
        self.assertEqual(result["status"], "retry")
        self.assertIn("activation failed", result["error"])
        downstream.assert_not_called()

    def test_wordpress_adapter_requeues_failed_message_only(self):
        from new_emails.pipeline import persist_properties
        from new_emails.publishing import sync_wordpress
        job = self.job()
        persist_properties(job, [{"address": "123 Main St"}])
        self.Listing.objects.update(set__wp_status="failed")
        adapter = types.ModuleType("integrations.wordpress.sync_poster")
        adapter.sync_wp_for_descriptions = Mock(return_value={"posted": 1})
        with patch.dict(sys.modules, {"integrations.wordpress.sync_poster": adapter}):
            result = sync_wordpress(gmail_message_id=job.pipeline_message_id)
        self.assertEqual(result["posted"], 1)
        self.assertEqual(self.Listing.objects.get().wp_status, "des_generated")
        adapter.sync_wp_for_descriptions.assert_called_once_with(gmail_message_id=job.pipeline_message_id, limit=100)


class AdapterContractTests(unittest.TestCase):
    def test_every_live_stage_supports_explicit_message_scope(self):
        functions = {
            "ai/media_verify.py": ["verify_and_fill_missing_media_for_not_processed"],
            "new_emails/dedup.py": ["process_not_processed_with_duplicate_rule"],
            "new_emails/rules_runner.py": ["apply_ai_english_rules"],
            "pipeline/price_drop_activate.py": ["process_price_drop_activations"],
            "pipeline/post_selection.py": ["select_passed_listings_for_post"],
            "ai/image_curation.py": ["process_listings_ready_for_image_processing", "process_primary_image_verification"],
            "ai/whatsapp_posts.py": ["make_whatsapp_posts_from_ready_to_post"],
            "integrations/wordpress/ai_mapper.py": ["ai_build_wp_payload_for_posted"],
            "integrations/wordpress/ai_property_description.py": ["ai_build_wp_property_description_for_posted"],
            "integrations/wordpress/sync_poster.py": ["sync_wp_for_descriptions"],
            "whatsapp/sender.py": ["process_whatsapp_queue"],
        }
        for filename, names in functions.items():
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
            for name in names:
                fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
                self.assertIn("gmail_message_id", [arg.arg for arg in fn.args.args + fn.args.kwonlyargs], f"{filename}:{name}")


if __name__ == "__main__":
    unittest.main()
