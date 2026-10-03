"""
Activate listings that passed the 30-day ≥6% price-drop dedup gate:
  1) Set WordPress post_status to publish, update asking_price, set REDUCED!! title
  2) Fire Podio catch webhook to mark property Active
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import requests

from models import ParsedListing
from integrations.wordpress.post_status import set_wp_post_status
from pipeline.publication_gate import apply_publication_gate

WEBHOOK_URL = os.getenv(
    "PRICE_DROP_PODIO_ACTIVE_WEBHOOK_URL",
    "https://workflow-automation.podio.com/catch/2rtkutxl47po7x7",
).strip()
WEBHOOK_TIMEOUT = int(os.getenv("PRICE_DROP_PODIO_ACTIVE_WEBHOOK_TIMEOUT", "20"))

# Blagojche 01.10 (empty-post bug): price-drop must UPDATE an existing WP post, NEVER create one.
# /create matches by EXACT posttitle; we were passing Google short-form ("Ave"/"NW") which never
# matched the long-form published post -> a new EMPTY post. Fix: look up the REAL post via the PL's
# STORED (long) address, require it to be the prev listing's post_id, reuse its EXACT title; else skip.
WP_GET_URL = f"{os.getenv('WP_API_BASE', 'https://inventory.joinbuyerslist.com/wp-json/addproperty/v1').rstrip('/')}/getproperty"
WP_API_TOKEN = os.getenv("WP_API_TOKEN")


def _stored_full_address(x) -> Optional[str]:
    """The PL's STORED address (long form, as the post was published) -> 'addr, city, ST ZIP, USA'."""
    if not x:
        return None
    parts = [str(getattr(x, "address", "") or "").strip(), str(getattr(x, "city", "") or "").strip()]
    tail = " ".join([t for t in [str(getattr(x, "state", "") or "").strip(),
                                  str(getattr(x, "zip", "") or "").strip()] if t])
    if tail:
        parts.append(tail)
    parts.append("USA")
    full = ", ".join([p for p in parts if p])
    return full or None


def _find_existing_wp_post(addr: str):
    """getproperty by address -> (post_id, exact_posttitle) when a post exists, else (None, None)."""
    if not addr:
        return None, None
    try:
        # NOTE (Blagojche 01.10): the addproperty getproperty endpoint takes the token as a QUERY
        # param (same as the existing _try_search_in_wp in sync_poster); it does not read a header/
        # body token, so the token appears in the inventory access log - accepted/known limitation.
        r = requests.get(WP_GET_URL, params={"address": addr, "token": WP_API_TOKEN}, timeout=25)
        if r.status_code != 200:
            return None, None
        data = (r.json() or {}).get("data") or []
        if data and isinstance(data[0], dict) and isinstance(data[0].get("post_id"), int):
            return data[0]["post_id"], (data[0].get("posttitle") or None)
    except Exception:
        logging.exception("price_drop: getproperty lookup failed addr=%r", addr)
    return None, None

REDUCED_TITLE_PREFIX = "<strong><span style='color: #ff6600;'>REDUCED!!</span> </strong>"
# #36 (Rich 01.10, rule A): the Today's Deals tag only for a drop of 6% or more. The WP price
# (rule B) and the reduction date _deal_date (rule C) are still updated on every drop by the plugin.
TODAYS_DEAL_MIN_DROP = float(os.getenv("TODAYS_DEAL_MIN_DROP", "0.06"))


def _now() -> datetime:
    return datetime.utcnow()


def _best_address_parts(pl: ParsedListing) -> Tuple[str, str, str, str]:
    addr = (getattr(pl, "address", None) or "").strip()
    city = (getattr(pl, "city", None) or "").strip()
    state = (getattr(pl, "state", None) or "").strip()
    zip_code = (getattr(pl, "zip", None) or "").strip()

    ci = getattr(pl, "complete_info", None) or {}
    if isinstance(ci, dict):
        if not addr:
            addr = (ci.get("address") or "").strip()
        if not city:
            city = (ci.get("city") or "").strip()
        if not state:
            state = (ci.get("state") or "").strip()
        if not zip_code:
            zip_code = (ci.get("zip") or "").strip()

    return addr, city, state, zip_code


