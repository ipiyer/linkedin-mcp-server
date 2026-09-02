"""Browser-DOM tests for the post-card ``data-urn`` scan in
``LinkedInExtractor._extract_root_content``.

The unit suite mocks ``page.evaluate``, so the scan's JS never executes there
and these tests are its only coverage. Every fixture is a *synthetic*
container carrying only the structural facts the scan reads -- a ``data-urn``
or ``data-id`` that is exactly a post URN, a commentary node, a comment whose
id embeds its post's -- so each test is a claim about the algorithm, not about
LinkedIn's markup. What the live pages render is pinned by hand (activity page
2026-08-30, content search 2026-09-01).

Labels are German so nothing here can pass by reading text.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

pytestmark = pytest.mark.browser_dom

FIRST = "urn:li:activity:7111111111"
SECOND = "urn:li:ugcPost:7222222222"
THIRD = "urn:li:share:7333333333"


def _post(urn: str, *, text: str | None = "Ein Beitrag", attr: str = "data-urn") -> str:
    commentary = (
        f'<div class="update-components-text"><span>{text}</span></div>'
        if text is not None
        else ""
    )
    return f'<div {attr}="{urn}"><p>Kopfzeile</p>{commentary}</div>'


def _comment(post_urn: str, cid: int) -> str:
    return f'<article data-id="urn:li:comment:({post_urn},{cid})"><span>Antwort</span></article>'


def _page_html(*parts: str) -> str:
    return f"<html><body><main>{''.join(parts)}</main></body></html>"


@pytest.fixture
async def dom_page():
    """Real chromium page, or skip when no browser is installed."""
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


async def _scan(page, *, post_urns: bool = True):
    return await LinkedInExtractor(page)._extract_root_content(
        ["main"], post_urns=post_urns
    )


class TestPostUrnScan:
    async def test_posts_come_back_in_dom_order_with_their_commentary(self, dom_page):
        await dom_page.set_content(
            _page_html(_post(FIRST, text="Erster"), _post(SECOND, text="Zweiter"))
        )
        result = await _scan(dom_page)
        assert [p["urn"] for p in result["post_urns"]] == [FIRST, SECOND]
        assert [p["text"] for p in result["post_urns"]] == ["Erster", "Zweiter"]

    async def test_data_id_is_read_when_data_urn_is_absent(self, dom_page):
        await dom_page.set_content(_page_html(_post(THIRD, attr="data-id")))
        result = await _scan(dom_page)
        assert [p["urn"] for p in result["post_urns"]] == [THIRD]

    async def test_a_comment_id_never_reads_as_a_post(self, dom_page):
        # A comment's data-id embeds the post URN; a substring match would
        # return one "post" per comment.
        await dom_page.set_content(
            _page_html(_post(FIRST), _comment(FIRST, 1), _comment(FIRST, 2))
        )
        result = await _scan(dom_page)
        assert [p["urn"] for p in result["post_urns"]] == [FIRST]

    async def test_a_repeated_urn_is_reported_once(self, dom_page):
        # LinkedIn nests wrappers that repeat the card's data-urn.
        await dom_page.set_content(
            _page_html(f'<div data-urn="{FIRST}">{_post(FIRST)}</div>')
        )
        result = await _scan(dom_page)
        assert [p["urn"] for p in result["post_urns"]] == [FIRST]

    async def test_a_card_without_commentary_still_yields_its_urn(self, dom_page):
        await dom_page.set_content(_page_html(_post(FIRST, text=None)))
        result = await _scan(dom_page)
        assert result["post_urns"] == [{"urn": FIRST, "text": "", "in_article": False}]

    async def test_the_scan_is_capped_at_fifty(self, dom_page):
        cards = "".join(_post(f"urn:li:activity:7{n:09d}") for n in range(60))
        await dom_page.set_content(_page_html(cards))
        result = await _scan(dom_page)
        assert len(result["post_urns"]) == 50
        assert result["post_urns"][0]["urn"] == "urn:li:activity:7000000000"

    async def test_the_scan_is_off_unless_asked_for(self, dom_page):
        await dom_page.set_content(_page_html(_post(FIRST)))
        result = await _scan(dom_page, post_urns=False)
        assert "post_urns" not in result
        # The anchor pipeline is untouched either way.
        assert result["references"] == []
