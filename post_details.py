"""Source-backed task/reward formatting. Pure functions; never sends messages."""

import copy
import re

MISSING = "Not listed by FUT.GG. Check the source before completing."


def clean(value):
    return str(value).strip() if value is not None else ""


def safe_text(value):
    # Source text is data, not Discord formatting or a mention instruction.
    text = clean(value).replace("@", "@\u200b")
    return re.sub(r"([\\*_`~|])", r"\\\1", text)


def named(value):
    """FUT.GG uses a string for SBC packs and a named object for some objectives."""
    return clean(value.get("name")) if isinstance(value, dict) else clean(value)


def reward_text(awards):
    """Keep quantities, loan duration and named rewards; never guess generic items."""
    lines = []
    for award in awards or []:
        if not isinstance(award, dict):
            continue
        player = award.get("playerItem") or award.get("player") or {}
        name = clean(award.get("name"))
        if player:
            name = clean(player.get("commonName") or player.get("nickname")) or " ".join(
                clean(player.get(k)) for k in ("firstName", "lastName")
            ).strip() or name
            qualifiers = [clean(player.get("overall")), clean(player.get("rarityName"))]
            name = " ".join(x for x in qualifiers + [name] if x)
        elif not name:
            if award.get("xp"):
                name = f"{award['xp']:,} SP"
            elif award.get("coins"):
                name = f"{award['coins']:,} coins"
            elif award.get("champsPoints"):
                name = f"{award['champsPoints']:,} Champions qualification points"
            else:
                name = (named(award.get("pack")) or named(award.get("item")) or
                        clean(award.get("evolutionName")) or named(award.get("other")) or
                        named(award.get("eventToken")) or named(award.get("pool")))
        if not name:
            name = "Reward details not listed by FUT.GG"
        if award.get("evoUnlock") or award.get("type") == "EVO_UNLOCK" or award.get("evolutionName"):
            name = "Evolution unlock: " + name
        if award.get("loanAmount"):
            name = re.sub(r"\s*\(Loan\)$", "", name, flags=re.I)
            name += f" (loan: {award['loanAmount']} matches)"
        quantity = award.get("count")
        if isinstance(quantity, (int, float)) and quantity > 1:
            name = f"{quantity:g} × {name}"
        if award.get("isUntradeable") is True or award.get("isTradable") is False:
            name += " (untradeable)"
        elif (award.get("isUntradeable") is False or award.get("isTradable") is True) and not award.get("coins"):
            name += " (tradeable)"
        lines.append(safe_text(name))
    return "\n".join(lines) if lines else MISSING


def field(name, value, inline=False):
    return {"name": clean(name), "value": clean(value) or MISSING, "inline": inline}


def objective_fields(obj):
    tasks = obj.get("objectives")
    tasks = tasks if isinstance(tasks, list) else []
    count = obj.get("tasksCount")
    count = count if isinstance(count, int) else len(tasks)
    required = obj.get("objectivesCompletionCount")
    completion = (f"Complete {required} of {count} tasks" if isinstance(required, int)
                  and required > 0 else f"Complete all {count} tasks")
    result = [field("Group reward — " + completion, reward_text(obj.get("awards")))]
    if obj.get("isLocked"):
        result.append(field("Availability", "Locked on FUT.GG — check the unlock requirements in game."))
    if not tasks:
        result.append(field("Tasks", MISSING))
    for index, task in enumerate(tasks, 1):
        description = safe_text(task.get("description")) or MISSING
        result.append(field(f"{index}. {safe_text(task.get('name')) or 'Task'}",
                            description + "\n\n**Reward:**\n" + reward_text(task.get("awards"))))
    if count > len(tasks):
        result.append(field("Task coverage", f"FUT.GG lists {count} tasks, but only {len(tasks)} task details are available. Open the source for the rest."))
    return result


