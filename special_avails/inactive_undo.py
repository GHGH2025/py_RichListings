#!/usr/bin/env python3
"""Undo a Special Avails inactive-hide apply run (2026-10-15).

Reads a snapshot JSON written by inactive_processor.py (SPECIAL_AVAIL_INACTIVE_SNAPSHOT_DIR)
and reverses every item it fired:
  1) Podio: re-activate the item (Status=Active, clear Marked-Non-Active-By-AI, reset Not-Found
     counter) via the existing direct_takeover_reactivate - the exact reverse of the fire webhook.
  2) WordPress: restore the post to 'publish' (only if this run actually privated it).
  3) Tracker: clear webhook_fired_at / webhook_ok / wp_private_*, restore status, so the item is
     re-evaluated normally on the next run.

Read-only on everything it did not itself touch. Usage:
  cd ~/apps/RichListings && venv/bin/python special_avails/inactive_undo.py <snapshot.json> [--dry]
"""
import json
import os
import sys

# Make the project root importable when run directly (so `integrations.*` resolves).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    if not args:
        sys.exit("usage: inactive_undo.py <snapshot.json> [--dry]")
    path = args[0]
    snap = json.load(open(path))
    items = snap.get("items", [])
    print(f"undo: {len(items)} item(s) from {path}  dry={dry}")

    from pymongo import MongoClient
    db = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=8000)[os.environ.get("MONGO_DB", "aiDb")]
    trackers = db["special_avail_inactive_trackers"]

    from integrations.wordpress.post_status import set_wp_post_status
    from integrations.podio.direct_wholesaler import _direct_takeover_reactivate, get_podio_access_token

    token = None
    for it in items:
        pid = it.get("podio_item_id")
        addr = it.get("address")
        res = {"podio_item_id": pid, "address": addr}

        # 1) Podio re-activate
        if dry:
            res["podio"] = "DRY_reactivate"
        else:
            try:
                token = token or get_podio_access_token()
                res["podio"] = "reactivated" if _direct_takeover_reactivate(token, pid) else "reactivate_FAILED"
            except Exception as e:
                res["podio"] = f"reactivate_ERR:{e}"

        # 2) WP restore to publish (only if we privated it this run and it was publish before)
        if it.get("wp_privated") and it.get("wp_status_before") == "publish":
            if dry:
                res["wp"] = "DRY_publish"
            else:
                try:
                    ok, sc, _ = set_wp_post_status(addr, "publish")
                    res["wp"] = f"publish({sc})" if ok else f"publish_FAILED({sc})"
                except Exception as e:
                    res["wp"] = f"publish_ERR:{e}"
        else:
            res["wp"] = "skip(not-privated-by-run)"

        # 3) tracker reset
        if dry:
            res["tracker"] = "DRY_reset"
        else:
            r = trackers.update_one(
                {"podio_item_id": pid},
                {"$set": {
                    "status": it.get("status_before") or "watching",
                    "webhook_fired_at": None,
                    "webhook_ok": None,
                    "wp_private_ok": None,
                    "wp_private_at": None,
                    "would_fire_at": None,
                }},
            )
            res["tracker"] = f"reset(matched={r.matched_count})"
        print("  ", res)
    print("undo done")


if __name__ == "__main__":
    main()
