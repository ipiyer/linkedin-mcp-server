"""Browser-DOM tests for the composer's post-submit checks.

The unit suite mocks ``page.evaluate``, so this JS never runs there. Fixtures
are synthetic containers carrying only the structural facts the code reads --
a tagged editor, anchors whose href matches a permalink shape, a ``data-urn``
that is exactly an activity URN -- so each test is a claim about the algorithm
rather than about LinkedIn's markup.

Both behaviours here were added after the 2026-08-30 hand-run: ``create_post``
reported ``composer_closed`` from a ``div[role="dialog"]`` that never matched
(the composer is not a dialog, per ``50872b7``), and returned no permalink at
all, so the caller had no way to reach the post it had just published.

Skipped automatically when chromium is not installed.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.approva.composer import (
    _await_permalink,
    _editor_still_open,
    _mark_editor,
    _newest_own_post,
    _permalinks,
)

pytestmark = pytest.mark.browser_dom

EDITOR = (
    '<div role="textbox" contenteditable="true" style="width:200px;height:40px"></div>'
)
ACTIVITY = "urn:li:activity:7499895627078746112"


def _page_html(*parts: str) -> str:
    return f"<html><body><main>{''.join(parts)}</main></body></html>"


def _link(href: str, text: str = "x") -> str:
    return f'<a href="{href}">{text}</a>'


@pytest.fixture
async def dom_page():
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


class TestComposerClosed:
    async def test_a_removed_editor_reads_as_closed(self, dom_page):
        await dom_page.set_content(_page_html(EDITOR))
        assert await _mark_editor(dom_page) is True
        assert await _editor_still_open(dom_page) is True

        await dom_page.evaluate(
            "() => document.querySelector('[role=\"textbox\"]').remove()"
        )
        assert await _editor_still_open(dom_page) is False

    async def test_a_hidden_editor_reads_as_closed(self, dom_page):
        # The composer can be detached from view without being removed.
        await dom_page.set_content(_page_html(EDITOR))
        await _mark_editor(dom_page)
        await dom_page.evaluate(
            "() => document.querySelector('[role=\"textbox\"]')"
            ".setAttribute('style', 'display:none')"
        )
        assert await _editor_still_open(dom_page) is False

    async def test_a_different_editor_does_not_keep_it_open(self, dom_page):
        # The property the old dialog check could not express: it is *this*
        # editor that must be gone, not merely "an editor". A feed page mounts
        # comment boxes of its own, and one of those must not read as a
        # composer that never closed.
        await dom_page.set_content(_page_html(EDITOR))
        await _mark_editor(dom_page)
        await dom_page.evaluate(
            """() => {
                document.querySelector('[role="textbox"]').remove();
                const other = document.createElement('div');
                other.setAttribute('role', 'textbox');
                other.setAttribute('contenteditable', 'true');
                other.setAttribute('style', 'width:200px;height:40px');
                document.querySelector('main').appendChild(other);
            }"""
        )
        assert await _editor_still_open(dom_page) is False

    async def test_an_unmarked_page_reads_as_closed(self, dom_page):
        await dom_page.set_content(_page_html(EDITOR))
        assert await _editor_still_open(dom_page) is False


class TestPermalink:
    async def test_only_permalink_shaped_hrefs_are_collected(self, dom_page):
        await dom_page.set_content(
            _page_html(
                _link("/in/someone/"),
                _link(f"/feed/update/{ACTIVITY}/"),
                _link("/jobs/view/123/"),
            )
        )
        assert await _permalinks(dom_page) == [f"/feed/update/{ACTIVITY}/"]

    async def test_the_single_new_link_is_returned_absolute(self, dom_page):
        await dom_page.set_content(_page_html(_link("/in/someone/")))
        before = await _permalinks(dom_page)
        await dom_page.evaluate(
            """([href]) => {
                const a = document.createElement('a');
                a.setAttribute('href', href);
                a.textContent = 'View post';
                document.querySelector('main').appendChild(a);
            }""",
            [f"/feed/update/{ACTIVITY}/"],
        )
        got = await _await_permalink(dom_page, before, timeout=3.0)
        assert got == f"https://www.linkedin.com/feed/update/{ACTIVITY}/"

    async def test_a_link_already_present_is_not_returned(self, dom_page):
        # The feed is full of permalinks to other people's posts. Only one
        # that appeared *after* the submit can be the post we just made.
        await dom_page.set_content(_page_html(_link(f"/feed/update/{ACTIVITY}/")))
        before = await _permalinks(dom_page)
        assert await _await_permalink(dom_page, before, timeout=2.0) is None

    async def test_two_new_links_return_nothing_rather_than_a_guess(self, dom_page):
        # The feed loads posts on its own while we wait. A wrong permalink is
        # worse than none: the caller would comment on a stranger's post.
        await dom_page.set_content(_page_html("<p>empty</p>"))
        before = await _permalinks(dom_page)
        await dom_page.evaluate(
            """() => {
                for (const id of ['111', '222']) {
                    const a = document.createElement('a');
                    a.setAttribute('href', '/feed/update/urn:li:activity:' + id + '/');
                    document.querySelector('main').appendChild(a);
                }
            }"""
        )
        assert await _await_permalink(dom_page, before, timeout=3.0) is None

    async def test_an_absolute_href_is_passed_through(self, dom_page):
        await dom_page.set_content(_page_html("<p>empty</p>"))
        before = await _permalinks(dom_page)
        full = f"https://www.linkedin.com/feed/update/{ACTIVITY}/"
        await dom_page.evaluate(
            """([href]) => {
                const a = document.createElement('a');
                a.setAttribute('href', href);
                document.querySelector('main').appendChild(a);
            }""",
            [full],
        )
        assert await _await_permalink(dom_page, before, timeout=3.0) == full


class TestActivityPageFallback:
    async def _goto(self, _url):
        """Stand in for navigation: the fixture is already on the page."""
        return None

    async def test_the_newest_post_urn_becomes_a_permalink(self, dom_page):
        await dom_page.set_content(
            _page_html(
                f'<div data-urn="{ACTIVITY}" style="width:300px;height:80px">neu</div>',
                '<div data-urn="urn:li:activity:1" style="width:300px;height:80px">alt</div>',
            )
        )
        got = await _newest_own_post(dom_page, self._goto)
        assert got == f"https://www.linkedin.com/feed/update/{ACTIVITY}/"

    async def test_a_comment_urn_is_not_mistaken_for_a_post(self, dom_page):
        # A comment's id embeds an activity URN; matched loosely it would be
        # returned as the permalink of the post we just published.
        await dom_page.set_content(
            _page_html(
                f'<div data-id="urn:li:comment:({ACTIVITY},9)" '
                'style="width:300px;height:80px">kommentar</div>',
                f'<div data-urn="{ACTIVITY}" style="width:300px;height:80px">post</div>',
            )
        )
        got = await _newest_own_post(dom_page, self._goto)
        assert got == f"https://www.linkedin.com/feed/update/{ACTIVITY}/"

    async def test_no_post_yields_nothing(self, dom_page):
        await dom_page.set_content(_page_html("<p>leer</p>"))
        assert await _newest_own_post(dom_page, self._goto) is None
