"""One-time, strictly allowlisted Season 1 replacement and footer-only cleanup.

Standalone: no dependency on another temporary runner, bot.main, or state APIs.
Every write is followed by independent read-back. Stop instead of retrying any
failure; receipts identify partial completion without exposing the webhook.
"""

import copy
import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import urlsplit

import requests


CHANNEL_ID = "1528621172407799991"
GUILD_ID = "1528621163390173194"
APPROVED_COMMIT_MESSAGE = "Run approved Season 1 SP Chase replacement and clean post footers 2026-10-16"
INSPECT_COMMIT_MESSAGE = "Inspect approved Season 1 SP Chase and objective message ownership 2026-10-16"
SEASON_ONLY_COMMIT_MESSAGE = "Replace approved Season 1 SP Chase only after read-only verification 2026-10-16"
SEASON = {
    "id": 2632, "name": "Season 1 SP Chase", "slug": "70-season-1-sp-chase",
    "category": "seasonal", "tasks": 5, "required": None,
    "old_id": "1560336742182162496",
    "old_url": "https://www.fut.gg/objectives/70-season-1-sp-chase/",
    "url": "https://www.fut.gg/objectives/seasonal/70-season-1-sp-chase/",
}
CLEANUP_TARGETS = (
    {"message_id": "1560847839951585341", "name": "Future Stardom",
     "url": "https://www.fut.gg/objectives/campaigns/145-future-stardom/"},
    {"message_id": "1560847853562110004", "name": "Future Stars SP",
     "url": "https://www.fut.gg/objectives/campaigns/144-future-stars-sp/"},
    {"message_id": "1560847862147973212", "name": "Ultimate Gauntlet 2",
     "url": "https://www.fut.gg/objectives/campaigns/133-ultimate-gauntlet-2/"},
)


class CleanupError(Exception):
    """Safe to print: contains no response bodies, credentials or request URLs."""


def emit(event, **values):
    print(json.dumps({"event": event, **values}, sort_keys=True), flush=True)


def guard_context(env):
    if env.get("GITHUB_RUN_ATTEMPT") != "1":
        raise CleanupError("Only attempt 1 is allowed. Inspect prior receipts; do not blindly rerun.")
    if env.get("GITHUB_EVENT_NAME") != "push" or env.get("GITHUB_REF") != "refs/heads/main":
        raise CleanupError("This cleanup requires the approved one-time push on main.")
    if env.get("APPROVED_EVENT_COMMIT_MESSAGE") != APPROVED_COMMIT_MESSAGE:
        raise CleanupError("Exact one-time replacement and footer cleanup commit message is required.")


def guard_inspect_context(env):
    if (env.get("GITHUB_RUN_ATTEMPT") != "1" or env.get("GITHUB_EVENT_NAME") != "push" or
            env.get("GITHUB_REF") != "refs/heads/main" or
            env.get("APPROVED_EVENT_COMMIT_MESSAGE") != INSPECT_COMMIT_MESSAGE):
        raise CleanupError("Read-only inspection requires its exact diagnostic push on main, attempt 1.")


def guard_season_only_context(env):
    if (env.get("GITHUB_RUN_ATTEMPT") != "1" or env.get("GITHUB_EVENT_NAME") != "push" or
            env.get("GITHUB_REF") != "refs/heads/main" or
            env.get("APPROVED_EVENT_COMMIT_MESSAGE") != SEASON_ONLY_COMMIT_MESSAGE):
        raise CleanupError("Season-only replacement requires its exact approved push on main, attempt 1.")


def validated_webhook(raw):
    try:
        parsed = urlsplit(raw)
    except (TypeError, ValueError):
        raise CleanupError("Objectives webhook configuration is missing or invalid.") from None
    match = re.fullmatch(r"/api(?:/v\d+)?/webhooks/(\d{17,20})/([A-Za-z0-9._-]+)", parsed.path)
    if (parsed.scheme != "https" or parsed.netloc != "discord.com" or
            parsed.query or parsed.fragment or not match):
        raise CleanupError("Objectives webhook configuration is missing or invalid.")
    return raw, match.group(1)


