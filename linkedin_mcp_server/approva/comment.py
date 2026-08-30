"""Comment on a LinkedIn post.

The comment box is the same kind of animal as the post composer -- a
React-hydrated contenteditable that ignores an assigned ``.value`` and only
builds its document from real key events -- so the focus and read-back path is
imported from :mod:`composer` rather than written twice.

Three decisions carry this module, and each replaces a guess with a fact the
page can be asked for:

*Which box.* A post permalink page renders exactly one editable element: the
comment box. The share composer lives on ``/feed/``, not here, and reply boxes
only mount after a Reply click this module never performs. So the count is the
signal -- one visible editor is unambiguous, and more than one is resolved by
scoping to the post's own container. Neither resolving means we stop and say
so, which is better than typing into whatever came first.

*Which button submits.* Not the one labelled "Comment": that verb is
locale-dependent, and the repo's scraping rules forbid classifying on text.
What is locale-independent is the *transition* -- LinkedIn keeps the submit
control disabled (or unmounted) until the box has content, so the button that
goes disabled->enabled, or appears from nothing, when we type is the submit
button. The button set is scanned before and after typing and diffed.

*Whether it worked.* Two signals, both diffed against the state captured
before typing: how many times our text appears in the rendered body with the
editor subtree removed, and which ``urn:li:comment:(...)`` values appear in the
page HTML. Either confirms; neither can be satisfied by a comment that was
already on the post, which is the point. The editor is excluded from the count
because an unsent draft still sitting in the box would otherwise confirm
itself.

The first attempt read those URNs off ``data-id`` attributes and found nothing:
measured against the live permalink page (2026-08-30), LinkedIn's newer
hashed-class rendering exposes neither a ``data-id`` carrying a comment URN nor
a ``data-urn`` on the post itself. The URN is in the HTML; it is simply not an
attribute. Per the spec, a submit that does not confirm is a failure -- silent
double-posting is the failure mode that costs the account.

Newlines are typed as Shift+Enter, never as a bare Enter: in some LinkedIn
renderings Enter submits the comment, which would post half a sentence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlunparse

from linkedin_mcp_server.approva.composer import (
    VISIBLE_JS,
    focus_contenteditable,
    read_contenteditable,
)

logger = logging.getLogger(__name__)

# LinkedIn's own limit on a comment body.
MAX_COMMENT_CHARS = 1250

# The spec's "2-6 s of human-plausible delay", read as the pause between
# finishing the text and submitting it. Applied per-keystroke it would take
# hours for a sentence.
_PAUSE_BEFORE_SUBMIT = (2.0, 6.0)

# Per-keystroke cadence, randomised per call. The composer types at a flat
# 12 ms; a comment is short enough that a little jitter costs nothing.
_KEY_DELAY_MS = (12, 28)

# The attribute used to tag buttons between the before and after scans. React
# re-renders freely, so the two scans cannot be matched by index alone.
_MARK_ATTR = "data-approva-btn"

_ACTIVITY_URN_RE = re.compile(r"urn:li:(?:activity|ugcPost|share):\d+")
_POSTS_SLUG_ACTIVITY_RE = re.compile(r"-activity-(\d+)")


class CommentError(RuntimeError):
    """A step of the comment flow could not be completed."""


# --------------------------------------------------------------------------
# Validation -- everything here runs before the browser is touched
# --------------------------------------------------------------------------


def normalize_post_url(post_url: str) -> tuple[str, str | None]:
    """Resolve ``post_url`` to an absolute LinkedIn URL and its activity URN.

    Accepts the two permalink shapes LinkedIn hands out -- ``/feed/update/``
    and ``/posts/`` -- and a bare ``urn:li:activity:...``. Returns
    ``(url, urn_or_none)``; the URN is used to scope the page and is optional
    because a ``/posts/`` slug does not always carry one.

    The host is checked rather than assumed. ``post_url`` reaches this server
    from a queue, and navigating an authenticated LinkedIn browser to an
    arbitrary host on the strength of a typo is not a risk worth carrying.
    """
    raw = (post_url or "").strip()
    if not raw:
        raise CommentError("post_url is empty.")

    if raw.startswith("urn:li:"):
        if not _ACTIVITY_URN_RE.fullmatch(raw):
            raise CommentError(
                f"{raw!r} is not an activity URN; expected the shape "
                "'urn:li:activity:7123456789'."
            )
        return f"https://www.linkedin.com/feed/update/{raw}/", raw

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        raise CommentError(
            f"post_url must be a linkedin.com address; got host {host or '(none)'!r}. "
            "Refusing to navigate the authenticated browser off LinkedIn."
        )

    path = parsed.path
    if "/feed/update/" not in path and "/posts/" not in path:
        raise CommentError(
            f"{raw!r} is not a post permalink. Expected a path containing "
            "'/feed/update/' or '/posts/'."
        )

    # LinkedIn percent-encodes the URN's colons in some copy paths.
    decoded = unquote(path)
    match = _ACTIVITY_URN_RE.search(decoded)
    if match:
        urn: str | None = match.group(0)
    else:
        slug = _POSTS_SLUG_ACTIVITY_RE.search(decoded)
        urn = f"urn:li:activity:{slug.group(1)}" if slug else None

    # Tracking parameters are dropped: they are noise in the returned
    # comment_url and change nothing about which post loads.
    clean = urlunparse((parsed.scheme or "https", parsed.netloc, path, "", "", ""))
    return clean, urn


def validate_text(text: str) -> str:
    """Check the comment body before anything is navigated or typed."""
    body = (text or "").strip()
    if not body:
        raise CommentError("Refusing to post an empty comment.")
    if len(body) > MAX_COMMENT_CHARS:
        raise CommentError(
            f"Comment is {len(body)} characters; LinkedIn's limit is "
            f"{MAX_COMMENT_CHARS}."
        )
    return body


# --------------------------------------------------------------------------
# Page interrogation
# --------------------------------------------------------------------------


def _selection_body(urn: str | None) -> str:
    """JS declaring ``postRoot``, ``boxes``, ``scopedBoxes`` and ``chosen``.

    Shared verbatim by every evaluate in this module so the editor the stats
    describe is always the editor the typing goes into. ``visible`` must
    already be in scope.

    The post container is matched on a ``data-urn``/``data-id`` that is
    *exactly* an activity URN. A prefix or substring match would also select
    every comment on the page, because a comment's own id embeds the post's:
    ``urn:li:comment:(urn:li:activity:7123,456)``.
    """
    return (
        "    const wanted = " + json.dumps(urn) + ";\n"
        r"""
    const rootAttr = (el) =>
        el.getAttribute('data-urn') || el.getAttribute('data-id') || '';
    const isPostRoot = (el) => {
        const v = rootAttr(el);
        return /^urn:li:(activity|ugcPost|share):\d+$/.test(v)
               && (!wanted || v === wanted);
    };
    const postRoot = Array.from(document.querySelectorAll('[data-urn], [data-id]'))
        .filter(visible).find(isPostRoot) || null;
    const boxes = Array.from(document.querySelectorAll(
        '[role="textbox"][contenteditable], [role="textbox"], [contenteditable="true"]'
    )).filter(visible);
    const scopedBoxes = postRoot ? boxes.filter((el) => postRoot.contains(el)) : [];
    // One editable on a post page is unambiguous. More than one only happens
    // when a reply box is open, and then the post-scoped list decides; the
    // top-level comment box precedes any reply box in document order.
    const chosen = boxes.length === 1
        ? boxes[0]
        : (scopedBoxes.length ? scopedBoxes[0] : null);
    const scanRoot = postRoot || document.querySelector('main') || document.body;
