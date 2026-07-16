# server_runner.py
import sys
import time
import logging
import os
import threading
from functools import wraps

from schedule import every, repeat, run_pending
from dotenv import load_dotenv
import uvicorn

# import your task entrypoints
from ingestion.gmail import process_account, _ensure_paths, ACCOUNTS, build_service_by_account
from pipeline.process_email import process_pending, reset_stale_processing_emails
from pipeline.dedup import process_not_processed_with_duplicate_rule
from ai.rules_runner import apply_ai_english_rules
from pipeline.post_selection import select_passed_listings_for_post
from ai.whatsapp_posts import make_whatsapp_posts_from_ready_to_post
from integrations.wordpress.ai_mapper import ai_build_wp_payload_for_posted
from integrations.wordpress.ai_property_description import ai_build_wp_property_description_for_posted
from integrations.wordpress.sync_poster import sync_wp_for_descriptions
from ai.media_verify import verify_and_fill_missing_media_for_not_processed
from integrations.wordpress.price_media_updates import process_wp_price_and_media_updates
from ingestion.forward_completed import forward_completed_source_emails
from whatsapp.sender import process_whatsapp_queue
from db.mongo_engine_conn import init_db
from models import (
    FilteredListingEmail,
    ParsedListing,
    ScrapingList,
    WebFormBuyerSubmission,
    ListingPipelineMetric,
    BuyerDealEmailSend,
    BuyerEmailBounceJobRun,
)
from models.special_avail_list import SpecialAvailList
from integrations.podio.direct_wholesaler import process_direct_wholeseller_batch
from whatsapp.keepalive import send_keepalive_template, parse_recipients_env

from ai.image_curation import process_listings_ready_for_image_processing, process_primary_image_verification
from special_avails.processor import process_one_special_avail_with_active_listings, process_one_special_avail_matching
from buyers.matching_api import process_pending_buyer_matching_batch
from buyers.matched_process import process_pending_buyer_descriptions, process_buyer_sends
from buyers.email_bounce_check import check_yesterday_buyer_email_bounces

from core.paths import data_path

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# from http.server import BaseHTTPRequestHandler, HTTPServer

START_TIME = time.time()  # for uptime calculation
os.environ["APP_START_TIME"] = str(START_TIME)  # used by api_app for consistent uptime
BUYER_MATCHING_CRON_MINUTES = int(os.getenv("BUYER_MATCHING_CRON_MINUTES", "3"))
BUYER_EMAIL_BOUNCE_CHECK_TIME = os.getenv("BUYER_EMAIL_BOUNCE_CHECK_TIME", "06:00")


def safe_scheduled_job(fn):
    """Catch and log exceptions; skip if a previous run of the same job is still in progress."""
    lock = threading.Lock()

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not lock.acquire(blocking=False):
            logging.warning("%s: skipped (previous run still in progress)", fn.__name__)
            return
        try:
            try:
                return fn(*args, **kwargs)
            except Exception:
                logging.exception("%s: crashed", fn.__name__)
        finally:
            lock.release()

    return wrapper


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
@safe_scheduled_job
def run_gmail_job():
    gmail_fetch_all()

# schedule parse email to create listing every minute
@repeat(every(1).minutes)
@safe_scheduled_job
def run_process_email():
    logging.info("run_process_email")
    process_pending()

@repeat(every(3).minutes)
@safe_scheduled_job
def run_verify_and_fill_missing_media_for_not_processed():
    logging.info("verify_and_fill_missing_media_for_not_processed")
    verify_and_fill_missing_media_for_not_processed(limit=35, max_workers=8)

@repeat(every(2).minutes)
@safe_scheduled_job
def run_process_wp_price_and_media_updates():
    logging.info("process_wp_price_and_media_updates")
    process_wp_price_and_media_updates(limit=5)

# schedule parse email to create listing every 5 minute
@repeat(every(1).minutes)
@safe_scheduled_job
def run_process_dup30days():
    logging.info("process_dup30days")
    process_not_processed_with_duplicate_rule()


@repeat(every(5).minutes)
@safe_scheduled_job
def run_ai_nl_rules_runner():
    logging.info("ai_nl_rules_runner")
    apply_ai_english_rules(str(data_path("ai_listing_rules.yaml")), limit=50)


@repeat(every(10).minutes)
@safe_scheduled_job
def run_select_passed_listings_for_post():
    logging.info("select_passed_listings_for_post")
    select_passed_listings_for_post(limit=200, sort_by="created_at", mark_ready_status=None)


@repeat(every(2).minutes)
@safe_scheduled_job
def run_process_listings_ready_for_image_processing():
    logging.info("process_listings_ready_for_image_processing")
    process_listings_ready_for_image_processing(limit=5)

@repeat(every(2).minutes)
@safe_scheduled_job
def run_process_primary_image_verification():
    logging.info("process_primary_image_verification")
    process_primary_image_verification(limit=5, model="gpt-5.6-luna")