def request(http, method, url, expected, **kwargs):
    try:
        response = http.request(method, url, timeout=30, allow_redirects=False, **kwargs)
    except requests.RequestException:
        suffix = " Delivery may be ambiguous; do not retry automatically." if method == "POST" else ""
        raise CleanupError(method + " transport failure." + suffix) from None
    if response.status_code not in expected:
        raise CleanupError(f"{method} returned HTTP {response.status_code}; stopped without automatic retry.")
    return response


def json_body(response):
    try:
        result = response.json()
    except (TypeError, ValueError):
        raise CleanupError("Discord returned an unreadable confirmation; inspect receipts before retrying.") from None
    if not isinstance(result, dict):
        raise CleanupError("Discord returned an unexpected confirmation shape.")
    return result


def message_identity(message, expected_id, webhook_id):
    if (str(message.get("id")) != expected_id or
            str(message.get("channel_id")) != CHANNEL_ID or
            str(message.get("webhook_id")) != webhook_id):
        raise CleanupError("Message identity/channel/webhook validation failed.")


def writable_embed(embed, remove_footer=False):
    """Preserve author-supplied content while excluding Discord rendering metadata.

    GET adds type/provider/video and proxy URL/size metadata. Those are not
    writable webhook embed properties; only reconstruct their writable subset.
    """
    result = {key: copy.deepcopy(embed[key]) for key in
              ("title", "description", "url", "timestamp", "color", "fields") if key in embed}
    for key, allowed in (("image", ("url",)), ("thumbnail", ("url",)),
                         ("author", ("name", "url", "icon_url")),
                         ("footer", ("text", "icon_url"))):
        if key == "footer" and remove_footer:
            continue
        if key in embed:
            if not isinstance(embed[key], dict):
                raise CleanupError("An embed has an unexpected metadata shape; no changes are safe.")
            result[key] = {name: copy.deepcopy(embed[key][name]) for name in allowed if name in embed[key]}
    if "fields" in result:
        result["fields"] = [
            {"name": value.get("name", ""), "value": value.get("value", ""),
             "inline": bool(value.get("inline"))}
            for value in result["fields"]
        ]
    return result


def preservation_snapshot(message):
    """Everything except the removed footer, edit timestamp and live reactions.

    Reactions are checked separately so a newly added reaction does not look
    like lost content. All other message properties must remain unchanged.
    """
    snapshot = copy.deepcopy(message)
    snapshot.pop("edited_timestamp", None)
    snapshot.pop("reactions", None)
    snapshot["embeds"] = [writable_embed(embed, remove_footer=True) for embed in message.get("embeds", [])]
    return snapshot


def reaction_counts(message):
    counts = {}
    for reaction in message.get("reactions") or []:
        emoji = reaction.get("emoji") or {}
        key = str(emoji.get("id") or emoji.get("name") or "unknown")
        count = reaction.get("count", 0)
        if not isinstance(count, int) or count < 0:
            raise CleanupError("Reaction confirmation has an unexpected shape.")
        counts[key] = counts.get(key, 0) + count
    return counts


def reactions_preserved(before, after):
    observed = reaction_counts(after)
    return all(observed.get(emoji, 0) >= count for emoji, count in reaction_counts(before).items())


def payload_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_page(page):
    if page.get("title") != SEASON["name"] or page.get("url") != SEASON["url"]:
        raise CleanupError("Formatted Season objective title/source does not match the allowlist.")
    if "footer" in page or any(key.startswith("_") for key in page):
        raise CleanupError("The new Season post must have no footer or private delivery metadata.")
    fields = page.get("fields", [])
    if len(fields) > 25 or len(page.get("title", "")) > 256 or len(page.get("description", "")) > 4096:
        raise CleanupError("Formatted Season objective exceeds a Discord limit.")
    size = len(page.get("title", "")) + len(page.get("description", ""))
    size += len((page.get("author") or {}).get("name", ""))
    for value in fields:
        if not 1 <= len(value.get("name", "")) <= 256 or not 1 <= len(value.get("value", "")) <= 1024:
            raise CleanupError("Formatted Season objective field exceeds a Discord limit.")
        size += len(value["name"]) + len(value["value"])
    if size > 6000:
        raise CleanupError("Formatted Season objective exceeds Discord's message character budget.")
    if not any(value["name"].startswith("Group reward") for value in fields):
        raise CleanupError("Formatted Season objective is missing its group reward.")
    for number in range(1, SEASON["tasks"] + 1):
        if not any(value["name"].startswith(f"{number}. ") and "**Reward:**" in value["value"] for value in fields):
            raise CleanupError("Formatted Season objective is missing a task or task reward.")


