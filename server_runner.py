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
from whatsapp_sender import process_whatsapp_queue
from mongo_engine_conn import init_db
from models import FilteredListingEmail, ParsedListing, SpecialAvail
from podio_direct_wholeseller import process_direct_wholeseller_batch,initialize_direct_wholeseller_flag
from whatsapp_keepalive import send_keepalive_template, parse_recipients_env

from image_curation import process_listings_ready_for_image_processing, process_primary_image_verification
from special_avails import process_one_special_avail_with_active_listings,process_one_special_avail_matching
import os
from dotenv import load_dotenv
load_dotenv()
import json
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
from config_runtime import get_whatsapp_send_mode

import uvicorn  # NEW: FastAPI server

# from http.server import BaseHTTPRequestHandler, HTTPServer

START_TIME = time.time()  # for uptime calculation
os.environ["APP_START_TIME"] = str(START_TIME)  # used by api_app for consistent uptime


# class StatusHandler(BaseHTTPRequestHandler):
#     def do_GET(self):
#         # Support both /server-sattus (as you requested) and /server-status (correct spelling)
#         if self.path in ("/server-sattus", "/server-status"):
#             self.send_response(200)
#             self.send_header("Content-Type", "application/json")
#             self.end_headers()

#             response = {
#                 "status": "working",
#                 "uptime_seconds": int(time.time() - START_TIME),
#             }

#             self.wfile.write(json.dumps(response).encode("utf-8"))
#         else:
#             self.send_response(404)
#             self.end_headers()

#     def do_POST(self):
#         if self.path == "/config/whatsapp-mode":
#             length = int(self.headers.get("Content-Length", "0") or 0)
#             body = self.rfile.read(length).decode("utf-8") if length else "{}"
#             try:
#                 data = json.loads(body or "{}")
#                 mode = (data.get("mode") or "").strip().lower()
#                 set_whatsapp_send_mode(mode)   # validates + persists
#                 self.send_response(200)
#                 self.send_header("Content-Type", "application/json")
#                 self.end_headers()
#                 self.wfile.write(json.dumps({"ok": True, "mode": get_whatsapp_send_mode()}).encode("utf-8"))
#             except ValueError as ve:
#                 self.send_response(400)
#                 self.send_header("Content-Type", "application/json")
#                 self.end_headers()
#                 self.wfile.write(json.dumps({"ok": False, "error": str(ve)}).encode("utf-8"))
#             except Exception as e:
#                 self.send_response(500)
#                 self.send_header("Content-Type", "application/json")
#                 self.end_headers()
#                 self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
#         else:
#             self.send_response(404)
#             self.end_headers()

#     # Avoid default noisy logging to stderr
#     def log_message(self, format, *args):
#         logging.info("StatusHandler: " + format % args)


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
def run_process_primary_image_verification():
    logging.info("process_primary_image_verification")
    process_primary_image_verification(limit=5, model="gpt-5.1")


@repeat(every(2).minutes)
def run_make_whatsapp_posts_from_ready_to_post():
    logging.info("make_whatsapp_posts_from_ready_to_post")
    make_whatsapp_posts_from_ready_to_post("ad_post_rules.txt", limit=5)

@repeat(every(1).minutes)
def run_process_whatsapp_queue():
    logging.info("process_whatsapp_queue")
    process_whatsapp_queue(limit=5)

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

@repeat(every(3).minutes)
def run_direct_wholeseller_linking():
    logging.info("run_direct_wholeseller_linking")
    # default 3; you can change to 5 if you want to push harder:
    process_direct_wholeseller_batch(batch_limit=5)

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

@repeat(every(3).minutes)
def run_process_one_special_avail_with_active_listings():
    logging.info("run_process_one_special_avail_with_active_listings")
    # default 3; you can change to 5 if you want to push harder:
    process_one_special_avail_with_active_listings()

@repeat(every(5).minutes)
def run_process_one_special_avail_matching():
    logging.info("run_process_one_special_avail_matching")
    # default 3; you can change to 5 if you want to push harder:
    process_one_special_avail_matching()

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

def start_api_server(host: str = "0.0.0.0", port: int = 8000):
    # Serve FastAPI app in this thread
    uvicorn.run("api_app:app", host=host, port=port, log_level="info")

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
    api_thread = threading.Thread(
        target=start_api_server,
        kwargs={"host": "0.0.0.0", "port": status_port},
        daemon=True,
    )
    api_thread.start()
    logging.info("FastAPI status available at http://0.0.0.0:%s/server-status", status_port)

    # Main scheduler loop
    while True:
        run_pending()
        time.sleep(1)