@repeat(every(2).minutes)
@safe_scheduled_job
def run_make_whatsapp_posts_from_ready_to_post():
    logging.info("make_whatsapp_posts_from_ready_to_post")
    make_whatsapp_posts_from_ready_to_post(str(data_path("ad_post_rules.txt")), limit=5)

@repeat(every(1).minutes)
@safe_scheduled_job
def run_process_whatsapp_queue():
    logging.info("process_whatsapp_queue")
    process_whatsapp_queue(limit=5)

@repeat(every(3).minutes)
@safe_scheduled_job
def run_ai_build_wp_payload_for_posted():
    logging.info("ai_build_wp_payload_for_posted")
    ai_build_wp_payload_for_posted(limit=5)


@repeat(every(3).minutes)
@safe_scheduled_job
def run_ai_build_wp_property_description_for_posted():
    logging.info("ai_build_wp_property_description_for_posted")
    ai_build_wp_property_description_for_posted(limit=5, batch_size=10, per_item_sleep_s=0.2)

@repeat(every(5).minutes)
@safe_scheduled_job
def run_sync_wp_for_descriptions():
    logging.info("sync_wp_for_descriptions")
    result = sync_wp_for_descriptions(limit=5, per_item_sleep_s=0.2)
    logging.info("run_sync_wp_for_descriptions: result=%s", result)

@repeat(every(3).minutes)
@safe_scheduled_job
def run_direct_wholeseller_linking():
    logging.info("run_direct_wholeseller_linking")
    # default 3; you can change to 5 if you want to push harder:
    process_direct_wholeseller_batch(batch_limit=5)

@repeat(every(15).minutes)
@safe_scheduled_job
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
@safe_scheduled_job
def run_process_one_special_avail_with_active_listings():
    logging.info("run_process_one_special_avail_with_active_listings")
    # default 3; you can change to 5 if you want to push harder:
    process_one_special_avail_with_active_listings()

@repeat(every(5).minutes)
@safe_scheduled_job
def run_process_one_special_avail_matching():
    logging.info("run_process_one_special_avail_matching")
    # default 3; you can change to 5 if you want to push harder:
    process_one_special_avail_matching()

@repeat(every(2).hours)
@safe_scheduled_job
def run_reset_stale_processing_emails():
    logging.info("reset_stale_processing_emails")
    reset_stale_processing_emails()

@repeat(every(BUYER_MATCHING_CRON_MINUTES).minutes)
@safe_scheduled_job
def run_buyer_matching_cron():
    logging.info("run_buyer_matching_cron: start")
    result = process_pending_buyer_matching_batch()
    logging.info("run_buyer_matching_cron: result=%s", result)

@repeat(every(5).minutes)
@safe_scheduled_job
def run_process_pending_buyer_descriptions():
    logging.info("process_pending_buyer_descriptions")
    process_pending_buyer_descriptions(limit=5)

@repeat(every(3).minutes)
@safe_scheduled_job
def run_process_buyer_sends():
    logging.info("process_buyer_sends")
    process_buyer_sends(limit=2)

@repeat(every().day.at(BUYER_EMAIL_BOUNCE_CHECK_TIME))
@safe_scheduled_job
def run_check_yesterday_buyer_email_bounces():
    logging.info("check_yesterday_buyer_email_bounces: start")
    result = check_yesterday_buyer_email_bounces()
    logging.info("check_yesterday_buyer_email_bounces: result=%s", result)

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
    try:
        uvicorn.run("api_app:app", host=host, port=port, log_level="info")
    except Exception:
        logging.exception("FastAPI server crashed")

if __name__ == "__main__":
    logging.info("Starting server_runner with Python: %s", sys.executable)
    logging.info("Scheduler loop started")

    # One-time DB bootstrap
    init_db()
    try:
        FilteredListingEmail.ensure_indexes()
        ParsedListing.ensure_indexes()
        WebFormBuyerSubmission.ensure_indexes()
        ScrapingList.ensure_indexes()
        SpecialAvailList.ensure_indexes()
        ListingPipelineMetric.ensure_indexes()
        BuyerDealEmailSend.ensure_indexes()
        BuyerEmailBounceJobRun.ensure_indexes()

        # gmail_fetch_all()
    except Exception:
        logging.exception("ensure_indexes failed")

    # Start status HTTP server in background
    status_port = int(os.getenv("STATUS_PORT", "8000"))
    api_thread = threading.Thread(
        name="fastapi-server",
        target=start_api_server,
        kwargs={"host": "0.0.0.0", "port": status_port},
        daemon=True,
    )
    api_thread.start()
    logging.info("FastAPI status available at http://0.0.0.0:%s/server-status", status_port)

    # Main scheduler loop
    try:
        while True:
            try:
                run_pending()
            except Exception:
                logging.exception("run_pending: crashed")
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Scheduler stopped by user (KeyboardInterrupt)")