def prepare(objectives, format_objective, expand_pages):
    matches = [obj for obj in objectives if obj.get("id") == SEASON["id"]]
    if len(matches) != 1:
        raise CleanupError("The allowlisted Season objective is missing or duplicated in the live source.")
    objective = matches[0]
    if (str(objective.get("game")) != "27" or objective.get("name") != SEASON["name"] or
            objective.get("slug") != SEASON["slug"] or
            (objective.get("category") or {}).get("slug") != SEASON["category"] or
            objective.get("tasksCount") != SEASON["tasks"] or
            objective.get("objectivesCompletionCount") != SEASON["required"]):
        raise CleanupError("Live Season objective identity or completion requirements changed; review before posting.")
    tasks = objective.get("objectives")
    if not isinstance(tasks, list) or len(tasks) != SEASON["tasks"] or not objective.get("awards"):
        raise CleanupError("Live Season objective task/reward data is incomplete.")
    if any(not task.get("name") or not task.get("description") or not task.get("awards") for task in tasks):
        raise CleanupError("A Season task is missing its name, requirements or reward.")
    pages = expand_pages(format_objective(objective))
    if len(pages) != 1:
        raise CleanupError("This exact one-message replacement requires all Season tasks inline in one post.")
    validate_page(pages[0])
    return pages[0]


def read_message(http, webhook_url, message_id, webhook_id):
    result = json_body(request(http, "GET", webhook_url + "/messages/" + message_id, {200}))
    message_identity(result, message_id, webhook_id)
    return result


def inspect_messages(webhook_url, http=requests):
    """Five GETs for an in-scope webhook; never POST/PATCH/DELETE.

    A webhook GET 404 cannot distinguish a deleted message from a message owned
    by another webhook. Log only safe identity fields, never response bodies,
    webhook URLs/tokens, attachment URLs, or arbitrary embed URLs.
    """
    webhook_url, webhook_id = validated_webhook(webhook_url)

    def get(url, label, message_id=None):
        try:
            response = http.request("GET", url, timeout=30, allow_redirects=False)
        except requests.RequestException:
            emit("inspect_get", target=label, message_id=message_id, status="transport_error")
            return None
        return response

    def safe_id(value):
        value = str(value or "")
        return value if re.fullmatch(r"\d{17,20}", value) else "unexpected_or_missing"

    response = get(webhook_url, "webhook")
    if response is None:
        return
    if response.status_code != 200:
        emit("inspect_webhook", status=response.status_code, expected_webhook_id=webhook_id)
        return
    metadata = json_body(response)
    in_scope = (str(metadata.get("id")) == webhook_id and str(metadata.get("channel_id")) == CHANNEL_ID and
                str(metadata.get("guild_id")) == GUILD_ID and metadata.get("type") == 1)
    emit("inspect_webhook", status=200, webhook_id=safe_id(metadata.get("id")),
         channel_id=safe_id(metadata.get("channel_id")), guild_id=safe_id(metadata.get("guild_id")),
         expected_scope_matches=in_scope)
    if not in_scope:
        raise CleanupError("Diagnostic webhook identity is outside the approved channel/server.")
    targets = [("old_season", SEASON["old_id"], SEASON["name"], SEASON["old_url"])]
    targets += [("footer_cleanup", target["message_id"], target["name"], target["url"]) for target in CLEANUP_TARGETS]
    for label, message_id, name, url in targets:
        response = get(webhook_url + "/messages/" + message_id, label, message_id)
        if response is None:
            continue
        if response.status_code != 200:
            emit("inspect_message", target=label, message_id=message_id, expected_title=name,
                 expected_url=url, status=response.status_code,
                 meaning="Unavailable to this webhook; not proof of deletion." if response.status_code == 404 else "Read-only lookup failed.")
            continue
        message = json_body(response)
        embeds = message.get("embeds") or []
        embed = embeds[0] if len(embeds) == 1 else {}
        observed_url = str(embed.get("url") or "")
        try:
            parsed = urlsplit(observed_url)
            safe_url = (observed_url if parsed.scheme == "https" and parsed.netloc == "www.fut.gg" and
                        parsed.path.startswith("/objectives/") and not parsed.query and not parsed.fragment and
                        len(observed_url) <= 250 else "unexpected_or_missing")
        except ValueError:
            safe_url = "unexpected_or_missing"
        emit("inspect_message", target=label, message_id=message_id, status=200,
             observed_id=safe_id(message.get("id")), channel_id=safe_id(message.get("channel_id")),
             webhook_id=safe_id(message.get("webhook_id")), title=str(embed.get("title") or "")[:256],
             source_url=safe_url, embed_count=len(embeds), footer_present="footer" in embed,
             expected_identity_matches=(str(message.get("id")) == message_id and
                 str(message.get("channel_id")) == CHANNEL_ID and str(message.get("webhook_id")) == webhook_id and
                 embed.get("title") == name and embed.get("url") == url))
    emit("inspection_complete", mutation_count=0)


