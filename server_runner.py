# server_runner.py
import time
import logging
from schedule import every, repeat, run_pending

# import your task entrypoints
from gmail_hourly_multi import process_account, _ensure_paths, ACCOUNTS
from processFilteredEmail import process_pending
from process_dup30days import process_not_processed_with_duplicate_rule
from ai_nl_rules_runner import apply_ai_english_rules
from post_selection import select_passed_listings_for_post
from ai_make_whatsapp_posts import make_whatsapp_posts_from_ready_to_post
from wp_ai_mapper_catalog_first import ai_build_wp_payload_for_posted
from wp_ai_property_description import ai_build_wp_property_description_for_posted
from wp_sync_poster import sync_wp_for_descriptions
from ai_media_verify import verify_and_fill_missing_media_for_not_processed
from wp_price_red_pic_links import process_wp_price_and_media_updates
from gmail_hourly_multi import build_service_by_account
from forward_completed_sources import forward_completed_source_emails

from mongo_engine_conn import init_db
from models import FilteredListingEmail, ParsedListing

from whatsapp_keepalive import send_keepalive_template, parse_recipients_env

from image_curation import process_listings_ready_for_image_processing

import os
from dotenv import load_dotenv
load_dotenv()
import json
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from http.server import BaseHTTPRequestHandler, HTTPServer

START_TIME = time.time()  # for uptime calculation


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Support both /server-sattus (as you requested) and /server-status (correct spelling)
        if self.path in ("/server-sattus", "/server-status"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            response = {
                "status": "working",
                "uptime_seconds": int(time.time() - START_TIME),
            }

            self.wfile.write(json.dumps(response).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    # Avoid default noisy logging to stderr
    def log_message(self, format, *args):
        logging.info("StatusHandler: " + format % args)


def start_status_server(port: int = 8000):
    server = HTTPServer(("0.0.0.0", port), StatusHandler)
    logging.info("Status server listening on 0.0.0.0:%s", port)
    server.serve_forever()

def gmail_fetch_all():
    logging.info("gmail_fetch_all: start")
    for raw in ACCOUNTS:
        acct = _ensure_paths(raw)
        try:
            process_account(acct)
        except Exception as e:
            logging.exception("gmail_fetch_all: error on %s", acct.label)
    logging.info("gmail_fetch_all: done")

# add more jobs here if you want
def placeholder_other_job():
    logging.info("other job ran")



# schedule every 30 minutes
@repeat(every(5).minutes)
def run_gmail_job():
    gmail_fetch_all()

# schedule parse email to create listing every minute
@repeat(every(1).minutes)
def run_process_email():
    logging.info("run_process_email")
    process_pending()

@repeat(every(3).minutes)
def run_verify_and_fill_missing_media_for_not_processed():
    logging.info("verify_and_fill_missing_media_for_not_processed")
    verify_and_fill_missing_media_for_not_processed(limit=35, max_workers=8)

@repeat(every(2).minutes)
def run_process_wp_price_and_media_updates():
    logging.info("process_wp_price_and_media_updates")
    process_wp_price_and_media_updates(limit=5)

# schedule parse email to create listing every 5 minute
@repeat(every(1).minutes)
def run_process_dup30days():
    logging.info("process_dup30days")
    process_not_processed_with_duplicate_rule()


@repeat(every(5).minutes)
def run_ai_nl_rules_runner():
    logging.info("ai_nl_rules_runner")
    apply_ai_english_rules("ai_listing_rules.yaml", limit=50)


@repeat(every(10).minutes)
def run_select_passed_listings_for_post():
    logging.info("select_passed_listings_for_post")
    select_passed_listings_for_post(limit=200, sort_by="created_at", mark_ready_status=None)


@repeat(every(2).minutes)
def run_process_listings_ready_for_image_processing():
    logging.info("process_listings_ready_for_image_processing")
    process_listings_ready_for_image_processing(limit=5)


@repeat(every(2).minutes)
def run_make_whatsapp_posts_from_ready_to_post():
    logging.info("make_whatsapp_posts_from_ready_to_post")
    make_whatsapp_posts_from_ready_to_post("ad_post_rules.txt", limit=5)

@repeat(every(3).minutes)
def run_ai_build_wp_payload_for_posted():
    logging.info("ai_build_wp_payload_for_posted")
    ai_build_wp_payload_for_posted(limit=5)


@repeat(every(3).minutes)
def run_ai_build_wp_property_description_for_posted():
    logging.info("ai_build_wp_property_description_for_posted")
    ai_build_wp_property_description_for_posted(limit=5, batch_size=10, per_item_sleep_s=0.2)

@repeat(every(5).minutes)
def run_sync_wp_for_descriptions():
    logging.info("sync_wp_for_descriptions")
    sync_wp_for_descriptions(limit=5, per_item_sleep_s=0.2)

@repeat(every(15).minutes)
def run_forward_email():
    logging.info("run_forward_email")
    service_by_account = build_service_by_account()

    # Where to forward
    TO = os.getenv("FORWARD_EMAIL")

    stats = forward_completed_source_emails(
        service_by_account=service_by_account,
        to_addr=TO,
        limit=10,
    )

# @repeat(every(15).hours)
# def run_whatsapp_keepalive():
#     logging.info("run_whatsapp_keepalive")
#     recipients = parse_recipients_env(os.getenv("TEAM_WHATSAPP_RECIPIENTS", ""))
#     if not recipients:
#         logging.warning("No TEAM_WHATSAPP_RECIPIENTS configured; skipping keepalive")
#         return
#     try:
#         send_keepalive_template(recipients)
#     except Exception:
#         logging.exception("run_whatsapp_keepalive failed")


# example: another job every 2 hours
# @repeat(every(2).hours)
# def run_other_job():
#     placeholder_other_job()

if __name__ == "__main__":
    logging.info("Scheduler loop started")

    # One-time DB bootstrap
    init_db()
    try:
        FilteredListingEmail.ensure_indexes()
        ParsedListing.ensure_indexes()
        # gmail_fetch_all()
    except Exception:
        logging.exception("ensure_indexes failed")

    # Start status HTTP server in background
    status_port = int(os.getenv("STATUS_PORT", "8000"))
    status_thread = threading.Thread(
        target=start_status_server,
        kwargs={"port": status_port},
        daemon=True,
    )
    status_thread.start()

    logging.info("Status endpoint available at /server-sattus (and /server-status) on port %s", status_port)


    # Loop forever
    while True:
        run_pending()
        time.sleep(1)