def sbc_fields(sbc):
    challenges = sbc.get("challenges")
    challenges = challenges if isinstance(challenges, list) else []
    count = sbc.get("challengesCount")
    count = count if isinstance(count, int) else len(challenges)
    completion = "Complete 1 challenge" if count == 1 else f"Complete all {count} challenges"
    result = [field("Group reward — " + completion, reward_text(sbc.get("awards")))]
    if not challenges:
        result.append(field("Challenge requirements", MISSING))
    for index, challenge in enumerate(challenges, 1):
        lines = []
        streamlined = challenge.get("isStreamlined") or challenge.get("challengeType") == "ONE_CLICK_CHALLENGE"
        if streamlined:
            score = challenge.get("scoreRequirement")
            lines.append(f"**Streamlined SBC — target score: {score:,}**" if isinstance(score, (int, float))
                         else "**Streamlined SBC — target score not listed by FUT.GG.**")
            lines.append("Eligible player requirements:")
        requirements = challenge.get("requirementsText") or []
        if challenge.get("eligibilityOperation") == "OR" and len(requirements) > 1:
            lines.append("Meet any ONE of these requirements:")
        elif challenge.get("eligibilityOperation") == "AND" and len(requirements) > 1:
            lines.append("Meet ALL of these requirements:")
        lines.extend("• " + safe_text(r) for r in requirements)
        if not requirements:
            lines.append(MISSING)
        awards = challenge.get("awards")
        reward = ("No separate challenge reward listed; see the group reward above."
                  if awards == [] else reward_text(awards))
        lines.extend(["", "**Challenge reward:**", reward])
        result.append(field(f"{index}. {safe_text(challenge.get('name')) or 'Challenge'}", "\n".join(lines)))
    if count > len(challenges):
        result.append(field("Challenge coverage", f"FUT.GG lists {count} challenges, but only {len(challenges)} challenge details are available. Open the source for the rest."))
    return result


def kv_text(rows):
    """Readable source labels and caps, without discarding long requirement lines."""
    lines = []
    for row in rows or []:
        if not isinstance(row, dict):
            lines.append(safe_text(row))
            continue
        cap = clean(row.get("maxValue"))
        cap = re.sub(r"^\(\^(.+)\)$", r"(max \1)", cap)
        value = " ".join(x for x in [clean(row.get("value")), cap] if x)
        lines.append(f"{safe_text(row.get('label'))}: {safe_text(value)}")
    return "\n".join(lines) or MISSING


def evolution_fields(evo):
    result = []
    for key, heading in (("objectiveGroupName", "Unlock via objective"), ("sbcName", "Unlock via SBC")):
        if evo.get(key):
            result.append(field(heading, safe_text(evo[key])))
    requirements = evo.get("requirementsText")
    eligibility = ("No player restrictions listed by FUT.GG; check in game."
                   if requirements == [] else kv_text(requirements))
    result.append(field("Player eligibility", eligibility))
    levels = evo.get("levels")
    levels = levels if isinstance(levels, list) else []
    has_choices = evo.get("hasUpgradeChoices") or any(l.get("hasUpgradeChoices") or len(l.get("upgradeOptions") or []) > 1 for l in levels)
    if has_choices:
        result.append(field("Upgrade choices", "Choose one option at each choice level. Options below are alternatives, not combined rewards."))
    elif evo.get("totalUpgradesText"):
        result.append(field("Overall upgrades", kv_text(evo["totalUpgradesText"])))
    if not levels:
        result.append(field("Completion tasks & level rewards", MISSING))
    for position, level in enumerate(levels, 1):
        label = f"Level {level.get('idx') or position}"
        challenges = level.get("challenges")
        if isinstance(challenges, list) and challenges:
            tasks = "\n".join(f"{n}. {safe_text(task)}" for n, task in enumerate(challenges, 1))
        elif challenges == [] and evo.get("isCosmeticEvolution"):
            tasks = "Cosmetic evolution — no match tasks listed by FUT.GG."
        elif challenges == [] and evo.get("isTimed") and level.get("readableTrainingTime"):
            tasks = "Training time: " + safe_text(level["readableTrainingTime"])
        elif challenges == []:
            tasks = "No match tasks listed by FUT.GG."
        else:
            tasks = MISSING
        result.append(field(label + " — Tasks", tasks))
        options = level.get("upgradeOptions") or []
        if level.get("hasUpgradeChoices") or len(options) > 1:
            if not options:
                result.append(field(label + " — Reward choices", MISSING))
            for n, option in enumerate(options, 1):
                result.append(field(f"{label} — Reward option {option.get('idx') or n} (choose one)",
                                    kv_text(option.get("totalUpgradesText"))))
        else:
            result.append(field(label + " — Rewards", kv_text(level.get("totalUpgradesText"))))
    return result


def split_text(text, limit):
    """Lossless split, preferring a newline then a space, including a giant single line."""
    text = clean(text)
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        else:
            cut = min(cut + 1, limit)
        yield text[:cut]
        text = text[cut:]
    if text:
        yield text


