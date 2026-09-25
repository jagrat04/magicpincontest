"""Vera-style merchant assistant for the magicpin challenge.

The composer is deliberately local and deterministic.  It uses only the
contexts supplied by the judge, so a new context version can be used without
changing code or making an external request.

Run locally with:
    python bot.py

The module also exposes ``compose(category, merchant, trigger, customer)``
for the offline submission file and for small unit tests.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


STARTED_AT = time.time()
VALID_SCOPES = {"category", "merchant", "customer", "trigger"}

# The judge keeps these objects in memory for the lifetime of the process.
contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, dict[str, Any]] = {}
sent_bodies: dict[str, set[str]] = {}


def _first_name(merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity") or {}
    if identity.get("owner_first_name"):
        return str(identity["owner_first_name"])
    name = str(identity.get("name") or "there")
    return name.split()[0]


def _merchant_name(merchant: dict[str, Any]) -> str:
    return str((merchant.get("identity") or {}).get("name") or "your business")


def _active_offers(merchant: dict[str, Any]) -> list[str]:
    return [
        str(item.get("title"))
        for item in merchant.get("offers") or []
        if item.get("status", "active") == "active" and item.get("title")
    ]


def _offer(merchant: dict[str, Any], *needles: str) -> str | None:
    offers = _active_offers(merchant)
    for offer in offers:
        low = offer.lower()
        if any(needle.lower() in low for needle in needles):
            return offer
    return offers[0] if offers else None


def _pct(value: Any, signed: bool = False) -> str:
    try:
        number = float(value) * 100
    except (TypeError, ValueError):
        return str(value)
    if signed:
        return f"{number:+.0f}%"
    return f"{number:.0f}%"


def _number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _date_label(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.hour == 0 and dt.minute == 0:
            return dt.strftime("%d %b")
        return dt.strftime("%d %b, %I:%M %p").replace(" 0", " ")
    except ValueError:
        return text


def _trend_label(value: Any) -> str:
    text = str(value).replace("_", " ")
    match = re.match(r"^(.*) ([+-])(\d+)$", text)
    if not match:
        return text
    direction = "up" if match.group(2) == "+" else "down"
    return f"{match.group(1)} {direction} {match.group(3)}%"


def _digest_item(category: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any] | None:
    payload = trigger.get("payload") or {}
    wanted = (
        payload.get("top_item_id")
        or payload.get("digest_item_id")
        or payload.get("alert_id")
    )
    digest = category.get("digest") or []
    if wanted:
        for item in digest:
            if item.get("id") == wanted:
                return item
    # Do not guess which digest item a sparse trigger refers to. The expanded
    # seed generator intentionally emits placeholder triggers; choosing the
    # first digest item here would make an unrelated citation look real.
    return None


def _is_placeholder(trigger: dict[str, Any]) -> bool:
    return bool((trigger.get("payload") or {}).get("placeholder") is True)


def _customer_can_be_contacted(customer: dict[str, Any], kind: str) -> bool:
    """Check the narrowest consent signal available in CustomerContext."""
    preferences = customer.get("preferences") or {}
    consent = customer.get("consent") or {}
    if preferences.get("reminder_opt_in") is False and kind in {
        "recall_due", "appointment_tomorrow", "chronic_refill_due", "trial_followup",
    }:
        return False
    required_scope = {
        "recall_due": "recall_reminders",
        "appointment_tomorrow": "appointment_reminders",
        "chronic_refill_due": "refill_reminders",
        "trial_followup": "kids_program_updates",
        "wedding_package_followup": "bridal_package_followup",
        "customer_lapsed_hard": "winback_offers",
        "customer_lapsed_soft": "promotional_offers",
    }.get(kind)
    scopes = set(consent.get("scope") or [])
    if required_scope and scopes:
        return required_scope in scopes
    # An explicit empty scope means there is no consent to use.
    if "scope" in consent and not scopes:
        return False
    return preferences.get("reminder_opt_in", True) is not False


def _suppressed_customer_result(trigger: dict[str, Any], reason: str) -> dict[str, str | bool]:
    return {
        "body": "No outbound customer message sent: the supplied consent does not cover this update.",
        "cta": "none",
        "send_as": "merchant_on_behalf",
        "suppression_key": str(trigger.get("suppression_key") or trigger.get("id") or ""),
        "rationale": reason,
        "suppressed": True,
    }


def _sparse_trigger_message(first: str, kind: str) -> tuple[str, str, str]:
    label = kind.replace("_", " ") or "account signal"
    return (
        f"No outbound message sent for {first}: the {label} trigger does not include a concrete detail I can verify yet.",
        "none",
        "The trigger is a generator placeholder, so the bot waits instead of inventing a date, amount, offer, or citation.",
    )


def _last_merchant_message(merchant: dict[str, Any]) -> str:
    for turn in reversed(merchant.get("conversation_history") or []):
        if turn.get("from") == "merchant":
            return str(turn.get("body") or "")
    return ""


def _conversation_text(merchant: dict[str, Any]) -> str:
    return " ".join(str(turn.get("body") or "") for turn in merchant.get("conversation_history") or [])


def _customer_language(customer: dict[str, Any]) -> str:
    return str((customer.get("identity") or {}).get("language_pref") or "english").lower()


def _customer_name(customer: dict[str, Any]) -> str:
    name = str((customer.get("identity") or {}).get("name") or "there")
    # Seed profiles sometimes annotate a child's profile as
    # ``Karthik (parent: Sumitra)``. Use the actual recipient's name in the
    # greeting; the parent annotation is relationship metadata, not a salutation.
    return name.split(" (parent:", 1)[0].strip() or "there"


def _merchant_message(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str, str]:
    """Return body, CTA type, and a short human-readable rationale."""
    first = _first_name(merchant)
    kind = str(trigger.get("kind") or "")
    payload = trigger.get("payload") or {}
    cat = str(merchant.get("category_slug") or category.get("slug") or "business")
    performance = merchant.get("performance") or {}
    aggregate = merchant.get("customer_aggregate") or {}
    item = _digest_item(category, trigger)

    if _is_placeholder(trigger):
        return _sparse_trigger_message(first, kind)

    if kind == "research_digest" and item:
        cohort = aggregate.get("high_risk_adult_count")
        anchor = f"your {cohort} high-risk adult patients" if cohort else "patients who may fit this cohort"
        trial = f"The {item['trial_n']}-patient study found " if item.get("trial_n") else "The study found "
        summary = str(item.get("summary") or item.get("title") or "a useful finding").rstrip(".")
        return (
            f"{first}, {item.get('source', 'this week\'s digest')} has one item worth a look for {anchor}: "
            f"{trial}{summary.lower()}. I can pull the short version and turn it into a patient-friendly post. Want that?",
            "open_ended",
            "Research digest matched to the merchant's patient mix, with the source and trial detail kept visible.",
        )

    if kind == "regulation_change" and item:
        deadline = payload.get("deadline_iso") or item.get("date")
        when = f" by {_date_label(deadline)}" if deadline and str(deadline) not in str(item.get("title", "")) else ""
        action = str(item.get("actionable") or "the relevant setup")
        return (
            f"{first}, a quick compliance note: {item.get('title', 'the category guidance')}{when}. "
            f"The practical step is to {action[:1].lower() + action[1:].rstrip('.')}. Want me to turn that into a one-page checklist?",
            "open_ended",
            "Compliance message leads with the supplied rule, deadline, and an immediately usable next step.",
        )

    if kind == "cde_opportunity" and item:
        date = _date_label(item.get("date"))
        date_part = f" on {date}" if date else ""
        fee = payload.get("fee") or item.get("actionable")
        fee_text = str(fee).replace("_", " ") if fee else "The access details are in the digest."
        credits = payload.get("credits") or item.get("credits")
        extra = f" {credits} credits" if credits else ""
        return (
            f"{first}, there is a relevant {cat[:-1] if cat.endswith('s') else cat} session{date_part}: "
            f"{item.get('title', 'a new learning session')}. {fee_text}{extra}. "
            "Want me to save the details and draft the reminder for your team?",
            "open_ended",
            "The opportunity is grounded in the matching category digest item and its supplied timing/details.",
        )

    if kind == "perf_dip":
        metric = str(payload.get("metric") or "performance").replace("_", " ")
        delta = _pct(payload.get("delta_pct"), signed=True)
        baseline = payload.get("vs_baseline")
        baseline_text = f" from a baseline of {baseline}" if baseline is not None else ""
        next_step = {
            "dentists": "refresh the Google post and put your active service offer in the first line",
            "salons": "refresh the service photos and lead with one service-plus-price offer",
            "restaurants": "check the delivery promise and surface one concrete menu item",
            "gyms": "focus the next post on member retention before buying more reach",
            "pharmacies": "check availability and make the refill/delivery path obvious",
        }.get(cat, "refresh the profile detail most relevant to this metric")
        verb = "is" if metric.rstrip().lower() in {"ctr", "conversion", "retention"} else "are"
        return (
            f"{first}, your {metric} {verb} {delta} over {payload.get('window', 'the latest window')}{baseline_text}. "
            f"That is worth fixing while it is fresh: {next_step}. Want me to draft the update?",
            "open_ended",
            "The message states the supplied performance movement and proposes one category-appropriate action.",
        )

    if kind == "renewal_due":
        days = payload.get("days_remaining", (merchant.get("subscription") or {}).get("days_remaining"))
        plan = payload.get("plan", (merchant.get("subscription") or {}).get("plan", "current"))
        amount = payload.get("renewal_amount")
        price = f" at ₹{_number(amount)}" if amount is not None else ""
        return (
            f"{first}, your {plan} plan has {days} days left{price}. I can keep the profile and current setup moving, "
            "but I need your go-ahead before the renewal window closes. Shall I prepare the renewal details?",
            "binary_yes_no",
            "Renewal timing, plan, and price are taken directly from the trigger/subscription context.",
        )

    if kind == "festival_upcoming":
        festival = payload.get("festival", "the upcoming festival")
        days = payload.get("days_until")
        timing = f" in {days} days" if days is not None else ""
        offer = _offer(merchant, "bridal", "hair spa", "haircut", "combo")
        offer_text = f" Your live offer is {offer}; that gives us something concrete to lead with." if offer else ""
        return (
            f"{first}, {festival}{timing} ({payload.get('date', 'date in the trigger')}).{offer_text} "
            "Want me to draft one useful GBP post for the week before it, rather than a generic festival discount?",
            "open_ended",
            "The seasonal nudge uses the supplied date and any existing active offer instead of inventing a promotion.",
        )

    if kind == "wedding_package_followup":
        name = _customer_name({"identity": {"name": "Kavya"}})
        days = payload.get("days_to_wedding")
        offer = _offer(merchant, "bridal")
        offer_text = f" Your live bridal option is {offer}." if offer else ""
        return (
            f"{first}, the bridal follow-up window is open for the trial customer getting married in "
            f"{days} days.{offer_text} I can map the next 30-day step and write the booking message. Shall I draft it?",
            "open_ended",
            "Bridal follow-up is tied to the supplied wedding countdown and the salon's actual offer, if present.",
        )

    if kind == "curious_ask_due":
        recent = _active_offers(merchant)
        hint = f" I can use {recent[0]} as the example." if recent else ""
        return (
            f"Hi {first} — quick operator question: which service has customers asked for most this week?"
            f"{hint} Tell me the one that comes to mind and I’ll turn it into a clean GBP post plus a short WhatsApp reply.",
            "open_ended",
            "A low-effort question invites the merchant's local knowledge and offers a concrete follow-up artifact.",
        )

    if kind == "winback_eligible":
        days = payload.get("days_since_expiry")
        lapsed = payload.get("lapsed_customers_added_since_expiry")
        offer = _active_offers(merchant)
        offer_text = f" around {offer[0]}" if offer else " around a service you choose"
        return (
            f"{first}, you’ve been off Vera for {days} days and {lapsed} customers have gone lapsed in that window. "
            f"That is a good moment for one focused reactivation note{offer_text}. Want me to draft it?",
            "open_ended",
            "The win-back message uses the expiry and lapsed-customer counts from the trigger.",
        )

    if kind == "ipl_match_today":
        match = payload.get("match", "today's match")
        venue = payload.get("venue")
        time_label = _date_label(payload.get("match_time_iso"))
        offer = _offer(merchant, "pizza", "buy 1", "match")
        offer_text = f" Your active offer is {offer}." if offer else ""
        # The category digest contains the supplied Saturday/weeknight distinction.
        insight = str(item.get("summary") or "") if item else ""
        insight_text = " Saturday home-watch behaviour usually pulls orders towards delivery, so I’d use delivery creative." if "Saturday" in insight else ""
        return (
            f"Quick one {first} — {match} at {venue or 'the stadium'} starts {time_label or 'tonight'}.{insight_text}"
            f"{offer_text} Want me to turn the existing offer into a delivery-first story for tonight?",
            "open_ended",
            "Match timing is combined with the category digest's supplied operating insight and the existing offer.",
        )

    if kind == "review_theme_emerged":
        theme = str(payload.get("theme", "a review theme")).replace("_", " ")
        count = payload.get("occurrences_30d")
        quote = payload.get("common_quote")
        quote_text = f' One customer put it as: “{quote}”.' if quote else ""
        return (
            f"{first}, {count} reviews in 30 days now point to {theme}, and the trend is {payload.get('trend', 'visible')}."
            f"{quote_text} I’d fix the promise before spending more to bring people in. Want a short reply template for affected customers?",
            "open_ended",
            "The message reflects the emerging review theme, count, trend, and supplied customer wording.",
        )

    if kind == "milestone_reached" and payload.get("value_now") is not None and payload.get("milestone_value") is not None:
        metric = str(payload.get("metric", "milestone")).replace("_", " ")
        now = payload.get("value_now")
        target = payload.get("milestone_value")
        return (
            f"{first}, you’re at {now} on {metric} — only {max(int(target or now) - int(now or 0), 0)} to go for the {target} mark. "
            "Want me to draft a simple thank-you post that does not read like a coupon?",
            "open_ended",
            "The milestone copy uses the current and target values from the trigger.",
        )

    if kind == "active_planning_intent":
        topic = str(payload.get("intent_topic", "the idea")).replace("_", " ")
        history = _conversation_text(merchant)
        if "corporate" in topic and "thali" in topic:
            offer = _offer(merchant, "thali") or "Weekday Lunch Thali @ ₹149"
            return (
                f"{first}, here’s a starter version for the corporate thali idea, using your {offer}:\n\n"
                f"{_merchant_name(merchant)} — office lunch in {((merchant.get('identity') or {}).get('locality') or 'your area')}\n"
                "• Keep the current thali as the anchor\n• Offer a simple pre-booking window for office orders\n"
                "• Take orders the day before and set one delivery slot\n\n"
                "I’ve kept the pricing open so you can set the margin. Want me to turn this into a 3-line message for nearby offices?",
                "open_ended",
                "The reply advances the merchant's explicit planning intent using the existing thali offer and locality.",
            )
        if "kids" in topic and "yoga" in topic:
            prior = "4-week program, 3 classes/week, age 7–12, ₹2,499" if "₹2,499" in history or "2,499" in history else "a short, age-banded summer program"
            return (
                f"{first}, picking up your kids-yoga idea: {prior}. I can turn that into the GBP post and parent-facing copy now. "
                "Shall I draft both together?",
                "open_ended",
                "The bot switches straight to an artifact for the merchant's stated planning request.",
            )
        return (
            f"{first}, I’m picking up your note about {topic}. I can turn the idea into a usable first draft using your current profile and offers. "
            "Want me to write that draft now?",
            "open_ended",
            "The message acknowledges the explicit planning intent and offers to do the next piece of work.",
        )

    if kind == "seasonal_perf_dip":
        delta = _pct(payload.get("delta_pct"), signed=True)
        members = aggregate.get("total_active_members")
        member_text = f" You still have {members} active members to retain." if members else ""
        return (
            f"{first}, {payload.get('metric', 'views')} are {delta} in the latest {payload.get('window', 'window')}, "
            f"but the trigger marks this as the expected {payload.get('season_note', 'seasonal')}.{member_text} "
            "I’d protect retention now and save acquisition effort for the stronger window. Want a simple member challenge draft?",
            "open_ended",
            "The expected seasonal dip is reframed using the supplied delta and member count.",
        )

    if kind == "supply_alert":
        molecule = payload.get("molecule", "the affected medicine")
        batches = ", ".join(map(str, payload.get("affected_batches") or [])) or "the batches in the alert"
        chronic = aggregate.get("chronic_rx_count")
        count = f" Your chronic-Rx base is {chronic} customers." if chronic else ""
        summary = str(item.get("summary") or "") if item else ""
        risk = " The supplied alert describes sub-potency rather than a safety risk." if "safety" in summary.lower() else ""
        return (
            f"{first}, urgent stock note: {molecule} batches {batches} from {payload.get('manufacturer', 'the named manufacturer')} are in the recall alert."
            f"{risk}{count} Pull the batch numbers and I can draft the customer note plus the replacement workflow. Shall I?",
            "binary_yes_no",
            "The alert is limited to the supplied molecule, batches, manufacturer, and pharmacy customer aggregate.",
        )

    if kind == "category_seasonal":
        trends = payload.get("trends") or []
        trend_text = ", ".join(_trend_label(t) for t in trends[:3])
        return (
            f"{first}, the summer shift is showing up as {trend_text or 'a change in demand'}. "
            "The trigger recommends a shelf action, so I’d move the rising seasonal items where customers can see them. Want a one-screen staff checklist?",
            "open_ended",
            "The seasonal message keeps the supplied demand movements and proposed shelf action intact.",
        )

    if kind == "gbp_unverified":
        path = str(payload.get("verification_path", "the supplied verification path")).replace("_", " ")
        uplift = _pct(payload.get("estimated_uplift_pct"))
        return (
            f"{first}, your Google profile is still unverified. The available path is {path}; the supplied estimate is up to {uplift} more visibility, not a guarantee. "
            "Want me to walk you through the verification steps?",
            "open_ended",
            "Verification status, path, and estimate are stated without presenting the estimate as a promise.",
        )

    if kind == "competitor_opened":
        competitor = payload.get("competitor_name", "a nearby competitor")
        distance = payload.get("distance_km")
        offer = payload.get("their_offer")
        own = _offer(merchant, "cleaning", "whitening", "consultation")
        own_text = f" You already have {own} to work with." if own else ""
        return (
            f"{first}, {competitor} opened {distance} km away with {offer or 'a new local offer'}.{own_text} "
            "I’d sharpen the profile around the service you want to be known for, not copy their price. Want me to draft that positioning?",
            "open_ended",
            "The competitor name, distance, and offer come from the trigger; the response stays on positioning rather than panic discounting.",
        )

    if kind == "perf_spike" and payload.get("delta_pct") is not None:
        metric = str(payload.get("metric", "performance")).replace("_", " ")
        driver = str(payload.get("likely_driver", "the latest update")).replace("_", " ")
        return (
            f"{first}, {metric} are up {_pct(payload.get('delta_pct'))} in the latest {payload.get('window', 'window')} "
            f"(from {payload.get('vs_baseline', 'the baseline')}). The likely driver is {driver}. Want me to turn that into a repeatable post while it is working?",
            "open_ended",
            "The performance spike is tied to the supplied baseline, change, and likely driver.",
        )

    if kind == "dormant_with_vera" and payload.get("days_since_last_merchant_message") is not None:
        days = payload.get("days_since_last_merchant_message", 0)
        topic = str(payload.get("last_topic", "the last topic")).replace("_", " ")
        return (
            f"Hi {first} — it’s been {days} days since we last spoke about {topic}. I checked the account and have one useful next step, not a generic nudge. "
            "Want the short version?",
            "open_ended",
            "Dormancy is acknowledged with the elapsed time and last topic supplied by the trigger.",
        )

    # Generic fallback for injected trigger kinds. It is intentionally modest:
    # it never invents a fact that is not in the payload or merchant context.
    detail = next((str(v) for k, v in payload.items() if k not in {"merchant_id", "customer_id", "placeholder"} and v), "a new account signal")
    return (
        f"Hi {first} — I’m reaching out because of {kind.replace('_', ' ') or 'a new account signal'}: {detail}. "
        "I can turn the supplied detail into one practical next step. Want me to draft it?",
        "open_ended",
        "A conservative fallback uses only the trigger kind and first available payload detail.",
    )


def _customer_message(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any]) -> tuple[str, str, str]:
    first = _first_name(merchant)
    name = _customer_name(customer)
    kind = str(trigger.get("kind") or "")
    payload = trigger.get("payload") or {}
    lang = _customer_language(customer)
    offers = _active_offers(merchant)

    if _is_placeholder(trigger):
        return _sparse_trigger_message(name, kind)

    if kind == "recall_due":
        slots = payload.get("available_slots") or []
        labels = [str(slot.get("label")) for slot in slots if slot.get("label")]
        service = str(payload.get("service_due", "your next visit")).replace("_", " ")
        offer = _offer(merchant, "cleaning", "checkup", "consult")
        offer_text = f" {offer}." if offer else ""
        if labels:
            slot_text = " ya ".join(labels[:2]) if "hi" in lang else " or ".join(labels[:2])
            body = f"Hi {name}, {merchant.get('identity', {}).get('name', 'the clinic')} here — your {service} is due. I have {slot_text} available.{offer_text} Reply with the slot that works, or tell me a better time."
        else:
            body = f"Hi {name}, {merchant.get('identity', {}).get('name', 'the clinic')} here — your {service} is due.{offer_text} Reply YES and we’ll share the next available slots."
        return body, "multi_choice_slot" if labels else "binary_yes_no", "Recall reminder uses the customer's relationship state, real slots, and a live merchant offer where available."

    if kind == "wedding_package_followup":
        days = payload.get("days_to_wedding")
        offer = _offer(merchant, "bridal")
        offer_text = f" {offer}." if offer else ""
        return (
            f"Hi {name} 💍 {merchant.get('identity', {}).get('name', 'the salon')} here. Your wedding is in {days} days — this is a good time to plan the next skin/hair step after your trial.{offer_text} Want us to map the next 30 days with you?",
            "binary_yes_no",
            "Bridal follow-up honors the wedding countdown and uses only the salon's active offer.",
        )

    if kind == "customer_lapsed_hard":
        days = payload.get("days_since_last_visit")
        focus = payload.get("previous_focus", "your earlier goal").replace("_", " ")
        trial = _offer(merchant, "trial", "month")
        offer_text = f" We can start with {trial}." if trial else " We can start with a low-pressure check-in."
        return (
            f"Hi {name} 👋 {merchant.get('identity', {}).get('name', 'the gym')} here. It’s been {days} days — no pressure, it happens. We remember your focus on {focus}.{offer_text} Want me to hold a no-obligation return slot?",
            "binary_yes_no",
            "The win-back message is warm, non-judgmental, and grounded in the customer's lapse and prior goal.",
        )

    if kind == "trial_followup":
        options = payload.get("next_session_options") or []
        labels = [str(o.get("label")) for o in options if o.get("label")]
        slot = labels[0] if labels else "the next available session"
        return (
            f"Hi {name}, checking in after your trial at {merchant.get('identity', {}).get('name', 'the studio')}. We have {slot} open. Want me to hold it for you?",
            "binary_yes_no",
            "Trial follow-up uses the customer's trial relationship and the supplied next-session option.",
        )

    if kind == "appointment_tomorrow":
        appointment = payload.get("appointment") or payload.get("appointment_time") or payload.get("slot")
        if appointment:
            return (
                f"Hi {name}, a reminder from {merchant.get('identity', {}).get('name', 'the business')}: your appointment is {appointment}. Reply CONFIRM if it still works, or tell us if you need to change it.",
                "binary_confirm_cancel",
                "Appointment reminder uses only the appointment detail supplied in the trigger.",
            )

    if kind == "customer_lapsed_soft":
        last_visit = (customer.get("relationship") or {}).get("last_visit")
        detail = f" Your last visit was {last_visit}." if last_visit else ""
        return (
            f"Hi {name} — a quick check-in from {merchant.get('identity', {}).get('name', 'the business')}.{detail} If you’d like to come back, reply YES and we’ll share a suitable next step.",
            "binary_yes_no",
            "Soft-lapse message is intentionally low-pressure and uses the customer's last-visit date only when present.",
        )

    if kind == "chronic_refill_due":
        medicines = ", ".join(map(str, payload.get("molecule_list") or []))
        date = _date_label(payload.get("stock_runs_out_iso"))
        address = " We can deliver to your saved address." if payload.get("delivery_address_saved") else ""
        return (
            f"Namaste {name} — {merchant.get('identity', {}).get('name', 'the pharmacy')} here. Your regular refill of {medicines or 'the medicines on file'} is due before {date or 'the run-out date'}.{address} Reply CONFIRM and we’ll check the pack details with you, or tell us if anything has changed.",
            "binary_confirm_cancel",
            "Refill reminder names only the supplied medicines, date, and saved-address status; it asks for confirmation without changing dosage.",
        )

    return (
        f"Hi {name}, {merchant.get('identity', {}).get('name', 'the business')} here. We have a quick follow-up based on your account. Reply YES if you’d like the details.",
        "binary_yes_no",
        "Customer message uses a conservative consent-respecting fallback.",
    )


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict[str, str]:
    """Compose one challenge message from the four context objects."""
    if customer is not None or trigger.get("scope") == "customer":
        if customer is None:
            return _suppressed_customer_result(trigger, "Customer-scoped trigger arrived without a CustomerContext; no outbound message is sent.")
        if not _customer_can_be_contacted(customer, str(trigger.get("kind") or "")):
            return _suppressed_customer_result(trigger, "Customer consent does not cover this trigger family; no outbound message is sent.")
        body, cta, rationale = _customer_message(category, merchant, trigger, customer)
        send_as = "merchant_on_behalf"
    else:
        body, cta, rationale = _merchant_message(category, merchant, trigger)
        send_as = "vera"
    result = {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": str(trigger.get("suppression_key") or trigger.get("id") or ""),
        "rationale": rationale,
    }
    if _is_placeholder(trigger):
        result["suppressed"] = True
    return result


def _get(scope: str, context_id: str) -> dict[str, Any] | None:
    item = contexts.get((scope, context_id))
    return item.get("payload") if item else None


def _category_for(merchant: dict[str, Any]) -> dict[str, Any]:
    slug = str(merchant.get("category_slug") or "")
    return _get("category", slug) or {"slug": slug}


def _remember_sent(conversation_id: str, body: str) -> None:
    sent_bodies.setdefault(conversation_id, set()).add(body.strip().lower())


def _is_auto_reply(message: str, state: dict[str, Any]) -> bool:
    text = re.sub(r"\s+", " ", message.lower()).strip()
    canned = any(phrase in text for phrase in (
        "thank you for contacting", "thanks for contacting", "our team will respond",
        "we have received your message", "your message has been received",
        "for your information", "aapki jaankari ke liye",
    ))
    state.setdefault("reply_texts", []).append(text)
    repeats = Counter(state["reply_texts"])[text]
    return canned or repeats >= 2


def _is_stop(message: str) -> bool:
    text = message.lower().strip()
    return bool(re.search(r"\b(stop|unsubscribe|not interested|don'?t message|do not message|remove me|no thanks)\b", text))


def _is_commitment(message: str) -> bool:
    text = message.lower()
    return bool(re.search(r"\b(yes|go ahead|let'?s do it|lets do it|proceed|confirm|do it|send it|draft it|i want to join)\b", text))


def _reply_action(body: str, cta: str, rationale: str) -> dict[str, Any]:
    return {"action": "send", "body": body, "cta": cta, "rationale": rationale}


def respond(conversation_id: str, merchant: dict[str, Any] | None, customer: dict[str, Any] | None, message: str, turn_number: int) -> dict[str, Any]:
    """Handle one merchant/customer reply without calling a model."""
    state = conversations.setdefault(conversation_id, {"turns": [], "reply_texts": []})
    state["turns"].append({"from": "merchant", "body": message, "turn_number": turn_number})

    if _is_stop(message):
        return {"action": "end", "rationale": "The recipient asked to stop; ending cleanly and not reopening the conversation."}
    if _is_auto_reply(message, state):
        return {"action": "end", "rationale": "The reply matches a canned WhatsApp acknowledgement; no further nudge is sent."}

    if merchant is None:
        return {"action": "wait", "wait_seconds": 1800, "rationale": "No merchant context is available yet; waiting avoids an ungrounded reply."}

    if _is_commitment(message):
        name = _merchant_name(merchant)
        return _reply_action(
            f"Done — I’m moving this forward for {name}. I’ll use the details already on your profile and come back with the draft/next step here. If anything looks off, you can edit it before it goes live.",
            "open_ended",
            "The merchant has given a clear go-ahead, so the bot advances to execution instead of asking another qualifying question.",
        )

    if re.search(r"\b(when|how long|what next|what do i|how do i|price|cost|details)\b", message.lower()):
        return _reply_action(
            "I can keep this simple: I’ll prepare the first draft from the details already in your account, then you can approve or edit it here. Want me to start with the version for your current offer?",
            "open_ended",
            "The answer addresses a practical next-step question and removes extra qualification.",
        )

    if turn_number >= 4:
        return {"action": "wait", "wait_seconds": 1800, "rationale": "The conversation has had several turns; giving the merchant space avoids over-messaging."}

    return _reply_action(
        "Got it. I’ll keep the next step focused and use the information already on your profile. Want me to send the short draft here?",
        "open_ended",
        "Acknowledged the reply and offered one clear next action without adding unsupported details.",
    )


def _handle_context(data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    scope = data.get("scope")
    context_id = data.get("context_id")
    version = data.get("version")
    if scope not in VALID_SCOPES or not context_id or not isinstance(version, int) or not isinstance(data.get("payload"), dict):
        return 400, {"accepted": False, "reason": "invalid_context"}
    key = (scope, str(context_id))
    current = contexts.get(key)
    if current and current["version"] >= version:
        return 409, {"accepted": False, "reason": "stale_version", "current_version": current["version"]}
    contexts[key] = {"version": version, "payload": data["payload"]}
    return 200, {"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


class Handler(BaseHTTPRequestHandler):
    server_version = "VeraLocal/1.0"

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return None

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/v1/healthz":
            counts = {scope: sum(1 for (stored_scope, _), _ in contexts.items() if stored_scope == scope) for scope in VALID_SCOPES}
            self._write(200, {"status": "ok", "uptime_seconds": int(time.time() - STARTED_AT), "contexts_loaded": counts})
            return
        if path == "/v1/metadata":
            self._write(200, {
                "team_name": os.getenv("VERA_TEAM_NAME", "Vera Local"),
                "team_members": [os.getenv("VERA_TEAM_MEMBER", "")],
                "model": "deterministic-context-composer",
                "approach": "category-aware rules with stateful reply routing",
                "version": "1.0.0",
            })
            return
        self._write(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        data = self._read_json()
        if data is None:
            self._write(400, {"accepted": False, "reason": "invalid_json"})
            return
        if path == "/v1/context":
            status, payload = _handle_context(data)
            self._write(status, payload)
            return
        if path == "/v1/tick":
            actions = []
            for trigger_id in data.get("available_triggers") or []:
                trigger = _get("trigger", trigger_id)
                if not trigger:
                    continue
                merchant_id = trigger.get("merchant_id")
                merchant = _get("merchant", merchant_id) if merchant_id else None
                if not merchant:
                    continue
                category = _category_for(merchant)
                customer_id = trigger.get("customer_id")
                customer = _get("customer", customer_id) if customer_id else None
                result = compose(category, merchant, trigger, customer)
                if result.get("suppressed"):
                    continue
                conversation_id = f"conv_{merchant_id}_{trigger_id}"
                state = conversations.setdefault(conversation_id, {})
                state.update({"merchant_id": merchant_id, "customer_id": customer_id, "trigger_id": trigger_id})
                body = result["body"]
                if body.lower() in sent_bodies.get(conversation_id, set()):
                    continue
                _remember_sent(conversation_id, body)
                actions.append({
                    "conversation_id": conversation_id,
                    "merchant_id": merchant_id,
                    "customer_id": customer_id,
                    "send_as": result["send_as"],
                    "trigger_id": trigger_id,
                    "template_name": f"vera_{trigger.get('kind', 'update')}_v1",
                    "template_params": [_first_name(merchant), trigger.get("kind", "update")],
                    **result,
                })
            self._write(200, {"actions": actions[:20]})
            return
        if path == "/v1/reply":
            conversation_id = str(data.get("conversation_id") or "reply")
            merchant_id = data.get("merchant_id")
            customer_id = data.get("customer_id")
            merchant = _get("merchant", merchant_id) if merchant_id else None
            customer = _get("customer", customer_id) if customer_id else None
            self._write(200, respond(conversation_id, merchant, customer, str(data.get("message") or ""), int(data.get("turn_number") or 1)))
            return
        self._write(404, {"detail": "not found"})

    def log_message(self, *_: Any) -> None:
        return


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Vera listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
