"""One-time, allowlisted Objective replacement. Never calls bot.main or state APIs.

The live workflow injects only the existing Objectives webhook. Stop on any
ambiguity; a new workflow dispatch is NOT an automatic recovery mechanism.
"""

import hashlib
import json
import os
import re
import time
from urllib.parse import urlsplit

import requests


CHANNEL_ID = "1528621172407799991"
GUILD_ID = "1528621163390173194"
APPROVED_COMMIT_MESSAGE = "Run approved three-objective replacement test 2026-10-16"
TARGETS = (
    {"id": 2635, "name": "Future Stardom", "slug": "145-future-stardom",
     "tasks": 5, "required": 4, "old_id": "1560699497669988578"},
    {"id": 2634, "name": "Future Stars SP", "slug": "144-future-stars-sp",
     "tasks": 4, "required": None, "old_id": "1560699507853893707"},
    {"id": 2633, "name": "Ultimate Gauntlet 2", "slug": "133-ultimate-gauntlet-2",
     "tasks": 10, "required": 8, "old_id": "1560699517022503065"},
)


class RepostError(Exception):
    """Safe to print: never contains response bodies, tokens or request URLs."""


def source_url(target):
    return "https://www.fut.gg/objectives/campaigns/" + target["slug"] + "/"


def old_source_url(target):
    return "https://www.fut.gg/objectives/" + target["slug"] + "/"


def emit(event, **values):
    print(json.dumps({"event": event, **values}, sort_keys=True), flush=True)


def guard_context(env):
    if env.get("GITHUB_RUN_ATTEMPT") != "1":
        raise RepostError("Only attempt 1 is allowed. Inspect prior receipts; do not blindly rerun.")
    if env.get("GITHUB_EVENT_NAME") != "push" or env.get("GITHUB_REF") != "refs/heads/main":
        raise RepostError("This test requires the approved one-time push on main.")
    if env.get("APPROVED_EVENT_COMMIT_MESSAGE") != APPROVED_COMMIT_MESSAGE:
        raise RepostError("Exact one-time replacement commit message is required.")


def validated_webhook(raw):
    parsed = urlsplit(raw)
    match = re.fullmatch(r"/api(?:/v\d+)?/webhooks/(\d{17,20})/([A-Za-z0-9._-]+)", parsed.path)
    if (parsed.scheme != "https" or parsed.netloc != "discord.com" or
            parsed.query or parsed.fragment or not match):
        raise RepostError("Objectives webhook configuration is missing or invalid.")
    return raw, match.group(1)


def request(http, method, url, expected, **kwargs):
    try:
        response = http.request(method, url, timeout=30, allow_redirects=False, **kwargs)
    except requests.RequestException:
        suffix = " Delivery may be ambiguous; do not retry automatically." if method == "POST" else ""
        raise RepostError(method + " transport failure." + suffix) from None
    if response.status_code not in expected:
        raise RepostError(f"{method} returned HTTP {response.status_code}; stopped without automatic retry.")
    return response


def json_body(response):
    try:
        result = response.json()
    except (ValueError, TypeError):
        raise RepostError("Discord returned an unreadable confirmation; inspect receipts before retrying.") from None
    if not isinstance(result, dict):
        raise RepostError("Discord returned an unexpected confirmation shape.")
    return result


def message_identity(message, expected_id, webhook_id):
    if (str(message.get("id")) != expected_id or
            str(message.get("channel_id")) != CHANNEL_ID or
            str(message.get("webhook_id")) != webhook_id):
        raise RepostError("Message identity/channel/webhook validation failed.")


def comparable_embed(embed):
    """Discord adds rendering metadata; compare only the content we submitted."""
    result = {key: embed.get(key, "") for key in ("title", "description", "url")}
    result["color"] = embed.get("color")
    result["footer"] = (embed.get("footer") or {}).get("text", "")
    result["fields"] = [
        {"name": value.get("name", ""), "value": value.get("value", ""),
         "inline": bool(value.get("inline"))}
        for value in embed.get("fields", [])
    ]
    for key in ("image", "thumbnail"):
        result[key] = (embed.get(key) or {}).get("url", "")
    return result


def validate_page(page, target):
    if page.get("title") != target["name"] or page.get("url") != source_url(target):
        raise RepostError("Formatted objective title/source does not match the allowlist.")
    if any(key.startswith("_") for key in page):
        raise RepostError("Private delivery metadata must not reach Discord.")
    fields = page.get("fields", [])
    if len(fields) > 25 or len(page.get("title", "")) > 256 or len(page.get("description", "")) > 4096:
        raise RepostError("Formatted objective exceeds a Discord limit.")
    size = len(page.get("title", "")) + len(page.get("description", ""))
    size += len((page.get("footer") or {}).get("text", ""))
    size += len((page.get("author") or {}).get("name", ""))
    for value in fields:
        if not 1 <= len(value.get("name", "")) <= 256 or not 1 <= len(value.get("value", "")) <= 1024:
            raise RepostError("Formatted objective field exceeds a Discord limit.")
        size += len(value["name"]) + len(value["value"])
    if size > 6000:
        raise RepostError("Formatted objective exceeds Discord's message character budget.")
    if not any(value["name"].startswith("Group reward") for value in fields):
        raise RepostError("Formatted objective is missing its group reward.")
    for number in range(1, target["tasks"] + 1):
        if not any(value["name"].startswith(f"{number}. ") and "**Reward:**" in value["value"] for value in fields):
            raise RepostError("Formatted objective is missing a task or task reward.")


