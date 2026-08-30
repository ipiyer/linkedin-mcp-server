"""LinkedIn post composer: write a post, attach images, hand it to LinkedIn's
own scheduler.

Why the native scheduler rather than our own timed sender: LinkedIn holds the
post, so the founder's Mac can be asleep in a bag at a conference and the post
still goes out. It is also the same flow he performs by hand, so it introduces
no new behaviour on the account.

Interaction style follows the pattern upstream established in
``extractor.send_message``: patchright's actionability checks time out against
LinkedIn's React-hydrated contenteditable, so we focus via ``page.evaluate``
and type via ``page.keyboard.type``, which fires the real key events React
needs. Buttons are clicked from JavaScript for the same reason.

Selectors use ARIA attributes, roles and visible text only -- never layout
class names -- so they survive LinkedIn's frequent restyling.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FEED_URL = "https://www.linkedin.com/feed/"

# LinkedIn's own limit on a post body.
MAX_POST_CHARS = 3000

# Images per post that LinkedIn accepts in one share.
MAX_IMAGES = 20

_ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class ComposerError(RuntimeError):
    """A step of the composer flow could not be completed."""


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------


# Shared by every evaluate in this package: an element is present if it
# occupies space. Declared once so the composer and the comment box cannot
# drift apart on what "visible" means.
VISIBLE_JS = """
    const visible = (el) => !!(el.offsetWidth || el.offsetHeight
                               || el.getClientRects().length);
"""

# An expression rather than a statement block, so it can be dropped into any
# evaluate that already has ``visible`` in scope -- which is what lets the
# comment box share the focus and read-back helpers below.
COMPOSER_EDITOR_JS = """Array.from(document.querySelectorAll(
        '[role="textbox"][contenteditable], [role="textbox"], [contenteditable="true"]'
    )).find(visible)"""

# The composer is not a role="dialog" -- LinkedIn renders it as a plain
# overlay -- so it cannot be scoped by role. It is located instead from its
# editor, a TipTap/ProseMirror contenteditable, by walking up a fixed number
# of levels to the overlay that holds the action bar. Class names are never
# matched: LinkedIn's are hashed and rotate.
_COMPOSER_ROOT_JS = (
    VISIBLE_JS
    + "    const editor = "
    + COMPOSER_EDITOR_JS
    + """;
    let composerRoot = null;
    if (editor) {
        composerRoot = editor;
        for (let i = 0; i < 6 && composerRoot.parentElement; i++)
            composerRoot = composerRoot.parentElement;
    }
"""
)


async def focus_contenteditable(page: Any, element_js: str) -> bool:
    """Focus the contenteditable ``element_js`` selects, without an
    actionability check.

    ``element_js`` is a JavaScript *expression*, evaluated with ``visible``
    in scope, returning an element or null. Both of LinkedIn's editors -- the
    post composer and the comment box -- are React-hydrated contenteditables
    that build their document from real key events, so text has to be typed
    into a focused element rather than assigned, and patchright's
    actionability checks time out against both. Parameterising the element is
    what lets one implementation serve both.
    """
    return await page.evaluate(
        "() => {"
        + VISIBLE_JS
        + "    const editor = ("
        + element_js
        + """);
            if (!editor) return false;
            editor.focus();
            const sel = window.getSelection();
            if (sel && editor.lastChild) {
                const range = document.createRange();
                range.selectNodeContents(editor);
                range.collapse(false);
                sel.removeAllRanges();
                sel.addRange(range);
            }
            return document.activeElement === editor
                || editor.contains(document.activeElement);
        }"""
    )


async def read_contenteditable(page: Any, element_js: str) -> str:
    """Read back what an editor contains, to verify the typing landed."""
    text = await page.evaluate(
        "() => {"
        + VISIBLE_JS
        + "    const editor = ("
        + element_js
        + "); return editor ? (editor.innerText || '') : ''; }"
    )
    return text if isinstance(text, str) else ""


async def _find_editor(page: Any) -> bool:
    """Report whether a visible composer editor is on the page."""
    return await page.evaluate("() => {" + _COMPOSER_ROOT_JS + " return !!editor; }")


async def _click_by_text(
    page: Any, candidates: list[str], *, scoped: bool = True
) -> str | None:
    """Click the first visible, enabled control matching a candidate.

    Candidates are tried in order, so the caller expresses precedence. Each is
    ``aria:<substring>``, ``text:<substring>`` or ``exact:<full text>``.

    ``exact`` exists because the composer's action bar contains both "Post"
    and "Post to Anyone": a substring match on "post" clicks the audience
    selector instead of publishing.
    """
    return await page.evaluate(
        """
        ([candidates, scoped]) => {
        """
        + _COMPOSER_ROOT_JS
        + """
            const root = (scoped && composerRoot) ? composerRoot : document;
            const controls = Array.from(
                root.querySelectorAll('button, a, [role="button"], [role="menuitem"]')
            ).filter((el) => visible(el) && !el.disabled
                     && el.getAttribute('aria-disabled') !== 'true');
            for (const cand of candidates) {
                const idx = cand.indexOf(':');
                const kind = cand.slice(0, idx);
                const needle = cand.slice(idx + 1).toLowerCase();
                for (const el of controls) {
                    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                    const text = (el.innerText || el.textContent || '')
                        .trim().toLowerCase();
                    let hit = false;
                    if (kind === 'aria') hit = aria && aria.includes(needle);
                    else if (kind === 'text') hit = text && text.includes(needle);
                    else if (kind === 'exact') hit = text === needle || aria === needle;
                    if (hit) { el.click(); return cand; }
                }
            }
            return null;
        }
        """,
        [candidates, scoped],
    )


async def _wait_for_composer(page: Any, *, timeout: float = 12.0) -> bool:
    """Wait for the composer's editor to appear."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await _find_editor(page):
            return True
        await asyncio.sleep(0.25)
    return False