def replace_season_and_clean_footers(webhook_url, page, http=requests, sleep=time.sleep,
                                   cleanup_targets=CLEANUP_TARGETS, season_only=False):
    cleanup_targets = tuple(cleanup_targets)
    if season_only:
        if cleanup_targets:
            raise CleanupError("Season-only mode cannot read or edit footer-cleanup targets.")
    elif cleanup_targets != CLEANUP_TARGETS:
        raise CleanupError("Normal cleanup requires the exact three-message allowlist.")
    validate_page(page)
    webhook_url, webhook_id = validated_webhook(webhook_url)
    webhook = json_body(request(http, "GET", webhook_url, {200}))
    if (str(webhook.get("id")) != webhook_id or str(webhook.get("channel_id")) != CHANNEL_ID or
            str(webhook.get("guild_id")) != GUILD_ID or webhook.get("type") != 1):
        raise CleanupError("Objectives webhook does not belong to the approved channel/server.")

    # Normal mode preflights all four targets. Explicit Season-only mode
    # preflights only the old Season post and never reads/edits the three others.
    old = read_message(http, webhook_url, SEASON["old_id"], webhook_id)
    old_embeds = old.get("embeds") or []
    if (len(old_embeds) != 1 or old_embeds[0].get("title") != SEASON["name"] or
            old_embeds[0].get("url") != SEASON["old_url"]):
        raise CleanupError("Old Season message title/source does not exactly match the allowlist.")
    originals = {}
    for target in cleanup_targets:
        original = read_message(http, webhook_url, target["message_id"], webhook_id)
        embeds = original.get("embeds") or []
        if (len(embeds) != 1 or embeds[0].get("title") != target["name"] or
                embeds[0].get("url") != target["url"]):
            raise CleanupError("A footer-cleanup message title/source does not exactly match the allowlist.")
        originals[target["message_id"]] = original
    emit("preflight_passed", season_objective_id=SEASON["id"], old_message_id=SEASON["old_id"],
         footer_message_ids=[target["message_id"] for target in cleanup_targets],
         mode="season_only" if season_only else "season_and_footers")

    posted = json_body(request(http, "POST", webhook_url, {200}, params={"wait": "true"},
                               json={"content": "", "embeds": [page], "allowed_mentions": {"parse": []}}))
    new_id = str(posted.get("id", ""))
    reserved_ids = {SEASON["old_id"]} | {target["message_id"] for target in CLEANUP_TARGETS}
    if not re.fullmatch(r"\d{17,20}", new_id) or new_id in reserved_ids:
        raise CleanupError("Discord did not provide a valid new message ID; inspect the channel before retrying.")
    emit("season_replacement_created", objective_id=SEASON["id"], new_message_id=new_id)
    current = read_message(http, webhook_url, new_id, webhook_id)
    embeds = current.get("embeds") or []
    if (len(embeds) != 1 or "footer" in embeds[0] or writable_embed(embeds[0]) != writable_embed(page) or
            current.get("content") or current.get("mention_roles") or current.get("mention_everyone") or
            current.get("mentions") or current.get("attachments")):
        raise CleanupError("New Season read-back did not match; no old post has been deleted.")
    season_receipt = {"objective_id": SEASON["id"], "old_message_id": SEASON["old_id"],
                      "new_message_id": new_id, "payload_sha256": payload_digest(writable_embed(page))}
    emit("season_replacement_verified", **season_receipt)
    sleep(1.5)

    cleanup_receipts = []
    for target in cleanup_targets:
        message_id = target["message_id"]
        original = originals[message_id]
        # Do not overwrite a message someone edited after the all-target preflight.
        before = read_message(http, webhook_url, message_id, webhook_id)
        if preservation_snapshot(before) != preservation_snapshot(original) or before.get("embeds") != original.get("embeds"):
            raise CleanupError("A cleanup target changed after preflight; stopped before overwriting it.")
        edited_embeds = [writable_embed(embed, remove_footer=True) for embed in before["embeds"]]
        request(http, "PATCH", webhook_url + "/messages/" + message_id, {200},
                json={"embeds": edited_embeds, "allowed_mentions": {"parse": []}})
        emit("footer_edit_submitted", message_id=message_id, name=target["name"])
        after = read_message(http, webhook_url, message_id, webhook_id)
        if (any("footer" in embed for embed in after.get("embeds", [])) or
                preservation_snapshot(after) != preservation_snapshot(before) or
                not reactions_preserved(before, after)):
            raise CleanupError("Footer-only edit read-back changed other content or reactions; old Season post retained.")
        receipt = {"message_id": message_id, "name": target["name"],
                   "unchanged_content_sha256": payload_digest(preservation_snapshot(after)),
                   "reaction_count_before": sum(reaction_counts(before).values()),
                   "reaction_count_after": sum(reaction_counts(after).values())}
        cleanup_receipts.append(receipt)
        emit("footer_removal_verified", **receipt)
        sleep(1.5)

    # Re-read the original again immediately before its one authorized deletion.
    old_current = read_message(http, webhook_url, SEASON["old_id"], webhook_id)
    if preservation_snapshot(old_current) != preservation_snapshot(old) or old_current.get("embeds") != old.get("embeds"):
        raise CleanupError("Old Season post changed after preflight; replacement is retained but deletion stopped.")
    old_url = webhook_url + "/messages/" + SEASON["old_id"]
    request(http, "DELETE", old_url, {204})
    emit("old_season_delete_submitted", old_message_id=SEASON["old_id"])
    request(http, "GET", old_url, {404})
    emit("old_season_deletion_verified", old_message_id=SEASON["old_id"])
    emit("complete", replacement=season_receipt, footer_removals=cleanup_receipts, deleted_count=1)
    return {"replacement": season_receipt, "footer_removals": cleanup_receipts, "deleted_count": 1}


