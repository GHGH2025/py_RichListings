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

from gmail_hourly_multi import build_service_by_account
from forward_completed_sources import forward_completed_source_emails

from mongo_engine_conn import init_db
from models import FilteredListingEmail, ParsedListing

import os
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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

# schedule parse email to create listing every 5 minute
@repeat(every(1).minutes)
def run_process_dup30days():
    logging.info("process_dup30days")
    process_not_processed_with_duplicate_rule()


@repeat(every(5).minutes)
def run_ai_nl_rules_runner():
    logging.info("ai_nl_rules_runner")
    apply_ai_english_rules("ai_listing_rules.yaml", limit=100)


@repeat(every(10).minutes)
def run_select_passed_listings_for_post():
    logging.info("select_passed_listings_for_post")
    select_passed_listings_for_post(limit=200, sort_by="created_at", mark_ready_status=None)


@repeat(every(2).minutes)
def run_make_whatsapp_posts_from_ready_to_post():
    logging.info("make_whatsapp_posts_from_ready_to_post")
    make_whatsapp_posts_from_ready_to_post("ad_post_rules.txt", limit=5)


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

    # Loop forever
    while True:
        run_pending()
        time.sleep(1)