def prepare(objectives, format_objective, expand_pages, targets):
    if len(targets) != 3 or len({target["id"] for target in targets}) != 3:
        raise RepostError("Exactly three distinct allowlisted objectives are required.")
    old_ids = [target["old_id"] for target in targets]
    if len(set(old_ids)) != 3 or any(not re.fullmatch(r"\d{17,20}", value) for value in old_ids):
        raise RepostError("All three exact old message IDs must be supplied before running.")
    prepared = []
    for target in targets:
        matches = [obj for obj in objectives if obj.get("id") == target["id"]]
        if len(matches) != 1:
            raise RepostError("An allowlisted objective is missing or duplicated in the live source.")
        obj = matches[0]
        if (str(obj.get("game")) != "27" or obj.get("name") != target["name"] or
                obj.get("slug") != target["slug"] or (obj.get("category") or {}).get("slug") != "campaigns" or
                obj.get("tasksCount") != target["tasks"] or
                obj.get("objectivesCompletionCount") != target["required"]):
            raise RepostError("Live objective identity or completion requirements changed; review before posting.")
        tasks = obj.get("objectives")
        if not isinstance(tasks, list) or len(tasks) != target["tasks"] or not obj.get("awards"):
            raise RepostError("Live objective task/reward data is incomplete.")
        if any(not task.get("name") or not task.get("description") or not task.get("awards") for task in tasks):
            raise RepostError("A live objective task is missing its name, requirements or reward.")
        pages = expand_pages(format_objective(obj))
        if len(pages) != 1:
            raise RepostError("This exact three-message test requires one complete inline message per objective.")
        validate_page(pages[0], target)
        prepared.append((target, pages[0]))
    return prepared


def replace_three(webhook_url, prepared, http=requests, sleep=time.sleep):
    webhook_url, webhook_id = validated_webhook(webhook_url)
    webhook = json_body(request(http, "GET", webhook_url, {200}))
    if (str(webhook.get("id")) != webhook_id or str(webhook.get("channel_id")) != CHANNEL_ID or
            str(webhook.get("guild_id")) != GUILD_ID or webhook.get("type") != 1):
        raise RepostError("Objectives webhook does not belong to the approved channel/server.")
    # Validate every old target before creating or deleting any message.
    for target, _ in prepared:
        old = json_body(request(http, "GET", webhook_url + "/messages/" + target["old_id"], {200}))
        message_identity(old, target["old_id"], webhook_id)
        embeds = old.get("embeds") or []
        if len(embeds) != 1 or embeds[0].get("title") != target["name"] or embeds[0].get("url") != old_source_url(target):
            raise RepostError("An old message does not exactly match the approved objective title/source.")
    emit("preflight_passed", objective_ids=[target["id"] for target, _ in prepared], old_message_count=3)
    replacements = []
    for target, page in prepared:
        payload = {"content": "", "embeds": [page], "allowed_mentions": {"parse": []}}
        posted = json_body(request(http, "POST", webhook_url, {200}, params={"wait": "true"}, json=payload))
        new_id = str(posted.get("id", ""))
        if not re.fullmatch(r"\d{17,20}", new_id) or new_id in [item["old_id"] for item, _ in prepared]:
            raise RepostError("Discord did not provide a valid new message ID; stop and inspect the channel.")
        # Receipt is emitted before read-back so a read failure still leaves an exact recovery target.
        emit("replacement_created", objective_id=target["id"], new_message_id=new_id)
        current = json_body(request(http, "GET", webhook_url + "/messages/" + new_id, {200}))
        message_identity(current, new_id, webhook_id)
        embeds = current.get("embeds") or []
        if (len(embeds) != 1 or comparable_embed(embeds[0]) != comparable_embed(page) or
                current.get("content") or current.get("mention_roles") or current.get("mention_everyone") or
                current.get("attachments")):
            raise RepostError("Replacement read-back did not match; old messages have not been deleted.")
        digest = hashlib.sha256(json.dumps(comparable_embed(page), sort_keys=True).encode()).hexdigest()
        replacements.append({"objective_id": target["id"], "old_message_id": target["old_id"],
                             "new_message_id": new_id, "payload_sha256": digest})
        emit("replacement_verified", **replacements[-1])
        sleep(1.5)
    # Never delete an original until all three complete replacements are confirmed.
    for receipt in replacements:
        old_url = webhook_url + "/messages/" + receipt["old_message_id"]
        request(http, "DELETE", old_url, {204})
        request(http, "GET", old_url, {404})
        emit("original_deleted", objective_id=receipt["objective_id"], old_message_id=receipt["old_message_id"])
        sleep(1.5)
    emit("complete", replacement_count=len(replacements), deleted_count=len(replacements), receipts=replacements)
    return replacements


def main():
    guard_context(os.environ)
    # Import has no delivery/state side effects. Only the Objective scraper and
    # production formatting helpers are called; all sending stays allowlisted.
    import bot
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            objectives = bot.fetch_objectives(browser.new_page())
        finally:
            browser.close()
    prepared = prepare(objectives, bot.objective_embed, bot.message_embeds, TARGETS)
    replace_three(os.environ.get("OBJECTIVES_WEBHOOK_URL", ""), prepared)


if __name__ == "__main__":
    try:
        main()
    except RepostError as error:
        emit("stopped", reason=str(error), instruction="Inspect receipts and the channel before any new dispatch; no automatic retry.")
        raise SystemExit(1)
    except Exception as error:
        # Do not print exception text/tracebacks: transport errors may contain credentials.
        emit("stopped", reason="Unexpected " + type(error).__name__, instruction="Inspect receipts and the channel; no automatic retry.")
        raise SystemExit(1)