async def _focus_editor(page: Any) -> bool:
    """Focus the composer's editor without an actionability check."""
    return await focus_contenteditable(page, COMPOSER_EDITOR_JS)


async def _editor_text(page: Any) -> str:
    """Read back what the composer's editor contains."""
    return await read_contenteditable(page, COMPOSER_EDITOR_JS)


# --------------------------------------------------------------------------
# Inspection -- used while pinning selectors against the live DOM
# --------------------------------------------------------------------------


async def inspect_composer(page: Any, goto: Any) -> dict[str, Any]:
    """Open the composer and report every control it exposes, before and after
    text is entered.

    Development aid, and a live check that LinkedIn has not moved anything.
    Types a short probe string so controls that only appear once the post has
    content (the scheduler among them) are visible, then discards the draft.
    Nothing is submitted.
    """
    await goto(FEED_URL)
    await asyncio.sleep(1.5)

    trigger = await _click_by_text(
        page,
        ["aria:start a post", "aria:create a post", "text:start a post"],
        scoped=False,
    )
    await asyncio.sleep(2.0)

    # Find the composer by locating its editor and walking up, rather than
    # assuming a container role. The first pass assumed role="dialog" and was
    # wrong: LinkedIn renders the composer as a plain overlay.
    shape = await page.evaluate(
        """() => {
            const boxes = Array.from(document.querySelectorAll(
                '[contenteditable="true"], [role="textbox"]'));
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight
                                       || el.getClientRects().length);
            const box = boxes.find(visible);
            if (!box) return { found: false, editors: boxes.length };
            const chain = [];
            let el = box;
            for (let i = 0; i < 8 && el; i++) {
                chain.push({
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role'),
                    aria: el.getAttribute('aria-label'),
                    cls: (el.getAttribute('class') || '').slice(0, 90),
                    id: el.id || null,
                });
                el = el.parentElement;
            }
            return {
                found: true,
                editorAria: box.getAttribute('aria-label'),
                editorRole: box.getAttribute('role'),
                editorCls: (box.getAttribute('class') || '').slice(0, 120),
                ancestors: chain,
            };
        }"""
    )

    async def scan(label: str) -> dict[str, Any]:
        return {
            "phase": label,
            "buttons": await page.evaluate(
                """() => {
                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight
                                               || el.getClientRects().length);
                    // Only the composer overlay: everything under the element
                    // that contains the visible editor.
                    const box = Array.from(document.querySelectorAll(
                        '[contenteditable="true"], [role="textbox"]')).find(visible);
                    if (!box) return [];
                    let root = box;
                    for (let i = 0; i < 6 && root.parentElement; i++)
                        root = root.parentElement;
                    return Array.from(root.querySelectorAll('button, a, [role="button"]'))
                        .filter(visible)
                        .map((el) => ({
                            aria: el.getAttribute('aria-label'),
                            text: (el.innerText || '').trim().slice(0, 40),
                            id: el.id || null,
                        }));
                }"""
            ),
        }

    before = await scan("empty")

    focused = await _focus_editor(page)
    if focused:
        await page.keyboard.type("probe", delay=12)
        await asyncio.sleep(1.5)
    after = await scan("with-text")

    # The scheduler may sit behind the overflow control rather than the bar.
    expanded = await _click_by_text(
        page,
        ["aria:expand content types", "aria:more", "text:more"],
        scoped=False,
    )
    await asyncio.sleep(1.2)
    after_expand = (
        await scan("expanded") if expanded else {"phase": "expanded", "buttons": []}
    )

    # Decisive check: is a scheduling affordance anywhere on the page at all?
    schedule_hunt = await page.evaluate(
        """() => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight
                                       || el.getClientRects().length);
            const hits = [];
            for (const el of document.querySelectorAll('*')) {
                const aria = (el.getAttribute?.('aria-label') || '');
                const title = (el.getAttribute?.('title') || '');
                const own = Array.from(el.childNodes)
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent).join(' ');
                const hay = (aria + ' ' + title + ' ' + own).toLowerCase();
                if (/schedul|clock/.test(hay)) {
                    hits.push({
                        tag: el.tagName.toLowerCase(),
                        role: el.getAttribute('role'),
                        aria: aria || null,
                        title: title || null,
                        text: own.trim().slice(0, 50),
                        visible: visible(el),
                    });
                }
            }
            return hits.slice(0, 25);
        }"""
    )

    url_before = page.url
    sched_open = await _click_by_text(
        page, ["aria:scheduled", "aria:schedule post", "aria:schedule"], scoped=True
    )
    await asyncio.sleep(1.8)
    schedule_dialog = await page.evaluate(
        """() => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight
                                       || el.getClientRects().length);
            const describe = (el) => ({
                tag: el.tagName.toLowerCase(), role: el.getAttribute('role'),
                aria: el.getAttribute('aria-label'), id: el.id || null,
                type: el.getAttribute('type'), name: el.getAttribute('name'),
                value: el.value ?? null,
                placeholder: el.getAttribute('placeholder'),
                text: (el.innerText || '').trim().slice(0, 40),
            });
            return {
                inputs: Array.from(document.querySelectorAll('input, select'))
                    .filter(visible).map(describe),
                buttons: Array.from(document.querySelectorAll(
                    'button, a, [role="button"]')).filter(visible)
                    .map(describe).slice(0, 30),
            };
        }"""
    )
    url_after = page.url

    typed_back = await _editor_text(page)

    await _dismiss_composer(page)
    return {
        "schedule_hunt": schedule_hunt,
        "schedule_open_matched": sched_open,
        "schedule_dialog": schedule_dialog,
        "url_before": url_before,
        "url_after": url_after,
        "editor_content": typed_back,
        "trigger_matched": trigger,
        "editor_focused": focused,
        "expand_matched": expanded,
        "composer_shape": shape,
        "scans": [before, after, after_expand],
    }


