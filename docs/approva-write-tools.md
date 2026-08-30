# Approva write tools — spec

**Status:** v1, 2026-08-30. Scope of the `approva` fork's diff against
`stickerdaniel/linkedin-mcp-server`.

This fork exists to give Approva's bot four write actions. It is the **execution surface only**:
one call does one write, immediately. It holds no queue, no approval logic, and no timers of its
own — those live in `linkedin-bot`. The single exception is `create_post`'s `schedule_at`, which
hands the post to *LinkedIn's own* scheduler so it still fires while the Mac is asleep.

## Rebase discipline

Everything new lives in `linkedin_mcp_server/approva/` and `linkedin_mcp_server/tools/approva.py`.
Upstream files are touched only by the registration lines in `server.py`. A change that needs a
deeper edit is a signal to reshape the change, not to spread the diff.

## Contract every tool obeys

1. **Refuses while the kill switch is set.** `guard.raise_if_stopped(tool_name)` runs before any
   navigation. The STOP file is `$APPROVA_STOP_FILE`, defaulting to
   `~/Approva/linkedin-bot/STOP`.
2. **Reports the page state it ended on.** Every return payload carries
   `page_state: normal | login | challenge`. The caller treats anything but `normal` as an
   anomaly: set STOP, notify, do not retry. Tools never attempt a login.
3. **One write per call, no retry, no scheduling.** A failed write returns an error; deciding
   whether to try again is the caller's.
4. **Types out, not scrapes out.** Return a dict, never a rendered string, so the caller can
   store it.

## Tools

| Tool | Signature | Status |
|---|---|---|
| `create_post` | `(text, image_paths?, schedule_at?)` | built |
| `approva_connect` | `(linkedin_username, note?)` | built |
| `approva_send_message` | `(linkedin_username, message)` | built |
| `approva_comment` | `(post_url, text)` | built |
| `inspect_composer` | `()` — dumps composer controls, posts nothing | built, debug aid |

### `create_post(text, image_paths=None, schedule_at=None)`

Publishes to the founder's feed. `image_paths` are local files attached in the order given and
validated before the composer opens (`composer.validate_images`) so a bad path fails before
anything is typed. `schedule_at` is a local ISO time (`2026-09-02T08:30`); when present the post
is handed to LinkedIn's native scheduler and the tool returns having scheduled, not posted.
Omit it to publish now.

Returns `{status, submitted, composer_closed, post_url, characters, images_attached,
scheduled_for, schedule_fields, url, page_state}`.

`post_url` is the published post's permalink, so the caller can comment on or track what it
just published without going to look for it. It is found by diffing the post-confirmation links
on the page against those present before submitting — an href shape, never the link's wording —
and falls back to the newest `data-urn` on the author's activity page. It is null for a
scheduled post, where LinkedIn is holding the post and there is nothing to link to yet, and
null rather than a guess when more than one new link appears.

`composer_closed` asks whether the editor we typed into is still on the page, by tagging that
element before typing. It previously asked whether a `div[role="dialog"]` was visible, which
never matched — the composer is not a dialog (`50872b7`) — so the field read `true` whether or
not the post went through.

### `approva_connect(linkedin_username, note=None)`

Delegates to upstream's connection flow — upstream already handles the degree states and the
note quota. The wrapper adds the kill switch, the page-state report, and a hard refusal when
`note` exceeds 300 characters (LinkedIn's limit; truncating a note silently would ship a
half sentence).

Returns `{url, status, message, note_sent, page_state}`. `status` is what distinguishes
`connected` / `already_connected` / `pending` / `accepted` / `connect_unavailable` /
`custom_note_limit_reached` / `send_failed`. `note_sent` reports delivery rather than that the
note was typed: an exhausted Premium note quota returns `custom_note_limit_reached` having sent
nothing at all — not an invite without a note — so a caller that wants the connection regardless
has to retry without one.

Upstream's `profile` key, the whole scraped profile page, is dropped here: a write should not
hand back a page scrape, and `get_person_profile` is there for callers who want one.

### `approva_send_message(linkedin_username, message)`

Delegates to upstream's messaging flow. Wrapper adds kill switch and page state.

Returns `{ok, page_state}`.

### `approva_comment(post_url, text)`

Comments on someone else's post, or on our own. `post_url` accepts either permalink shape
(`/feed/update/…`, `/posts/…`) or a bare `urn:li:activity:…`; a non-LinkedIn host is refused
rather than navigated to.

Three things the page is asked rather than assumed, because each replaces a guess:

- **Which box.** A permalink page renders exactly one editable — the share composer is on
  `/feed/`, and reply boxes only mount on a Reply click this tool never performs. So the count
  is the signal: one editable is unambiguous, more than one is resolved by scoping to the
  post's own container, and neither resolving stops the tool instead of typing into whatever
  came first.
- **Which button submits.** Not the one labelled "Comment" — that verb is locale-dependent.
  LinkedIn keeps the submit control disabled (or unmounted) until the box has content, so the
  button set is scanned before and after typing and the control that went disabled→enabled, or
  appeared from nothing, is the submit. Attribute *presence* and transition, never text.
- **Whether it worked.** Two signals, both diffed against the state captured before typing:
  how many times our text appears in the rendered body with the editor subtree removed, and
  which `urn:li:comment:(…)` values appear in the page HTML. Either confirms; neither can be
  satisfied by a comment already on the post. The editor is excluded from the count because an
  unsent draft still in the box would otherwise confirm itself. A submit that does not confirm
  raises, and says not to retry blind.

Newlines are typed as Shift+Enter — a bare Enter submits the comment in some renderings, which
would post half a sentence. The "2–6 s of human-plausible delay" is applied as the pause
between finishing the text and submitting.

Returns `{ok, post_url, comment_urn, comment_url, confirmed_by, characters, editor_cleared,
submitted_via, url, page_state}`. `comment_urn`/`comment_url` are null when the page exposed no
comment URN, or more than one new one — the permalink is withheld rather than guessed.

**Not built, deliberately:** replying to a specific comment inside a thread. Near neighbour of
this tool; add it when the bot needs it, not before.

## Verification

Each tool is verified by one hand-run against the live account, with the result pasted into the
PR. Selectors are pinned against a live DOM dump (`scripts/dump_snapshots.py`, and
`inspect_composer` for the post path) rather than guessed — the four assumptions corrected in
`50872b7` are why.

`create_post` and `approva_comment` were hand-run on 2026-08-30 against the test account
(`rahul-pai-562798259`): a post published to its own feed, then a comment on that post,
confirmed by reading the page back independently — two comments, one each, no duplicate. A
second run then chained the two, commenting on the permalink `create_post` returned; the
comment's own URN embedded that post's activity id, which is what proves the permalink right.

`approva_connect` and `approva_send_message` have not been hand-run. Both need a target account,
and messaging additionally needs an accepted connection before LinkedIn will open a composer.

That run corrected a fifth assumption. The permalink page is a different rendering from the
recent-activity page: it carries **no** `data-urn` on the post and **no** `data-id` holding a
comment URN, so the first confirmation attempt found nothing and — correctly — refused to
report success for a comment that had in fact posted. The comment URN is in the page HTML
without being an attribute, which is what confirmation reads now. The write path itself needed
no change: the single-visible-editable fallback is what carried it, and the submit control was
found by mounting rather than by a disabled→enabled flip.
