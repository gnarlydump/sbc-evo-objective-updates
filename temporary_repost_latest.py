"""One-time allowlisted 10-per-category resend. No state, edits or deletions.

All 30 posts and all three destinations are preflighted before the first POST.
Every POST gets an immediate safe receipt and an independent GET read-back.
Never retry automatically: a failed POST may already have created a message.
"""

import copy
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests


APPROVED_COMMIT_MESSAGE = "Repost latest 10 Objectives SBCs and Evolutions in organized layout 2026-10-16"
REPOSITORY = "gnarlydump/sbc-evo-objective-updates"
GUILD_ID = "1528621163390173194"
OBJECTIVES_CHANNEL_ID = "1528621172407799991"
CATEGORIES = ("objectives", "sbcs", "evolutions")
WEBHOOK_ENV = {
    "objectives": "OBJECTIVES_WEBHOOK_URL",
    "sbcs": "SBC_WEBHOOK_URL",
    "evolutions": "EVOLUTIONS_WEBHOOK_URL",
}
URL_PREFIX = {"objectives": "/objectives/", "sbcs": "/sbc/", "evolutions": "/evolutions/"}
MANIFEST_PATH = Path(__file__).with_name("temporary_repost_manifest.json")
POST_DELAY_SECONDS = 2


class RepostError(Exception):
    """Controlled, credential-free errors suitable for Actions logs."""


def emit(event, **values):
    print(json.dumps({"event": event, **values}, sort_keys=True), flush=True)


def guard_context(env):
    if (env.get("GITHUB_RUN_ATTEMPT") != "1" or
            env.get("GITHUB_EVENT_NAME") != "push" or
            env.get("GITHUB_REF") != "refs/heads/main" or
            env.get("GITHUB_REPOSITORY") != REPOSITORY or
            env.get("APPROVED_EVENT_COMMIT_MESSAGE") != APPROVED_COMMIT_MESSAGE):
        raise RepostError("Requires the exact approved push on this repository main, attempt 1. Never blindly rerun.")


def snowflake(value):
    return isinstance(value, str) and re.fullmatch(r"\d{17,20}", value) is not None


def validate_manifest(manifest):
    if (not isinstance(manifest, dict) or manifest.get("version") != 1 or
            manifest.get("guild_id") != GUILD_ID or
            set(manifest.get("categories", {})) != set(CATEGORIES)):
        raise RepostError("Manifest must specify the approved guild and exactly three categories.")
    channels = []
    for category in CATEGORIES:
        entry = manifest["categories"][category]
        if not isinstance(entry, dict) or not snowflake(entry.get("channel_id")):
            raise RepostError("Manifest destination is missing or invalid.")
        channels.append(entry["channel_id"])
        items = entry.get("items")
        if not isinstance(items, list) or len(items) != 10:
            raise RepostError("Manifest requires exactly 10 items in each category.")
        ids, urls = set(), set()
        for item in items:
            if not isinstance(item, dict):
                raise RepostError("Manifest item is invalid.")
            item_id = str(item.get("id", ""))
            if not re.fullmatch(r"\d+", item_id) or item_id in ids:
                raise RepostError("Manifest IDs must be numeric and unique within their category.")
            name = item.get("name")
            url = item.get("url")
            if not isinstance(name, str) or not name.strip() or not isinstance(url, str):
                raise RepostError("Manifest item title/source is missing.")
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.netloc != "www.fut.gg" or
                    not parsed.path.startswith(URL_PREFIX[category]) or parsed.query or parsed.fragment or url in urls):
                raise RepostError("Manifest source URL is invalid or duplicated.")
            ids.add(item_id)
            urls.add(url)
    if len(set(channels)) != 3 or manifest["categories"]["objectives"]["channel_id"] != OBJECTIVES_CHANNEL_ID:
        raise RepostError("Destinations must be distinct and the Objectives channel must match its approved identity.")
    return manifest


def source_record(category, item):
    if not isinstance(item, dict):
        raise RepostError("Live source item shape is invalid.")
    record = item.get("evolution") if category == "evolutions" else item
    if not isinstance(record, dict):
        raise RepostError("Live source item identity is missing.")
    return record