"""
    )


def _editor_js(urn: str | None) -> str:
    """A JS expression returning the comment editor, for the shared helpers.

    Self-contained: it carries its own ``visible`` rather than borrowing the
    caller's, so it can be dropped into any evaluate. The declaration is
    scoped to the IIFE and shadows an outer one harmlessly.
    """
    return "(() => {" + VISIBLE_JS + _selection_body(urn) + " return chosen; })()"


async def _editor_stats(page: Any, urn: str | None) -> dict[str, Any]:
    """Report what the page offers, for both waiting and error messages."""
    result = await page.evaluate(
        "() => {"
        + VISIBLE_JS
        + _selection_body(urn)
        + """
        return {
            found: !!chosen,
            post_root_found: !!postRoot,
            editable_count: boxes.length,
            scoped_editable_count: scopedBoxes.length,
        };
        }"""
    )
    return result if isinstance(result, dict) else {"found": False}


async def _scroll_post_into_view(page: Any, urn: str | None) -> None:
    """Nudge the post into view so LinkedIn mounts the comment box.

    The box is lazily mounted on some renderings; scrolling is the only thing
    that reliably triggers it, and it reads as ordinary browsing.
    """
    try:
        await page.evaluate(
            "() => {"
            + VISIBLE_JS
            + _selection_body(urn)
            + """
            (postRoot || document.body).scrollIntoView(
                { block: 'end', behavior: 'instant' });
            }"""
        )
    except Exception:
        logger.debug("Could not scroll the post into view", exc_info=True)


async def _wait_for_comment_editor(
    page: Any, urn: str | None, *, timeout: float = 15.0
) -> dict[str, Any]:
    """Wait for the comment box to mount, scrolling to provoke it."""
    deadline = asyncio.get_event_loop().time() + timeout
    stats: dict[str, Any] = {"found": False}
    while asyncio.get_event_loop().time() < deadline:
        stats = await _editor_stats(page, urn)
        if stats.get("found"):
            return stats
        await _scroll_post_into_view(page, urn)
        await asyncio.sleep(0.5)
    return stats


async def _scan_buttons(page: Any, urn: str | None, *, mark: bool) -> list[dict]:
    """Record every visible control near the post, optionally tagging it.

    Tagging happens on the first pass only: the second pass has to be able to
    tell a button it already saw from one that mounted while we typed, and a
    submit control that appears only once the box has content is one of the
    two shapes LinkedIn uses.
    """
    result = await page.evaluate(
        "([shouldMark, attr]) => {"
        + VISIBLE_JS
        + _selection_body(urn)
        + """
        const btns = Array.from(
            scanRoot.querySelectorAll('button, [role="button"]')).filter(visible);
        return btns.map((el, i) => {
            if (shouldMark) el.setAttribute(attr, String(i));
            return {
                mark: el.getAttribute(attr),
                enabled: !el.disabled
                         && el.getAttribute('aria-disabled') !== 'true',
                aria: el.getAttribute('aria-label'),
                text: (el.innerText || '').trim().slice(0, 40),
            };
        });
        }""",
        [mark, _MARK_ATTR],
    )
    return result if isinstance(result, list) else []


async def _clear_marks(page: Any) -> None:
    """Remove our tags, so the page is left as we found it."""
    try:
        await page.evaluate(
            "(attr) => { document.querySelectorAll('[' + attr + ']')"
            ".forEach((el) => el.removeAttribute(attr)); }",
            _MARK_ATTR,
        )
    except Exception:
        logger.debug("Could not clear button marks", exc_info=True)


async def _confirmation_state(page: Any, needle: str) -> dict[str, Any]:
    """Read the two before/after signals that a comment landed.

    Measured against the live permalink page (2026-08-30), which is LinkedIn's
    newer hashed-class rendering: it exposes **no** ``data-id`` carrying a
    comment URN and no ``data-urn`` on the post, so an attribute scan finds
    nothing. What it does expose is the comment URN in the page HTML and the
    comment text in the rendered body. Both are read here:

    ``occurrences``
        How many times our text appears with the editor subtree removed. The
        removal is what makes this a confirmation rather than a tautology --
        an unsent draft still sitting in the box must not read as a comment.

    ``urns``
        Every ``urn:li:comment:(...)`` in the HTML, whatever element or payload
        carries it. Diffed, this yields the new comment's own URN and so its
        permalink.

    Whitespace and case are normalised to match :func:`_normalize`.
    """
    result = await page.evaluate(
        """([needle]) => {
        const sel = '[role="textbox"][contenteditable], [role="textbox"],'
                    + ' [contenteditable="true"]';
        // Count in a copy with the editors stripped, so a draft that never
        // submitted cannot confirm itself.
        const clone = document.body.cloneNode(true);
        clone.querySelectorAll(sel).forEach((e) => e.remove());
        const text = (clone.innerText || '').replace(/\s+/g, ' ').toLowerCase();
        const occurrences = needle ? text.split(needle).length - 1 : 0;
        const html = document.documentElement.outerHTML;
        const urns = Array.from(new Set(
            html.match(/urn:li:comment:\([^)]{0,120}\)/g) || []));
        return { occurrences, urns };
        }""",
        [needle],
    )
    if not isinstance(result, dict):
        return {"occurrences": 0, "urns": []}
    return result


# --------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Collapse whitespace so a DOM read can be compared to what we typed."""
    return " ".join(text.split()).casefold()