def build_activation_address(pl: ParsedListing) -> Optional[str]:
    """
    Prefer Google formatted_address; else 'addr, city, ST zip, USA'.
    """
    geo = getattr(pl, "geo_code_response", None) or {}
    if isinstance(geo, dict):
        formatted = (geo.get("formatted_address") or "").strip()
        if formatted:
            return formatted

    addr, city, state, zip_code = _best_address_parts(pl)
    if not addr and not city:
        return None

    parts = []
    if addr:
        parts.append(addr)
    if city:
        parts.append(city)

    state_zip = " ".join(p for p in (state, zip_code) if p).strip()
    if state_zip:
        parts.append(state_zip)

    full = ", ".join(parts)
    if full and not full.upper().endswith(", USA") and not full.upper().endswith(" USA"):
        full = f"{full}, USA"
    return full or None


def _activation_price(pl: ParsedListing) -> Optional[float]:
    for raw in (getattr(pl, "price_drop_curr_price", None), getattr(pl, "price", None)):
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _format_asking_price(price: float) -> str:
    return str(int(price)) if float(price).is_integer() else str(float(price))


def _fire_podio_active_webhook(address: str) -> bool:
    if not WEBHOOK_URL:
        logging.warning("price_drop_activate: webhook URL not configured")
        return False
    try:
        resp = requests.post(
            WEBHOOK_URL,
            json={"add": address},
            headers={"Content-Type": "application/json"},
            timeout=WEBHOOK_TIMEOUT,
        )
        ok = resp.status_code in (200, 201, 202)
        if not ok:
            logging.warning(
                "price_drop_activate: webhook non-2xx status=%s body=%s",
                resp.status_code,
                (resp.text or "")[:300],
            )
        return ok
    except requests.RequestException:
        logging.exception("price_drop_activate: webhook failed for %s", address)
        return False