def main(argv=()):
    if list(argv) == ["--inspect"]:
        guard_inspect_context(os.environ)
        inspect_messages(os.environ.get("OBJECTIVES_WEBHOOK_URL", ""))
        return
    season_only = list(argv) == ["--season-only"]
    if argv and not season_only:
        raise CleanupError("Unsupported runner arguments.")
    if season_only:
        guard_season_only_context(os.environ)
    else:
        guard_context(os.environ)
    import bot
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            objectives = bot.fetch_objectives(browser.new_page())
        finally:
            browser.close()
    page = prepare(objectives, bot.objective_embed, bot.message_embeds)
    if season_only:
        replace_season_and_clean_footers(os.environ.get("OBJECTIVES_WEBHOOK_URL", ""), page,
                                       cleanup_targets=(), season_only=True)
    else:
        replace_season_and_clean_footers(os.environ.get("OBJECTIVES_WEBHOOK_URL", ""), page)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except CleanupError as error:
        emit("stopped", reason=str(error), instruction="Inspect receipts and the channel before any further action; no automatic retry.")
        raise SystemExit(1)
    except Exception as error:
        # Never print exception text/tracebacks: transport exceptions may carry secrets.
        emit("stopped", reason="Unexpected " + type(error).__name__, instruction="Inspect receipts and the channel; no automatic retry.")
        raise SystemExit(1)