def _needle(body: str) -> str:
    """The prefix used to recognise our comment once LinkedIn renders it.

    A prefix rather than the whole body: LinkedIn truncates a long comment
    behind a "see more" control, so the tail may genuinely not be in the DOM.
    """
    return _normalize(body)[:40]


async def _click_submit(
    page: Any, urn: str | None, before: list[dict]
) -> dict[str, Any]:
    """Click the control that typing enabled, and report which one that was.

    Candidates are buttons that went disabled->enabled, plus buttons that were
    not there at all before. The last in document order wins: the submit
    control sits at the end of the comment form, after the emoji and media
    affordances that share its container.
    """
    result = await page.evaluate(
        "([before, attr]) => {"
        + VISIBLE_JS
        + _selection_body(urn)
        + """
        const wasEnabled = new Map();
        for (const b of before) {
            if (b.mark !== null && b.mark !== undefined)
                wasEnabled.set(String(b.mark), !!b.enabled);
        }
        const btns = Array.from(
            scanRoot.querySelectorAll('button, [role="button"]')).filter(visible);
        const describe = (el) => ({
            mark: el.getAttribute(attr),
            aria: el.getAttribute('aria-label'),
            text: (el.innerText || '').trim().slice(0, 40),
        });
        const candidates = [];
        for (const el of btns) {
            const enabled = !el.disabled
                            && el.getAttribute('aria-disabled') !== 'true';
            if (!enabled) continue;
            const mark = el.getAttribute(attr);
            const seen = mark !== null && wasEnabled.has(mark);
            // Enabled by our typing, or mounted by it. Either is the submit
            // control; neither depends on reading a label.
            if (!seen || wasEnabled.get(mark) === false) {
                candidates.push(el);
            }
        }
        if (!candidates.length) {
            return { clicked: null, candidates: [], buttons: btns.map(describe) };
        }
        const target = candidates[candidates.length - 1];
        const info = describe(target);
        target.click();
        return {
            clicked: info,
            candidates: candidates.map(describe),
            buttons: btns.map(describe),
        };
        }""",
        [before, _MARK_ATTR],
    )
    return result if isinstance(result, dict) else {"clicked": None, "buttons": []}


