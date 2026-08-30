"""Browser-DOM tests for the comment box's element selection and submit
detection.

The unit suite mocks ``page.evaluate``, so none of the JS in
``approva/comment.py`` executes there and these tests are its only coverage.

Every fixture below is a *synthetic* container, not an imitation of
LinkedIn's markup: it carries only the structural facts the algorithm reads --
a ``data-urn`` that is exactly an activity URN, a visible contenteditable, a
``disabled`` attribute that flips. So each test is a claim about the
algorithm, not a claim about LinkedIn. What the live page actually renders is
pinned by a hand-run, per the spec's verification section.

Labels are German throughout, for the same reason the action-signal tests use
them: none of this may classify by reading text.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.approva.comment import (
    _await_confirmation,
    _click_submit,
    _confirmation_state,
    _editor_js,
    _editor_stats,
    _scan_buttons,
)
from linkedin_mcp_server.approva.composer import (
    focus_contenteditable,
    read_contenteditable,
)

pytestmark = pytest.mark.browser_dom

POST_URN = "urn:li:activity:7123456789"
OTHER_URN = "urn:li:activity:7999999999"


def _post(
    urn: str = POST_URN,
    *,
    editor: bool = True,
    comments: str = "",
    extra: str = "",
) -> str:
    """A post container: an activity URN, an optional comment box, comments."""
    box = (
        '<div role="textbox" contenteditable="true" '
        'style="width:200px;height:40px"></div>'
        if editor
        else ""
    )
    return f"""
    <div data-urn="{urn}" style="width:400px;height:200px">
      <p>Ein Beitrag</p>
      {comments}
      <div class="box">
        {box}
        <button type="button" aria-label="Emoji">SM</button>
        <button type="button" aria-label="Kommentieren" disabled>Kommentieren</button>
      </div>
      {extra}
    </div>
    """


def _comment(cid: str, text: str) -> str:
    return f'<article data-id="{cid}"><span>{text}</span></article>'


def _page_html(*parts: str) -> str:
    return f"<html><body><main>{''.join(parts)}</main></body></html>"


@pytest.fixture
async def dom_page():
    """Real chromium page, or skip when no browser is installed.

    Only launch/setup is guarded by the skip -- the ``yield`` sits outside it
    so a JS error in a test body is never swallowed into a skip.
    """
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


class TestEditorSelection:
    async def test_the_only_editable_on_the_page_is_the_comment_box(self, dom_page):
        await dom_page.set_content(_page_html(_post()))
        stats = await _editor_stats(dom_page, POST_URN)
        assert stats["found"] is True
        assert stats["editable_count"] == 1

    async def test_selection_survives_an_unknown_urn(self, dom_page):
        # A /posts/ slug carrying no activity id passes urn=None. The single
        # editable still resolves; only the scoping is unavailable.
        await dom_page.set_content(_page_html(_post()))
        stats = await _editor_stats(dom_page, None)
        assert stats["found"] is True

    async def test_a_reply_box_is_resolved_by_scoping_to_the_post(self, dom_page):
        reply = (
            '<div role="textbox" contenteditable="true" '
            'style="width:100px;height:20px"></div>'
        )
        await dom_page.set_content(_page_html(_post(extra=reply)))
        stats = await _editor_stats(dom_page, POST_URN)
        assert stats["editable_count"] == 2
        assert stats["scoped_editable_count"] == 2
        assert stats["found"] is True

    async def test_two_posts_select_the_addressed_one(self, dom_page):
        await dom_page.set_content(_page_html(_post(OTHER_URN), _post(POST_URN)))
        chosen = await dom_page.evaluate(
            "() => { const el = " + _editor_js(POST_URN) + ";"
            " return el ? el.closest('[data-urn]').getAttribute('data-urn') : null; }"
        )
        assert chosen == POST_URN

    async def test_the_scan_does_not_reach_into_a_neighbouring_post(self, dom_page):
        # Scoping to the addressed post is what keeps the submit-button and
        # comment scans honest. Scoped to the page instead, a neighbouring
        # post's controls join the candidate set and its comments count as
        # confirmation of ours.
        other = _post(
            OTHER_URN,
            extra='<button type="button" aria-label="Fremd">F</button>',
        )
        await dom_page.set_content(_page_html(other, _post(POST_URN)))

        buttons = await _scan_buttons(dom_page, POST_URN, mark=False)
        assert [b["aria"] for b in buttons] == ["Emoji", "Kommentieren"]

    async def test_ambiguity_outside_any_post_is_refused(self, dom_page):
        # Two editables and no resolvable post container: the algorithm must
        # decline rather than type into whichever came first.
        stray = '<div contenteditable="true" style="width:100px;height:20px"></div>'
        await dom_page.set_content(_page_html(_post(editor=True), stray))
        stats = await _editor_stats(dom_page, "urn:li:activity:404")
        assert stats["post_root_found"] is False
        assert stats["found"] is False

    async def test_a_comment_is_never_mistaken_for_the_post_container(self, dom_page):
        # A comment's own id embeds the post's: "urn:li:comment:(urn:li:
        # activity:7123,111)". Matched loosely, a comment element satisfies
        # the post-container test -- and when one precedes the real container
        # in document order it is what the scan scopes to, which silently
        # empties both the button scan and the comment scan.
        #
        # Driven with urn=None, the case a /posts/ slug carrying no activity
        # id produces: there is no exact URN left to fall back on.
        stray = _comment(f"urn:li:comment:({POST_URN},111)", "Woanders")
        await dom_page.set_content(_page_html(stray, _post()))

        buttons = await _scan_buttons(dom_page, None, mark=False)
        assert [b["aria"] for b in buttons] == ["Emoji", "Kommentieren"]

    async def test_a_hidden_editor_is_not_selected(self, dom_page):
        hidden = _post().replace(
            'style="width:200px;height:40px"', 'style="display:none"'
        )
        await dom_page.set_content(_page_html(hidden))
        stats = await _editor_stats(dom_page, POST_URN)
        assert stats["found"] is False


class TestSubmitDetection:
    async def _type(self, page, text: str) -> None:
        assert await focus_contenteditable(page, _editor_js(POST_URN))
        await page.keyboard.type(text)

    async def test_the_button_typing_enables_is_the_one_clicked(self, dom_page):
        await dom_page.set_content(_page_html(_post()))
        await dom_page.evaluate(
            """() => {
                const box = document.querySelector('[role="textbox"]');
                const btn = document.querySelectorAll('button')[1];
                box.addEventListener('input', () => { btn.disabled = false; });
                btn.addEventListener('click', () =>
                    document.body.setAttribute('data-clicked', 'submit'));
            }"""
        )
        before = await _scan_buttons(dom_page, POST_URN, mark=True)
        await self._type(dom_page, "Guter Beitrag")
        result = await _click_submit(dom_page, POST_URN, before)

        assert result["clicked"]["aria"] == "Kommentieren"
        assert await dom_page.get_attribute("body", "data-clicked") == "submit"

    async def test_a_button_that_mounts_on_input_is_also_found(self, dom_page):
        # LinkedIn's other shape: the submit control does not exist until the
        # box has content, so there is no disabled->enabled transition to see.
        await dom_page.set_content(_page_html(_post()))
        await dom_page.evaluate(
            """() => {
                const box = document.querySelector('[role="textbox"]');
                box.addEventListener('input', () => {
                    if (document.querySelector('#late')) return;
                    const btn = document.createElement('button');
                    btn.id = 'late';
                    btn.setAttribute('aria-label', 'Senden');
                    btn.onclick = () =>
                        document.body.setAttribute('data-clicked', 'late');
                    box.parentElement.appendChild(btn);
                });
            }"""
        )
        before = await _scan_buttons(dom_page, POST_URN, mark=True)
        await self._type(dom_page, "Guter Beitrag")
        result = await _click_submit(dom_page, POST_URN, before)

        assert result["clicked"]["aria"] == "Senden"
        assert await dom_page.get_attribute("body", "data-clicked") == "late"

    async def test_a_button_already_enabled_is_never_clicked(self, dom_page):
        # The emoji button is enabled the whole time. Nothing transitions, so
        # there is nothing to submit -- and clicking the wrong control here is
        # exactly what the transition rule exists to prevent.
        await dom_page.set_content(_page_html(_post()))
        before = await _scan_buttons(dom_page, POST_URN, mark=True)
        await self._type(dom_page, "Guter Beitrag")
        result = await _click_submit(dom_page, POST_URN, before)

        assert result["clicked"] is None
        assert [b["aria"] for b in result["buttons"]] == ["Emoji", "Kommentieren"]

    async def test_the_last_transitioning_control_wins(self, dom_page):
        # The submit control sits after the affordances that share its row.
        await dom_page.set_content(_page_html(_post()))
        await dom_page.evaluate(
            """() => {
                const box = document.querySelector('[role="textbox"]');
                const btns = document.querySelectorAll('button');
                btns[0].disabled = true;
                box.addEventListener('input', () => {
                    btns[0].disabled = false;
                    btns[1].disabled = false;
                });
            }"""
        )
        before = await _scan_buttons(dom_page, POST_URN, mark=True)
        await self._type(dom_page, "Guter Beitrag")
        result = await _click_submit(dom_page, POST_URN, before)

        assert len(result["candidates"]) == 2
        assert result["clicked"]["aria"] == "Kommentieren"

    async def test_a_control_that_mounts_disabled_is_not_clicked(self, dom_page):
        # "Not seen before" is not on its own a reason to click: LinkedIn
        # mounts controls in a disabled state too, and clicking one of those
        # instead of the submit button loses the comment silently.
        await dom_page.set_content(_page_html(_post()))
        await dom_page.evaluate(
            """() => {
                const box = document.querySelector('[role="textbox"]');
                const btns = document.querySelectorAll('button');
                box.addEventListener('input', () => {
                    if (document.querySelector('#late')) return;
                    btns[1].disabled = false;
                    const late = document.createElement('button');
                    late.id = 'late';
                    late.setAttribute('aria-label', 'Spaeter');
                    late.setAttribute('aria-disabled', 'true');
                    box.parentElement.appendChild(late);
                });
            }"""
        )
        before = await _scan_buttons(dom_page, POST_URN, mark=True)
        await self._type(dom_page, "Guter Beitrag")
        result = await _click_submit(dom_page, POST_URN, before)

        assert [c["aria"] for c in result["candidates"]] == ["Kommentieren"]
        assert result["clicked"]["aria"] == "Kommentieren"

    async def test_aria_disabled_counts_as_disabled(self, dom_page):
        await dom_page.set_content(_page_html(_post()))
        await dom_page.evaluate(
            """() => {
                const box = document.querySelector('[role="textbox"]');
                const btn = document.querySelectorAll('button')[1];
                btn.removeAttribute('disabled');
                btn.setAttribute('aria-disabled', 'true');
                box.addEventListener('input', () =>
                    btn.setAttribute('aria-disabled', 'false'));
            }"""
        )
        before = await _scan_buttons(dom_page, POST_URN, mark=True)
        await self._type(dom_page, "Guter Beitrag")
        result = await _click_submit(dom_page, POST_URN, before)

        assert result["clicked"]["aria"] == "Kommentieren"


class TestTypingPath:
    async def test_key_events_land_in_the_contenteditable(self, dom_page):
        await dom_page.set_content(_page_html(_post()))
        assert await focus_contenteditable(dom_page, _editor_js(POST_URN))
        await dom_page.keyboard.type("Guter Beitrag")
        assert "Guter Beitrag" in await read_contenteditable(
            dom_page, _editor_js(POST_URN)
        )


class TestConfirmation:
    """The confirmation contract, as re-pinned against the live page.

    LinkedIn's permalink rendering exposes no ``data-id`` carrying a comment
    URN, so confirmation reads the two signals that survive that: our text in
    the rendered body with the editor subtree removed, and comment URNs
    scanned out of the page HTML. Both are diffs against the pre-typing state.
    """

    async def test_a_new_comment_with_our_text_confirms(self, dom_page):
        await dom_page.set_content(_page_html(_post()))
        before = await _confirmation_state(dom_page, "guter beitrag")
        await dom_page.evaluate(
            """([html]) => {
                document.querySelector('[data-urn]')
                    .insertAdjacentHTML('beforeend', html);
            }""",
            [_comment(f"urn:li:comment:({POST_URN},222)", "Guter Beitrag")],
        )
        landed = await _await_confirmation(
            dom_page, "guter beitrag", before, timeout=3.0
        )
        assert landed is not None
        assert landed["signal"] == "text"
        assert landed["comment_urn"] == f"urn:li:comment:({POST_URN},222)"

    async def test_a_draft_still_in_the_editor_does_not_confirm(self, dom_page):
        # The failure this guards is the one that would make the whole check
        # a tautology: a submit that never went through leaves our text in the
        # box, and counting it would confirm a comment that does not exist.
        await dom_page.set_content(_page_html(_post()))
        before = await _confirmation_state(dom_page, "guter beitrag")
        assert await focus_contenteditable(dom_page, _editor_js(POST_URN))
        await dom_page.keyboard.type("Guter Beitrag")

        state = await _confirmation_state(dom_page, "guter beitrag")
        assert state["occurrences"] == 0
        landed = await _await_confirmation(
            dom_page, "guter beitrag", before, timeout=2.0
        )
        assert landed is None

    async def test_a_preexisting_identical_comment_does_not_confirm(self, dom_page):
        # The double-post guard: our text is already on the post, so a submit
        # that rendered nothing must not read as a success.
        existing = _comment(f"urn:li:comment:({POST_URN},111)", "Guter Beitrag")
        await dom_page.set_content(_page_html(_post(comments=existing)))
        before = await _confirmation_state(dom_page, "guter beitrag")
        assert before["occurrences"] == 1

        landed = await _await_confirmation(
            dom_page, "guter beitrag", before, timeout=2.0
        )
        assert landed is None

    async def test_a_new_comment_from_someone_else_does_not_confirm(self, dom_page):
        await dom_page.set_content(_page_html(_post()))
        before = await _confirmation_state(dom_page, "guter beitrag")
        await dom_page.evaluate(
            """([html]) => {
                document.querySelector('[data-urn]')
                    .insertAdjacentHTML('beforeend', html);
            }""",
            ["<article><span>Ein anderer Kommentar</span></article>"],
        )
        landed = await _await_confirmation(
            dom_page, "guter beitrag", before, timeout=2.0
        )
        assert landed is None

    async def test_a_new_urn_confirms_when_the_text_is_truncated(self, dom_page):
        # LinkedIn folds a long comment behind "see more", so the tail can be
        # absent from the rendered text. A single new comment URN carries the
        # confirmation on its own.
        await dom_page.set_content(_page_html(_post()))
        before = await _confirmation_state(dom_page, "guter beitrag")
        await dom_page.evaluate(
            """([html]) => {
                document.querySelector('[data-urn]')
                    .insertAdjacentHTML('beforeend', html);
            }""",
            [_comment(f"urn:li:comment:({POST_URN},333)", "Guter Beitr…")],
        )
        landed = await _await_confirmation(
            dom_page, "guter beitrag", before, timeout=3.0
        )
        assert landed is not None
        assert landed["signal"] == "urn"
        assert landed["comment_urn"] == f"urn:li:comment:({POST_URN},333)"

    async def test_two_new_urns_confirm_but_attribute_no_permalink(self, dom_page):
        # Two comments landing at once leaves us unable to say which is ours,
        # so the comment survives but the permalink is withheld rather than
        # guessed.
        await dom_page.set_content(_page_html(_post()))
        before = await _confirmation_state(dom_page, "guter beitrag")
        await dom_page.evaluate(
            """([a, b]) => {
                const root = document.querySelector('[data-urn]');
                root.insertAdjacentHTML('beforeend', a);
                root.insertAdjacentHTML('beforeend', b);
            }""",
            [
                _comment(f"urn:li:comment:({POST_URN},444)", "Guter Beitrag"),
                _comment(f"urn:li:comment:({POST_URN},555)", "Etwas anderes"),
            ],
        )
        landed = await _await_confirmation(
            dom_page, "guter beitrag", before, timeout=3.0
        )
        assert landed is not None
        assert landed["signal"] == "text"
        assert landed["comment_urn"] is None

    async def test_two_new_urns_without_text_evidence_do_not_confirm(self, dom_page):
        # Truncation removed the text signal and two comments landed at once,
        # so nothing on the page says ours is among them. Confirming here
        # would be a guess, and the caller's next move after a confirmation is
        # to stop worrying about a double post.
        await dom_page.set_content(_page_html(_post()))
        before = await _confirmation_state(dom_page, "guter beitrag")
        await dom_page.evaluate(
            """([a, b]) => {
                const root = document.querySelector('[data-urn]');
                root.insertAdjacentHTML('beforeend', a);
                root.insertAdjacentHTML('beforeend', b);
            }""",
            [
                _comment(f"urn:li:comment:({POST_URN},666)", "Etwas"),
                _comment(f"urn:li:comment:({POST_URN},777)", "Anderes"),
            ],
        )
        landed = await _await_confirmation(
            dom_page, "guter beitrag", before, timeout=2.0
        )
        assert landed is None