def validate_source_details(category, record):
    if category in ("objectives", "sbcs"):
        key, count_key = (("objectives", "tasksCount") if category == "objectives"
                          else ("challenges", "challengesCount"))
        tasks = record.get(key)
        if (not isinstance(tasks, list) or not tasks or record.get(count_key) != len(tasks) or
                not isinstance(record.get("awards"), list) or not record["awards"]):
            raise RepostError("A selected item has incomplete tasks/challenges or group rewards.")
        for task in tasks:
            if not isinstance(task, dict) or not task.get("name") or not isinstance(task.get("awards"), list):
                raise RepostError("A selected task/challenge has incomplete identity or rewards.")
            if category == "objectives" and (not task.get("description") or not task["awards"]):
                raise RepostError("A selected objective task has incomplete instructions or rewards.")
            if category == "sbcs" and not task.get("requirementsText"):
                raise RepostError("A selected SBC challenge has incomplete requirements.")
    else:
        levels = record.get("levels")
        if not isinstance(record.get("requirementsText"), list) or not isinstance(levels, list) or not levels:
            raise RepostError("A selected evolution has incomplete eligibility or level details.")
        for level in levels:
            if not isinstance(level, dict) or not isinstance(level.get("challenges"), list):
                raise RepostError("A selected evolution level has incomplete task details.")
            options = level.get("upgradeOptions") or []
            if level.get("hasUpgradeChoices") or len(options) > 1:
                if not options or any(not option.get("totalUpgradesText") for option in options):
                    raise RepostError("A selected evolution choice has incomplete upgrades.")
            elif not level.get("totalUpgradesText"):
                raise RepostError("A selected evolution level has incomplete upgrades.")


def text_size(value):
    if not isinstance(value, str):
        raise RepostError("A formatted embed text value has an invalid type.")
    return len(value.encode("utf-16-le")) // 2


def validate_page(page, expected):
    if not isinstance(page, dict) or page.get("title") != expected["name"] or page.get("url") != expected["url"]:
        raise RepostError("Formatted title/source does not exactly match the selected manifest item.")
    allowed = {"title", "description", "url", "timestamp", "color", "fields", "author", "thumbnail", "image"}
    if set(page) - allowed:
        raise RepostError("A post contains footer, attachments, private metadata or unsupported embed properties.")
    size = text_size(page.get("title", "")) + text_size(page.get("description", ""))
    if text_size(page.get("title", "")) > 256 or text_size(page.get("description", "")) > 4096:
        raise RepostError("A post exceeds a Discord title/description limit.")
    author = page.get("author") or {}
    author_size = text_size(author.get("name", ""))
    if author_size > 256:
        raise RepostError("A post exceeds Discord's author limit.")
    size += author_size
    fields = page.get("fields", [])
    if not isinstance(fields, list) or not fields or len(fields) > 25:
        raise RepostError("A post has missing fields or exceeds the Discord field count.")
    for value in fields:
        if not isinstance(value, dict) or set(value) - {"name", "value", "inline"}:
            raise RepostError("A post field has unexpected properties.")
        name_size, value_size = text_size(value.get("name", "")), text_size(value.get("value", ""))
        if not 1 <= name_size <= 256 or not 1 <= value_size <= 1024:
            raise RepostError("A post exceeds a Discord field length limit.")
        size += name_size + value_size
    if size > 6000:
        raise RepostError("A post exceeds Discord's total embed limit.")
    serialized = json.dumps(page, ensure_ascii=False)
    missing_markers = ("Not listed by FUT.GG. Check the source before completing.",
                       "Reward details not listed by FUT.GG", "target score not listed by FUT.GG",
                       "Task coverage", "Challenge coverage")
    if any(marker in serialized for marker in missing_markers):
        raise RepostError("A post contains incomplete task/reward details; review before sending.")
    for key in ("image", "thumbnail"):
        if key in page and (not isinstance(page[key], dict) or set(page[key]) != {"url"} or
                            not str(page[key]["url"]).startswith("https://")):
            raise RepostError("A post image must be a remote HTTPS image, not an attachment.")