async def _await_confirmation(
    page: Any,
    needle: str,
    before: dict[str, Any],
    *,
    timeout: float = 20.0,
) -> dict[str, Any] | None:
    """Wait for evidence that our comment, and not a previous one, rendered.

    Two independent signals, either of which confirms, both of them diffs
    against the state captured before we typed -- so a comment that was
    already on the post cannot satisfy either.

    The text count is the primary signal because it is the one that says the
    comment is *ours*. A new URN is accepted on its own because LinkedIn
    truncates a long comment behind a "see more", which can keep the tail out
    of the rendered text; it only attributes a permalink when exactly one new
    URN appeared, since two would leave us guessing which is ours.
    """
    before_urns = set(before.get("urns") or [])
    before_count = int(before.get("occurrences") or 0)
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        state = await _confirmation_state(page, needle)
        new_urns = [u for u in (state.get("urns") or []) if u not in before_urns]
        grew = int(state.get("occurrences") or 0) > before_count
        if grew or len(new_urns) == 1:
            return {
                "signal": "text" if grew else "urn",
                "comment_urn": new_urns[0] if len(new_urns) == 1 else None,
                "occurrences": state.get("occurrences"),
                "new_urns": len(new_urns),
            }
        await asyncio.sleep(1.0)
    return None


# --------------------------------------------------------------------------
# The write
# --------------------------------------------------------------------------


