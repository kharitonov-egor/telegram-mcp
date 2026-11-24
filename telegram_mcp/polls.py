"""Read-only poll rendering shared by get_poll, get_messages and get_media_info.

Nothing here votes. Telegram hides per-option counts from an account that has
not voted in an open poll; the summary reports that instead of unlocking them.
"""

from typing import Any, Optional

from telethon import types

from sanitize import sanitize_name, sanitize_user_content

HIDDEN_RESULTS_NOTE = (
    "This account has not voted and Telegram hides the per-option results until it "
    "votes or the poll closes. Counts and voters are not shown. get_poll never votes "
    "to unlock them."
)
MIN_RESULTS_NOTE = (
    "Telegram returned partial (min) results, so per-option counts and this "
    "account's choice may be missing or stale."
)


def poll_media(msg: Any) -> Optional[types.MessageMediaPoll]:
    media = getattr(msg, "media", None)
    return media if isinstance(media, types.MessageMediaPoll) else None


def poll_text(value: Any) -> str:
    raw = getattr(value, "text", value)
    return sanitize_user_content(raw) if isinstance(raw, str) and raw else ""


def _option_entries(poll: types.Poll, by_option: dict) -> list:
    options = []
    for index, answer in enumerate(poll.answers):
        entry: dict = {"index": index, "text": poll_text(answer.text)}
        voters = by_option.get(answer.option)
        if voters is not None:
            entry["votes"] = voters.voters or 0
            entry["chosen"] = bool(voters.chosen)
            if voters.correct:
                entry["correct"] = True
        options.append(entry)
    return options


def _quiz_details(options: list, results: Any) -> dict:
    details: dict = {}
    correct = [o["text"] for o in options if o.get("correct")]
    if correct:
        details["correct_answers"] = correct
    solution = getattr(results, "solution", None)
    if solution:
        details["solution"] = sanitize_user_content(solution)
    return details


def _results_note(counts_visible: bool, is_min: bool, voted: bool, closed: bool):
    if not counts_visible and not voted and not closed:
        return HIDDEN_RESULTS_NOTE
    if is_min:
        return MIN_RESULTS_NOTE
    if not counts_visible:
        return "Telegram did not include per-option results for this poll."
    return None


def summarize_poll(media: types.MessageMediaPoll) -> dict:
    """Everything Telegram already sent with the message, without another request."""
    poll = media.poll
    results = media.results
    by_option = {r.option: r for r in (getattr(results, "results", None) or [])}
    counts_visible = bool(by_option)
    is_min = bool(getattr(results, "min", False))
    options = _option_entries(poll, by_option)
    chosen = [o["text"] for o in options if o.get("chosen")]

    summary: dict = {
        "question": poll_text(poll.question),
        "options": options,
        "total_voters": getattr(results, "total_voters", None),
        "voted": bool(chosen),
        "chosen_by_me": chosen,
        "closed": bool(poll.closed),
        "anonymous": not poll.public_voters,
        "public_voters": bool(poll.public_voters),
        "multiple_choice": bool(poll.multiple_choice),
        "quiz": bool(poll.quiz),
        "results_visible": counts_visible and not is_min,
    }
    if getattr(poll, "hide_results_until_close", False):
        summary["results_hidden_until_close"] = True
    if poll.close_date:
        summary["close_date"] = poll.close_date.isoformat()
    if poll.quiz:
        summary.update(_quiz_details(options, results))
    note = _results_note(counts_visible, is_min, bool(chosen), bool(poll.closed))
    if note:
        summary["results_note"] = note
    return summary


def _inline_option(option: dict) -> str:
    item = f"[{option['index']}] {option['text']}"
    if "votes" in option:
        item += f" ({option['votes']})"
    if option.get("chosen"):
        item += " ✓me"
    if option.get("correct"):
        item += " ✓correct"
    return item


def _inline_flags(s: dict) -> list:
    flags = [f"{s['total_voters']} voters"] if s["total_voters"] is not None else []
    flags.append("public" if s["public_voters"] else "anonymous")
    named = [("multiple_choice", "multiple choice"), ("quiz", "quiz"), ("closed", "closed")]
    flags.extend(label for key, label in named if s[key])
    if not s["results_visible"] and not s["voted"]:
        flags.append("results hidden until you vote")
    return flags


def poll_inline_text(msg: Any) -> Optional[str]:
    """One-line poll rendering for message listings, or None for non-poll messages."""
    media = poll_media(msg)
    if media is None:
        return None
    s = summarize_poll(media)
    options = ", ".join(_inline_option(o) for o in s["options"])
    text = f"Poll: {s['question']}; options: {options}; {', '.join(_inline_flags(s))}"
    return text.replace("\n", "\\n")


def peer_id(peer: Any) -> Optional[int]:
    for attr in ("user_id", "chat_id", "channel_id"):
        value = getattr(peer, attr, None)
        if value is not None:
            return value
    return None


def describe_voter(peer: Any, users: dict, chats: dict) -> dict:
    pid = peer_id(peer)
    voter: dict = {"id": pid}
    if isinstance(peer, types.PeerUser):
        user = users.get(pid)
        if user is not None:
            name = " ".join(p for p in (user.first_name, user.last_name) if p)
            voter["name"] = sanitize_name(name) if name else "Deleted Account"
            if user.username:
                voter["username"] = sanitize_name(user.username)
    else:
        chat = chats.get(pid)
        title = getattr(chat, "title", None)
        if title:
            voter["name"] = sanitize_name(title)
    return voter


def vote_options(vote: Any) -> list:
    if isinstance(vote, types.MessagePeerVoteMultiple):
        return list(vote.options)
    option = getattr(vote, "option", None)
    return [option] if option is not None else []