async def _dismiss_composer(page: Any) -> None:
    """Close the composer, discarding any draft, without posting."""
    try:
        await _click_by_text(page, ["aria:dismiss", "aria:close"], scoped=True)
        await asyncio.sleep(0.6)
        # LinkedIn asks whether to save a draft when there is text.
        await _click_by_text(page, ["text:discard", "aria:discard"], scoped=True)
        await asyncio.sleep(0.4)
    except Exception:
        logger.debug("Composer dismiss failed; continuing", exc_info=True)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_images(image_paths: list[str] | None) -> list[Path]:
    """Resolve and check image paths before the browser is touched."""
    if not image_paths:
        return []
    if len(image_paths) > MAX_IMAGES:
        raise ComposerError(
            f"{len(image_paths)} images requested; LinkedIn accepts at most {MAX_IMAGES}."
        )
    resolved: list[Path] = []
    for raw in image_paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ComposerError(f"Image not found: {path}")
        if path.suffix.lower() not in _ALLOWED_IMAGE_SUFFIXES:
            raise ComposerError(
                f"Unsupported image type {path.suffix!r} for {path.name}; "
                f"expected one of {sorted(_ALLOWED_IMAGE_SUFFIXES)}."
            )
        resolved.append(path.resolve())
    return resolved