def prepare(manifest, source_items, formatters, expand_pages):
    validate_manifest(manifest)
    result = []
    for category in CATEGORIES:
        items = source_items.get(category)
        if not isinstance(items, list):
            raise RepostError("A selected source category could not be loaded.")
        for expected in manifest["categories"][category]["items"]:
            matches = [item for item in items if str(source_record(category, item).get("id")) == str(expected["id"])]
            if len(matches) != 1:
                raise RepostError("A selected item is missing or duplicated in the live source.")
            item = matches[0]
            record = source_record(category, item)
            if str(record.get("game")) != "27" or record.get("name") != expected["name"]:
                raise RepostError("A selected live item game/title changed; review before posting.")
            if "start_time" in expected and record.get("startTime") != expected["start_time"]:
                raise RepostError("A selected live item release time changed; review before posting.")
            validate_source_details(category, record)
            pages = expand_pages(formatters[category](item))
            if len(pages) != 1:
                raise RepostError("Every selected item must fit one full message; automatic continuations are disabled for this resend.")
            validate_page(pages[0], expected)
            result.append({"category": category, "id": str(expected["id"]),
                           "name": expected["name"], "embed": copy.deepcopy(pages[0])})
    if len(result) != 30:
        raise RepostError("Exactly 30 complete posts must pass preflight.")
    return result


def validated_webhook(raw):
    try:
        parsed = urlsplit(raw)
        match = re.fullmatch(r"/api(?:/v\d+)?/webhooks/(\d{17,20})/([A-Za-z0-9._-]+)", parsed.path)
    except (TypeError, ValueError):
        raise RepostError("A webhook configuration is missing or invalid.") from None
    if parsed.scheme != "https" or parsed.netloc != "discord.com" or parsed.query or parsed.fragment or not match:
        raise RepostError("A webhook configuration is missing or invalid.")
    return raw, match.group(1)