def full_text(embed):
    lines = []
    for page in message_embeds(embed):
        lines.extend([page.get("title", ""), page.get("url", ""), "", page.get("description", "")])
        for item in page.get("fields", []):
            lines.extend(["", item.get("name", ""), item.get("value", "")])
    return "\n".join(lines)


def embed_size(embed):
    return (len(embed.get("title", "")) + len(embed.get("description", "")) +
            len((embed.get("footer") or {}).get("text", "")) +
            len((embed.get("author") or {}).get("name", "")) +
            sum(len(f.get("name", "")) + len(f.get("value", "")) for f in embed.get("fields", [])))


def finalize_embed(embed):
    """Keep every task/reward inline, using separately delivered continuation pages.

    The first embed remains a dict for existing callers. Its private
    ``_continuation_embeds`` list contains the remaining numbered pages. Use
    ``message_embeds`` before sending, and send ONE returned embed per message:
    Discord's 6000-character budget is shared by all embeds in one message.
    """
    embed = copy.deepcopy(embed)
    embed.pop("_full_details_text", None)
    embed.pop("_continuation_embeds", None)
    embed.pop("footer", None)
    original_title = clean(embed.get("title")) or "FUT.GG update"
    description = clean(embed.get("description"))
    fields = []

    def add_fields(name, value, inline=False):
        for index, chunk in enumerate(split_text(value or MISSING, 1024)):
            # The first heading retains its entire original name. Subsequent
            # headings repeat a shortened label, without discarding source data.
            label = name if index == 0 else name[:248] + " (cont.)"
            fields.append({"name": label, "value": chunk,
                           "inline": bool(inline) and index == 0})

    title_in_fields = len(original_title) > 256
    embed["title"] = original_title[:230] if title_in_fields else original_title
    if title_in_fields:
        add_fields("Full title", original_title)
    if len(description) > 4096:
        # Putting a long description into ordered fields avoids losing the tail
        # or separating it from the item's subsequent task/reward content.
        embed["description"] = ""
        add_fields("Description", description)
    else:
        embed["description"] = description
    author = embed.get("author") or {}
    if len(clean(author.get("name"))) > 256:
        add_fields("Author", clean(author["name"]))
        author["name"] = clean(author["name"])[:256]
    for position, item in enumerate(embed.get("fields", []), 1):
        name = clean(item.get("name")) or "Details"
        if len(name) > 256:
            # A source task name is content too, not disposable decoration.
            add_fields(f"Section {position} title", name)
            name = f"Section {position} — Details"
        add_fields(name, clean(item.get("value")) or MISSING, item.get("inline"))
    embed["fields"] = fields
    if len(fields) <= 25 and embed_size(embed) <= 6000:
        return embed

    if len(original_title) > 230 and not title_in_fields:
        # Numbered page titles need a suffix. Preserve any title shortened to
        # make room for it in a full-title field on the first page.
        saved_fields = fields
        fields = []
        add_fields("Full title", original_title)
        fields.extend(saved_fields)

    def new_page(first=False):
        page = copy.deepcopy(embed) if first else {
            key: copy.deepcopy(embed[key]) for key in ("color", "url", "timestamp") if key in embed
        }
        page["title"] = original_title[:230]
        page["fields"] = []
        return page

    pages = []
    page = new_page(first=True)
    for item in fields:
        # Reserve the maximum possible 256-char title before the final number
        # of pages is known; numbering can never push a page over its budget.
        reserved_size = embed_size(page) - len(page["title"]) + 256
        if len(page["fields"]) >= 25 or reserved_size + len(item["name"]) + len(item["value"]) > 6000:
            pages.append(page)
            page = new_page()
        page["fields"].append(item)
    pages.append(page)
    for index, page in enumerate(pages, 1):
        suffix = f" ({index}/{len(pages)})"
        page["title"] = original_title[:256 - len(suffix)] + suffix
    pages[0]["_continuation_embeds"] = pages[1:]
    return pages[0]


def message_embeds(embed):
    """Return standalone ordered pages without mutating input or leaking metadata."""
    pages = [embed] + list(embed.get("_continuation_embeds") or [])
    return [{key: copy.deepcopy(value) for key, value in page.items()
             if key not in ("_continuation_embeds", "_full_details_text")} for page in pages]