def parse_schedule_at(schedule_at: str | None) -> dt.datetime | None:
    """Parse an ISO-8601 local datetime and refuse anything LinkedIn will not take.

    LinkedIn requires a scheduled time at least ~10 minutes out and no more
    than 3 months ahead, on a 5-minute boundary in the account's timezone.
    """
    if not schedule_at:
        return None
    try:
        when = dt.datetime.fromisoformat(schedule_at)
    except ValueError as exc:
        raise ComposerError(
            f"schedule_at must be ISO-8601 local time such as "
            f"'2026-09-02T08:30' -- got {schedule_at!r}."
        ) from exc
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)

    now = dt.datetime.now()
    if when <= now + dt.timedelta(minutes=10):
        raise ComposerError(
            f"schedule_at must be at least 10 minutes ahead; "
            f"{when.isoformat()} is not (now is {now.isoformat(timespec='minutes')})."
        )
    if when > now + dt.timedelta(days=90):
        raise ComposerError(
            "LinkedIn does not accept a scheduled time more than ~3 months ahead."
        )
    if when.minute % 5 != 0:
        raise ComposerError(
            f"LinkedIn's scheduler only offers 5-minute increments; "
            f"{when.strftime('%H:%M')} is not on one."
        )
    return when


# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------


async def _attach_images(page: Any, images: list[Path]) -> None:
    """Attach images by feeding LinkedIn's hidden file input directly.

    Clicking "Add media" and driving the OS file picker is not automatable;
    setting files on the input the picker would have populated is, and it is
    what LinkedIn's own JavaScript reads.
    """
    await _click_by_text(
        page,
        [
            "aria:media",
            "aria:add media",
            "aria:add a photo",
        ],
        scoped=True,
    )
    await asyncio.sleep(1.0)

    file_input = page.locator('input[type="file"]').first
    try:
        await file_input.set_input_files([str(p) for p in images], timeout=15000)
    except Exception as exc:  # noqa: BLE001 - reported to the caller as status
        raise ComposerError(
            f"Could not hand {len(images)} image(s) to LinkedIn's file input: {exc}"
        ) from exc

    # Upload + preview render. LinkedIn shows a media editor with its own
    # confirm button before returning to the composer.
    await asyncio.sleep(3.0)
    for _ in range(3):
        matched = await _click_by_text(
            page,
            ["text:next", "aria:next", "text:done", "aria:done"],
            scoped=True,
        )
        if not matched:
            break
        await asyncio.sleep(1.2)


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


async def _set_input_matching(page: Any, value_pattern: str, value: str) -> bool:
    """Set the visible input whose current value matches ``value_pattern``.

    The schedule dialog's inputs carry React-generated ids ("\u00abr46\u00bb"), no
    name and no aria-label, so they cannot be addressed by attribute. What
    does identify them is what LinkedIn has already put in them: a date looks
    like a date and a time looks like a time. Matching on that is stable
    across renames and does not depend on field order.

    Assigning ``.value`` alone is invisible to React -- it tracks the previous
    value on the node and treats the event as a no-op -- so the write goes
    through the prototype's native setter first.
    """
    return await page.evaluate(
        """
        ([pattern, value]) => {
            const re = new RegExp(pattern);
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight
                                       || el.getClientRects().length);
            const el = Array.from(document.querySelectorAll('input'))
                .filter(visible)
                .find((i) => re.test(i.value || ''));
            if (!el) return false;
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.blur();
            return true;
        }
        """,
        [value_pattern, value],
    )


# What LinkedIn itself renders into the two fields, and therefore the shapes
# it parses back: "8/29/2026" and "8:45 PM" -- no zero padding on either.
_DATE_PATTERN = r"^\d{1,2}/\d{1,2}/\d{4}$"
_TIME_PATTERN = r"^\d{1,2}:\d{2}\s*(AM|PM)$"