def process_price_drop_activations(limit: int = 50) -> Dict[str, Any]:
    """
    For each listing with price_drop_pass and not yet activated:
      - set WP post_status to publish, update asking_price, set REDUCED!! custom_title
      - fire Podio Active webhook
      - mark price_drop_activated when both succeed
    """
    checked = activated = wp_ok = podio_ok = failed = skipped_no_addr = skipped_no_price = 0

    candidates = (
        apply_publication_gate(ParsedListing.objects(
            price_drop_pass=True,
            price_drop_activated__ne=True,
        ))
        .order_by("updated_at")
        .limit(limit)
    )

    for pl in candidates:
        checked += 1
        address = build_activation_address(pl)
        if not address:
            skipped_no_addr += 1
            pl.update(
                set__price_drop_activate_error="no address available for activation",
                set__updated_at=_now(),
            )
            failed += 1
            continue

        price = _activation_price(pl)
        if price is None:
            skipped_no_price += 1
            pl.update(
                set__price_drop_activate_error="no price available for WP reduction update",
                set__updated_at=_now(),
            )
            failed += 1
            continue

        # A2 (28.09, title-fixed 30.09 per Blagojche): use the PREV post's address so /create's
        # fuzzy match hits the existing post (the record's address is often WhatsApp-mangled -> a
        # duplicate 52408), AND use the PREV street for the REDUCED!! title so it doesn't show a
        # broken address.
        _prev_id = getattr(pl, "price_drop_prev_id", None)
        _prev_street = None
        if _prev_id:
            _prev = ParsedListing.objects(id=_prev_id).only(
                "address", "city", "state", "zip", "geo_code_response", "post_id").first()
            if _prev:
                _prev_addr = build_activation_address(_prev)
                if _prev_addr:
                    address = _prev_addr
                _ps, _, _, _ = _best_address_parts(_prev)
                if _ps:
                    _prev_street = _ps
        street, _, _, _ = _best_address_parts(pl)
        if _prev_street:
            street = _prev_street
        title_address = street or address
        asking_price = _format_asking_price(price)
        custom_title = f"{REDUCED_TITLE_PREFIX} {title_address}"

        # Blagojche 01.10: find the REAL post via the STORED long address + prev listing post_id;
        # reuse its EXACT title so /create UPDATES it. Never create (empty-post bug).
        _expected_pid = (getattr(_prev, "post_id", None) if _prev else None) or getattr(pl, "post_id", None)
        _match_pid = _match_title = None
        _found_any = None   # a post exists for the address, even if its id != expected (mismatch)
        for _cand in (_stored_full_address(pl), (_stored_full_address(_prev) if _prev else None)):
            if not _cand:
                continue
            _gp_pid, _gp_title = _find_existing_wp_post(_cand)
            if _gp_pid:
                _found_any = (_gp_pid, _gp_title)
                if _gp_title and _expected_pid and int(_gp_pid) == int(_expected_pid):
                    _match_pid, _match_title = _gp_pid, _gp_title
                    break
        if not _match_title:
            # Blagojche 01.10 follow-up: never create, AND never retry forever.
            if _found_any:
                # a DIFFERENT post occupies this address -> genuine mismatch. Stop (one error log,
                # drop price-drop ownership so it is not re-picked every 2 min). Do NOT requeue - the
                # address already has a post, so the poster must not create another.
                logging.error("price_drop MISMATCH: address post %s != expected %s id=%s -> stop retry",
                              _found_any[0], _expected_pid, pl.id)
                pl.update(set__price_drop_pass=False,
                          set__price_drop_activate_error="address matched a different post (%s != %s); stopped" % (_found_any[0], _expected_pid),
                          set__updated_at=_now())
            else:
                # expected post is gone (404) / no post for the address -> hand back to the poster as a
                # NEW listing (A3): drop price-drop ownership + requeue des_generated so sync_poster
                # re-posts it (with its own masked/no-house + dup gates). Stops the 2-min retry loop.
                logging.warning("price_drop: expected post %s missing (404) id=%s -> requeue to poster (des_generated)",
                                _expected_pid, pl.id)
                pl.update(set__price_drop_pass=False, set__wp_status="des_generated",
                          set__price_drop_activate_error="expected post missing (404); requeued to poster",
                          set__updated_at=_now())
            failed += 1
            continue

        # #35 (Rich 02.10): the REDUCED!! title must carry the FULL address (street, city, state
        # zip) - since the 30.09 title change it showed the street alone. Use the real post's own
        # exact title (the same text every non-reduced deal shows).
        if _match_title and str(_match_title).strip():
            custom_title = f"{REDUCED_TITLE_PREFIX} {str(_match_title).strip()}"

        # #36: tag Today's Deals only when the drop is >= TODAYS_DEAL_MIN_DROP (6%).
        try:
            _tdq = float(getattr(pl, "price_drop_pct", 0) or 0) >= TODAYS_DEAL_MIN_DROP
        except Exception:
            _tdq = False
        wp_success, wp_status, wp_payload = set_wp_post_status(
            _match_title,
            "publish",
            asking_price=asking_price,
            custom_title=custom_title,
            address=_match_title,
            newest_deals=(["Todays Deal"] if _tdq else None),
        )
        if wp_success and isinstance(wp_payload, dict):
            _ret_pid = wp_payload.get("post_id")
            if _ret_pid is not None and int(_ret_pid) != int(_match_pid):
                logging.error("price_drop ALARM: /create returned post_id=%s != real %s id=%s addr=%r "
                              "(should never happen - title matched)", _ret_pid, _match_pid, pl.id, address)
        if not wp_success:
            err = f"wp_publish_failed status={wp_status} detail={str(wp_payload)[:200]}"
            if str(wp_status) == "404":
                # A1v2 follow-up (30.09 per Blagojche): the existing post is gone (404). A1 already
                # marked the record already_found so the poster won't create it, and the price-drop
                # path can't update a missing post -> the deal would be lost. Hand it back to the
                # poster as a NEW listing: drop price-drop ownership + requeue for /create. 404 only.
                pl.update(
                    set__price_drop_pass=False,
                    set__wp_status="des_generated",
                    set__price_drop_activate_error=err,
                    set__updated_at=_now(),
                )
                failed += 1
                continue
            pl.update(
                set__price_drop_activate_error=err,
                set__updated_at=_now(),
            )
            failed += 1
            continue

        wp_ok += 1
        wp_at = _now()

        podio_success = _fire_podio_active_webhook(address)
        if not podio_success:
            pl.update(
                set__price_drop_wp_public_at=wp_at,
                set__price_drop_activate_error="podio_active_webhook_failed",
                set__updated_at=_now(),
            )
            failed += 1
            continue

        podio_ok += 1
        now = _now()
        prev_price = getattr(pl, "price_drop_prev_price", None)
        update_fields: Dict[str, Any] = {
            "set__price_drop_wp_public_at": wp_at,
            "set__price_drop_podio_webhook_at": now,
            "set__price_drop_activated": True,
            "set__price_drop_activated_at": now,
            "set__price_drop_activate_error": None,
            "set__wp_check_reduced": "updated",
            "set__wp_check_new_price": float(price),
            "set__updated_at": now,
        }
        if prev_price is not None:
            try:
                update_fields["set__wp_check_prev_price"] = float(prev_price)
            except (TypeError, ValueError):
                pass
        pl.update(**update_fields)
        activated += 1

        # --- #26b (Blagojche 02.10): these price-drop PLs are OWNED here (A1 guard in
        # sync_poster skips them with `continue` BEFORE _defer_link_existing_podio), so the
        # item-reuse + buyer rematch must happen HERE. Record the real post_id, reuse the
        # sibling post's Podio item, then (gated by DEFER_PRICE_DROP_REMATCH + >= MIN_PCT,
        # once-per-drop) set pending+rematch so buyers get the NEW price (match_buyers reads
        # price from this PL). Zero new Podio items.
        try:
            _mpid = int(_match_pid)
            _sib = (ParsedListing.objects(post_id=_mpid, buyer_matching_podio_item_id__ne=None,
                                          id__ne=pl.id)
                    .only("buyer_matching_podio_item_id").order_by("-updated_at").first())
            _set = {"set__post_id": _mpid, "set__updated_at": _now()}
            if _sib and getattr(_sib, "buyer_matching_podio_item_id", None):
                _set["set__buyer_matching_podio_item_id"] = int(_sib.buyer_matching_podio_item_id)
            pl.update(**_set)
            pl.reload()
            # Blagojche 02.10: rematch ONLY if a sibling item was actually reused. Without an
            # item, run_buyer_matching_cron does left_pending+continue forever -> pending piles
            # up and never matches. No item -> log and stop (no pending, no email).
            if "set__buyer_matching_podio_item_id" in _set:
                from integrations.wordpress.sync_poster import _defer_maybe_rematch_price_drop
                _defer_maybe_rematch_price_drop(pl, _mpid)
            else:
                logging.info("price_drop 26b: no sibling item post=%s id=%s - rematch skipped", _mpid, getattr(pl, "id", None))
        except Exception:
            logging.exception("price_drop #26b rematch hookup failed id=%s", getattr(pl, "id", None))

    return {
        "checked": checked,
        "activated": activated,
        "wp_ok": wp_ok,
        "podio_ok": podio_ok,
        "failed": failed,
        "skipped_no_addr": skipped_no_addr,
        "skipped_no_price": skipped_no_price,
    }