async def create_comment(
    page: Any,
    goto: Any,
    *,
    post_url: str,
    text: str,
) -> dict[str, Any]:
    """Comment on a LinkedIn post, confirming the comment rendered.

    One write, no retry. Every failure path raises :class:`CommentError` with
    what the page actually showed, because the caller's only safe response to
    an unconfirmed submit is to look rather than to try again.
    """
    body = validate_text(text)
    url, urn = normalize_post_url(post_url)

    await goto(url)
    await asyncio.sleep(1.5)

    landed = page.url or ""
    if "/feed/update/" not in landed and "/posts/" not in landed:
        raise CommentError(
            f"{url} did not resolve to a post; LinkedIn landed on {landed}. "
            "The post may be deleted, private, or the URL may be wrong."
        )

    stats = await _wait_for_comment_editor(page, urn)
    if not stats.get("found"):
        raise CommentError(
            "Could not identify the comment box on this post "
            f"(editables on page: {stats.get('editable_count')}, "
            f"within the post: {stats.get('scoped_editable_count')}, "
            f"post container found: {stats.get('post_root_found')}). "
            "Nothing was typed."
        )

    editor_js = _editor_js(urn)
    if not await focus_contenteditable(page, editor_js):
        raise CommentError("Could not focus the comment box. Nothing was typed.")

    before_buttons = await _scan_buttons(page, urn, mark=True)
    needle = _needle(body)
    before = await _confirmation_state(page, needle)

    await asyncio.sleep(0.2)
    key_delay = random.randint(*_KEY_DELAY_MS)
    # Shift+Enter for line breaks: a bare Enter submits the comment in some
    # LinkedIn renderings, which would post the first line on its own.
    for index, line in enumerate(body.split("\n")):
        if index:
            await page.keyboard.press("Shift+Enter")
        if line:
            await page.keyboard.type(line, delay=key_delay)
    await asyncio.sleep(0.8)

    landed_text = await read_contenteditable(page, editor_js)
    if _needle(body) not in _normalize(landed_text):
        await _clear_marks(page)
        raise CommentError(
            "The comment box did not receive the text (what it contains does "
            "not match what was typed). Nothing was submitted."
        )

    pause = random.uniform(*_PAUSE_BEFORE_SUBMIT)
    logger.info(
        "approva_comment: typed %d chars on %s, pausing %.1fs before submit",
        len(body),
        url,
        pause,
    )
    await asyncio.sleep(pause)

    submit = await _click_submit(page, urn, before_buttons)
    if not submit.get("clicked"):
        await _clear_marks(page)
        raise CommentError(
            "No control became available when the comment was typed, so there "
            "was nothing to submit; the draft is still in the box. Controls "
            f"seen: {submit.get('buttons')}"
        )

    logger.info("approva_comment: submitted via %s", submit["clicked"])
    await asyncio.sleep(2.0)

    landed = await _await_confirmation(page, needle, before)
    editor_cleared = not _normalize(await read_contenteditable(page, editor_js))
    await _clear_marks(page)

    if landed is None:
        raise CommentError(
            "Submitted the comment but it did not appear on the post within "
            "20s (editor cleared: {}). Treat this as a failure and look at the "
            "post before commenting again -- retrying blind risks posting "
            "twice. Submitted via: {}".format(editor_cleared, submit["clicked"])
        )

    comment_urn = landed.get("comment_urn")
    return {
        "ok": True,
        "post_url": url,
        "comment_urn": comment_urn,
        # Absent when the page exposed no comment URN, or more than one new
        # one: the spec marks this optional for exactly that reason.
        "comment_url": (
            f"{url}?commentUrn={quote(comment_urn, safe='')}" if comment_urn else None
        ),
        "confirmed_by": landed["signal"],
        "characters": len(body),
        "editor_cleared": editor_cleared,
        "submitted_via": submit["clicked"],
        "url": page.url,
    }