async def _apply_schedule(page: Any, when: dt.datetime) -> dict[str, Any]:
    """Drive LinkedIn's native schedule dialog to the requested time."""
    opened = await _click_by_text(
        page, ["aria:scheduled", "aria:schedule post", "aria:schedule"], scoped=True
    )
    if not opened:
        raise ComposerError(
            "Could not find the scheduling control in the composer. "
            "It is an anchor labelled 'Scheduled'; run inspect_composer to "
            "check whether LinkedIn has moved it."
        )
    await asyncio.sleep(1.8)

    date_str = f"{when.month}/{when.day}/{when.year}"
    time_str = when.strftime("%I:%M %p").lstrip("0")

    date_ok = await _set_input_matching(page, _DATE_PATTERN, date_str)
    await asyncio.sleep(0.4)
    time_ok = await _set_input_matching(page, _TIME_PATTERN, time_str)
    await asyncio.sleep(0.4)

    if not (date_ok and time_ok):
        raise ComposerError(
            f"Schedule dialog did not take the values (date_set={date_ok}, "
            f"time_set={time_ok}); wanted {date_str} {time_str}."
        )

    confirmed = await _click_by_text(
        page, ["exact:confirm", "aria:confirm"], scoped=False
    )
    if not confirmed:
        raise ComposerError("Schedule dialog had no Confirm control.")
    await asyncio.sleep(1.5)
    return {"date": date_str, "time": time_str}


# --------------------------------------------------------------------------
# The write
# --------------------------------------------------------------------------


# The attribute used to identify the composer's own editor across a submit.
# React re-renders freely, so the element has to be tagged rather than
# re-found: "is *this* element still here" is a different question from "is
# an editor still here", and only the first one answers whether the composer
# we typed into closed.
_EDITOR_MARK = "data-approva-editor"

# LinkedIn's permalinks. Matched by URL shape rather than by the link's text,
# which is locale-dependent; an href pattern is not.
_PERMALINK_SELECTOR = 'a[href*="/feed/update/urn:li:activity:"]'


async def _mark_editor(page: Any) -> bool:
    """Tag the editor we are about to type into, so it can be found again."""
    return await page.evaluate(
        "(attr) => {"
        + VISIBLE_JS
        + "    const editor = "
        + COMPOSER_EDITOR_JS
        + """;
            if (!editor) return false;
            editor.setAttribute(attr, '1');
            return true;
        }""",
        _EDITOR_MARK,
    )


async def _editor_still_open(page: Any) -> bool:
    """Report whether the editor we typed into is still on the page.

    Asked of the tagged element, never of ``div[role="dialog"]``: the composer
    is not a dialog (``50872b7``), so that selector never matched and the
    old check reported "closed" whether or not the post went through.
    """
    return await page.evaluate(
        "(attr) => {"
        + VISIBLE_JS
        + """
            const el = document.querySelector('[' + attr + ']');
            return !!(el && visible(el));
        }""",
        _EDITOR_MARK,
    )


async def _permalinks(page: Any) -> list[str]:
    """Every post permalink currently linked from the page."""
    result = await page.evaluate(
        "(sel) => Array.from(new Set(Array.from(document.querySelectorAll(sel))"
        ".map((a) => a.getAttribute('href')).filter(Boolean)))",
        _PERMALINK_SELECTOR,
    )
    return result if isinstance(result, list) else []


async def _await_permalink(
    page: Any, before: list[str], *, timeout: float = 12.0
) -> str | None:
    """Wait for the permalink LinkedIn offers after a successful post.

    LinkedIn confirms a post with a link to it. Diffing against the links
    present before submitting is what makes the new one identifiable, and
    matching the href shape rather than the link's wording keeps it working in
    any locale. Returns ``None`` rather than guessing when more than one new
    link appears -- the feed loads posts on its own, and a wrong permalink is
    worse than none.
    """
    seen = set(before)
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        fresh = [h for h in await _permalinks(page) if h not in seen]
        if len(fresh) == 1:
            href = fresh[0]
            return (
                href if href.startswith("http") else f"https://www.linkedin.com{href}"
            )
        if len(fresh) > 1:
            return None
        await asyncio.sleep(0.5)
    return None


