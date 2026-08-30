"""Unit tests for the comment tool's pre-flight checks.

Everything here runs before the browser is touched, which is the point: a
malformed post URL or an oversized body must cost no page load and leave no
half-typed draft behind.
"""

from __future__ import annotations

import pytest

from linkedin_mcp_server.approva.comment import (
    MAX_COMMENT_CHARS,
    CommentError,
    _needle,
    normalize_post_url,
    validate_text,
)


class TestNormalizePostUrl:
    def test_feed_update_url_yields_its_urn(self):
        url, urn = normalize_post_url(
            "https://www.linkedin.com/feed/update/urn:li:activity:7123456789/"
        )
        assert url == "https://www.linkedin.com/feed/update/urn:li:activity:7123456789/"
        assert urn == "urn:li:activity:7123456789"

    def test_percent_encoded_urn_is_decoded_for_matching(self):
        _, urn = normalize_post_url(
            "https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A7123456789/"
        )
        assert urn == "urn:li:activity:7123456789"

    def test_posts_slug_yields_the_activity_urn(self):
        _, urn = normalize_post_url(
            "https://www.linkedin.com/posts/williamhgates_a-title-here"
            "-activity-7123456789-AbCd"
        )
        assert urn == "urn:li:activity:7123456789"

    def test_bare_urn_becomes_a_permalink(self):
        url, urn = normalize_post_url("urn:li:activity:7123456789")
        assert url == "https://www.linkedin.com/feed/update/urn:li:activity:7123456789/"
        assert urn == "urn:li:activity:7123456789"

    def test_tracking_parameters_are_dropped(self):
        url, _ = normalize_post_url(
            "https://www.linkedin.com/feed/update/urn:li:activity:7123456789/"
            "?utm_source=share&trackingId=abc"
        )
        assert url.endswith("/urn:li:activity:7123456789/")
        assert "?" not in url

    def test_urnless_posts_slug_is_accepted_without_a_urn(self):
        # A /posts/ slug does not always carry the activity id. The page still
        # loads, so the flow proceeds unscoped rather than refusing.
        url, urn = normalize_post_url("https://www.linkedin.com/posts/some-slug")
        assert url == "https://www.linkedin.com/posts/some-slug"
        assert urn is None

    def test_non_linkedin_host_is_refused(self):
        with pytest.raises(CommentError, match="linkedin.com"):
            normalize_post_url(
                "https://evil.example.com/feed/update/urn:li:activity:1/"
            )

    def test_lookalike_host_is_refused(self):
        with pytest.raises(CommentError, match="linkedin.com"):
            normalize_post_url("https://linkedin.com.evil.example/posts/x-activity-1-a")

    def test_linkedin_subdomain_is_accepted(self):
        url, _ = normalize_post_url(
            "https://de.linkedin.com/feed/update/urn:li:activity:7123456789/"
        )
        assert url.startswith("https://de.linkedin.com/")

    def test_non_post_linkedin_path_is_refused(self):
        with pytest.raises(CommentError, match="post permalink"):
            normalize_post_url("https://www.linkedin.com/in/williamhgates/")

    def test_malformed_urn_is_refused(self):
        with pytest.raises(CommentError, match="activity URN"):
            normalize_post_url("urn:li:fsd_profile:ACoAAB")

    def test_empty_is_refused(self):
        with pytest.raises(CommentError, match="empty"):
            normalize_post_url("   ")


class TestValidateText:
    def test_body_is_stripped(self):
        assert validate_text("  hello  ") == "hello"

    def test_whitespace_only_is_refused(self):
        with pytest.raises(CommentError, match="empty"):
            validate_text("\n\t ")

    def test_at_the_limit_is_accepted(self):
        body = "x" * MAX_COMMENT_CHARS
        assert validate_text(body) == body

    def test_over_the_limit_is_refused(self):
        with pytest.raises(CommentError, match=str(MAX_COMMENT_CHARS)):
            validate_text("x" * (MAX_COMMENT_CHARS + 1))


class TestNeedle:
    def test_whitespace_is_collapsed_so_a_dom_read_can_match(self):
        # LinkedIn re-flows a comment's markup; the needle has to survive that.
        assert _needle("Great\n\n  post,   Bill") == "great post, bill"

    def test_long_bodies_are_truncated_below_the_see_more_fold(self):
        assert len(_needle("word " * 200)) == 40