def request(http, method, url, **kwargs):
    if method not in ("GET", "POST"):
        raise RepostError("Only read-only requests and new-message creation are permitted.")
    try:
        response = http.request(method, url, timeout=30, allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise RepostError(method + " transport failure; do not retry automatically. A POST may already have created a message.") from None
    if response.status_code != 200:
        raise RepostError(f"{method} returned HTTP {response.status_code}; stopped without retry. Inspect receipts before any recovery.")
    try:
        body = response.json()
    except (TypeError, ValueError):
        raise RepostError("Unreadable Discord confirmation; inspect receipts before any recovery.") from None
    if not isinstance(body, dict):
        raise RepostError("Unexpected Discord confirmation shape; inspect receipts before any recovery.")
    return body


def writable_embed(embed):
    result = {key: copy.deepcopy(embed[key]) for key in
              ("title", "description", "url", "timestamp", "color", "fields") if key in embed}
    for key, allowed in (("image", ("url",)), ("thumbnail", ("url",)),
                         ("author", ("name", "url", "icon_url")), ("footer", ("text", "icon_url"))):
        if key in embed:
            result[key] = {name: copy.deepcopy(embed[key][name]) for name in allowed if name in embed[key]}
    if "fields" in result:
        result["fields"] = [{"name": value.get("name", ""), "value": value.get("value", ""),
                             "inline": bool(value.get("inline"))} for value in result["fields"]]
    return result


def payload_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_message(message, expected_id, destination, embed):
    if (str(message.get("id")) != expected_id or
            str(message.get("channel_id")) != destination["channel_id"] or
            str(message.get("webhook_id")) != destination["webhook_id"]):
        raise RepostError("Created-message identity/channel/webhook does not match the approved destination.")
    if (message.get("content") or message.get("attachments") or message.get("mentions") or
            message.get("mention_roles") or message.get("mention_everyone") or
            not (message.get("flags", 0) & 4096)):
        raise RepostError("Created message has unexpected content, attachment, mention or notification settings.")
    embeds = message.get("embeds")
    if (not isinstance(embeds, list) or len(embeds) != 1 or
            writable_embed(embeds[0]) != writable_embed(embed)):
        raise RepostError("Created-message embed differs from the fully prepared intended post.")


def deliver(manifest, prepared, webhook_values, http=requests, sleep=time.sleep):
    validate_manifest(manifest)
    expected_order = [(category, str(item["id"])) for category in CATEGORIES
                      for item in manifest["categories"][category]["items"]]
    if [(item["category"], item["id"]) for item in prepared] != expected_order:
        raise RepostError("Prepared item order/identity does not match all 30 allowlisted posts.")
    for item in prepared:
        expected = next(value for value in manifest["categories"][item["category"]]["items"]
                        if str(value["id"]) == item["id"])
        validate_page(item["embed"], expected)
    destinations = {}
    for category in CATEGORIES:
        webhook_url, webhook_id = validated_webhook(webhook_values.get(category, ""))
        metadata = request(http, "GET", webhook_url)
        channel_id = manifest["categories"][category]["channel_id"]
        if (str(metadata.get("id")) != webhook_id or metadata.get("type") != 1 or
                str(metadata.get("guild_id")) != GUILD_ID or str(metadata.get("channel_id")) != channel_id):
            raise RepostError("Webhook identity is outside the approved category channel/server.")
        destinations[category] = {"url": webhook_url, "webhook_id": webhook_id, "channel_id": channel_id}
    if len({destination["webhook_id"] for destination in destinations.values()}) != 3:
        raise RepostError("Each category must use its own distinct webhook.")
    emit("preflight_passed", total=30, counts={category: 10 for category in CATEGORIES})
    receipts = []
    for index, item in enumerate(prepared):
        if index:
            sleep(POST_DELAY_SECONDS)
        destination = destinations[item["category"]]
        payload = {"content": "", "embeds": [item["embed"]], "allowed_mentions": {"parse": []}, "flags": 4096}
        created = request(http, "POST", destination["url"], params={"wait": "true"}, json=payload)
        message_id = str(created.get("id", ""))
        if not snowflake(message_id):
            raise RepostError("POST returned no valid message ID; delivery may have succeeded. Inspect before retrying.")
        receipt = {"category": item["category"], "item_id": item["id"], "name": item["name"],
                   "message_id": message_id, "channel_id": destination["channel_id"],
                   "embed_sha256": payload_digest(item["embed"])}
        emit("created", **receipt)
        verify_message(created, message_id, destination, item["embed"])
        observed = request(http, "GET", destination["url"] + "/messages/" + message_id)
        verify_message(observed, message_id, destination, item["embed"])
        receipts.append(receipt)
        emit("verified", **receipt)
    counts = {category: sum(row["category"] == category for row in receipts) for category in CATEGORIES}
    if counts != {category: 10 for category in CATEGORIES}:
        raise RepostError("Final verified counts do not match the approved 10 per category.")
    emit("complete", verified_count=len(receipts), counts=counts, edits=0, deletions=0, state_writes=0)
    return receipts


def main():
    guard_context(os.environ)
    if len(sys.argv) != 1:
        raise RepostError("No alternate modes or arguments are permitted.")
    manifest = validate_manifest(json.loads(MANIFEST_PATH.read_text()))
    import bot
    from post_details import message_embeds

    source_items = {}
    with bot.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            for category, fetch in (("objectives", bot.fetch_objectives), ("sbcs", bot.fetch_sbcs),
                                    ("evolutions", bot.fetch_evolutions)):
                source_items[category] = fetch(page)
        finally:
            browser.close()
    prepared = prepare(manifest, source_items,
                       {"objectives": bot.objective_embed, "sbcs": bot.sbc_embed, "evolutions": bot.evolution_embed},
                       message_embeds)
    deliver(manifest, prepared, {category: os.environ.get(WEBHOOK_ENV[category], "") for category in CATEGORIES})


if __name__ == "__main__":
    try:
        main()
    except RepostError as error:
        emit("stopped", reason=str(error))
        sys.exit(1)
    except Exception as error:
        # Exception strings may contain webhook credentials. Never print them.
        emit("stopped", reason="Unexpected " + type(error).__name__ + "; inspect safe receipts before recovery. No automatic retry.")
        sys.exit(1)