async def _newest_own_post(page: Any, goto: Any) -> str | None:
    """Fall back to the author's activity page for the post just published.

    Measured 2026-08-30: unlike the permalink page, the recent-activity page
    does carry ``data-urn`` set to exactly an activity URN on each post, and
    the newest post is first. Costs one navigation, so it runs only when the
    confirmation link did not resolve.
    """
    try:
        await goto("https://www.linkedin.com/in/me/recent-activity/all/")
        await asyncio.sleep(3.0)
        urn = await page.evaluate(
            "() => {"
            + VISIBLE_JS
            + r"""
            const el = Array.from(document.querySelectorAll('[data-urn], [data-id]'))
                .filter(visible)
                .find((e) => /^urn:li:(activity|ugcPost|share):\d+$/.test(
                    e.getAttribute('data-urn') || e.getAttribute('data-id') || ''));
            return el ? (el.getAttribute('data-urn') || el.getAttribute('data-id')) : null;
            }"""
        )
    except Exception:
        logger.debug("Activity-page fallback failed", exc_info=True)
        return None
    if not isinstance(urn, str) or not urn:
        return None
    return f"https://www.linkedin.com/feed/update/{urn}/"


async def create_post(
    page: Any,
    goto: Any,
    *,
    text: str,
    image_paths: list[str] | None = None,
    schedule_at: str | None = None,
) -> dict[str, Any]:
    """Publish or schedule a post on the founder's own feed.

    Every argument is validated before the browser is touched, so a bad
    request costs no page load and cannot leave a half-written draft behind.
    """
    body = (text or "").strip()
    if not body:
        raise ComposerError("Refusing to post empty text.")
    if len(body) > MAX_POST_CHARS:
        raise ComposerError(
            f"Post is {len(body)} characters; LinkedIn's limit is {MAX_POST_CHARS}."
        )
    images = validate_images(image_paths)
    when = parse_schedule_at(schedule_at)

    await goto(FEED_URL)
    await asyncio.sleep(1.5)

    trigger = await _click_by_text(
        page,
        [
            "aria:start a post",
            "aria:create a post",
            "text:start a post",
        ],
        scoped=False,
    )
    if not trigger:
        raise ComposerError("Could not find the 'Start a post' control on the feed.")
    if not await _wait_for_composer(page):
        raise ComposerError("Composer did not open after clicking 'Start a post'.")

    if not await _focus_editor(page):
        await _dismiss_composer(page)
        raise ComposerError("Could not focus the composer text editor.")

    await _mark_editor(page)
    await asyncio.sleep(0.2)
    # Typed rather than pasted: LinkedIn's editor builds its internal model
    # from key events, and a human-plausible cadence costs us nothing here.
    await page.keyboard.type(body, delay=12)
    await asyncio.sleep(0.8)

    landed = await _editor_text(page)
    if body.split("\n", 1)[0][:40] not in landed:
        await _dismiss_composer(page)
        raise ComposerError(
            "Composer did not receive the text (editor content did not match)."
        )

    if images:
        await _attach_images(page, images)

    schedule_info: dict[str, Any] | None = None
    if when is not None:
        schedule_info = await _apply_schedule(page, when)

    links_before = await _permalinks(page)
    submit = await _click_by_text(
        page,
        (
            # After Confirm the primary action relabels; accept either.
            ["exact:schedule", "exact:post"] if when is not None else ["exact:post"]
        ),
        scoped=True,
    )
    if not submit:
        await _dismiss_composer(page)
        raise ComposerError(
            "Could not find the "
            + ("Schedule" if when is not None else "Post")
            + " button; nothing was submitted."
        )

    await asyncio.sleep(3.0)
    still_open = await _editor_still_open(page)

    # A scheduled post has no permalink to hand back -- LinkedIn is holding it.
    post_url: str | None = None
    if when is None:
        post_url = await _await_permalink(page, links_before)
        if post_url is None:
            post_url = await _newest_own_post(page, goto)

    return {
        "status": "scheduled" if when is not None else "posted",
        "submitted": True,
        "composer_closed": not still_open,
        "post_url": post_url,
        "characters": len(body),
        "images_attached": len(images),
        "scheduled_for": when.isoformat(timespec="minutes") if when else None,
        "schedule_fields": schedule_info,
        "url": page.url,
    }
