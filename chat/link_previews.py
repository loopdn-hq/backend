"""Link previews: a small card for the first link in a message.

WHOSE SETTING DECIDES
---------------------
The SENDER's. "Show link previews" sits under Settings -> Messaging -> Sending
messages, so it is a choice about the messages a person sends: with it on, a
link they send gets a card everybody in the conversation sees; with it off,
their links go out as plain text. `MessagingSettings.link_previews_enabled`.

WHO FETCHES THE PAGE
--------------------
The server, once, after the message is saved - never each reader's browser.
A reader opening a chat therefore does not visit the linked site or hand it
their address. The card is stored on the message and pushed to every open
chat with `message.updated`, so it appears a moment after the message.

The picture is the one exception: its URL is stored, not the image, so a
reader's browser loads it from the site. Only https pictures are kept, since an
http one on an https page is mixed content that browsers block.

SOME SITES ARE ASKED, NOT READ
------------------------------
A card normally comes from a page's <head> tags. YouTube's watch page is about
1.4MB, and its tags do not start until roughly 700KB in - past MAX_BYTES, so
reading it gave nothing. YouTube publishes the same card as oEmbed, a small JSON
answer made for exactly this, so links to it are asked there instead
(_OEMBED_PROVIDERS). The alternatives were worse: raising the cap triples the
download for every link and the next heavy site breaks it again, and sending
another company's crawler name to get the tags early is not ours to send.

TikTok is asked the same way: to a server its page is a 1KB challenge with no
tags at all. A link straight to an image becomes a card of the picture itself,
and a Google Images result - whose page says only "Google Images" - becomes a
card of the picture its link names. Shopee draws its pages in the browser and
carries no tags a server can read, so its links stay plain.

FETCHING A URL SOMEBODY TYPED IS DANGEROUS
------------------------------------------
Without care it is server-side request forgery: a message saying
http://169.254.169.254/ or http://localhost:8000/admin/ would make this server
fetch its own cloud metadata or internal services. So every fetch obeys:

    http or https only, on the standard ports, no credentials in the link
    every address the host resolves to must be public - checked on EVERY
        redirect hop, which are followed here by hand, at most MAX_REDIRECTS
    short timeouts, and no more than MAX_BYTES read
    HTML only; nothing is ever executed, only <head> tags are read

KNOWN LIMIT: the address is checked, then connected to. A hostname that
resolves to a public address for the check and a private one a moment later
(DNS rebinding) can slip between the two. Closing that needs connecting to the
exact address that was checked, which `requests` does not offer simply; it is
recorded here rather than pretended away.
"""
import ipaddress
import json
import logging
import re
import socket
import threading
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests
from django.db import connection, transaction

logger = logging.getLogger(__name__)

MAX_BYTES = 512 * 1024
# An oEmbed answer is a few hundred bytes; anything near this is not one.
MAX_OEMBED_BYTES = 64 * 1024
MAX_REDIRECTS = 3
# (connect, read) seconds. A page that is slow to answer gets no card; the
# message itself was delivered long before this matters.
TIMEOUT = (3, 5)
USER_AGENT = "LoopdnLinkPreview/1.0"
_REDIRECTS = frozenset({301, 302, 303, 307, 308})

# Host -> oEmbed endpoint, for sites whose card is better asked for than read.
# See "SOME SITES ARE ASKED, NOT READ" above. A new provider is one line here.
_YOUTUBE_OEMBED = "https://www.youtube.com/oembed"
_TIKTOK_OEMBED = "https://www.tiktok.com/oembed"
_OEMBED_PROVIDERS = {
    "youtube.com": _YOUTUBE_OEMBED,
    "www.youtube.com": _YOUTUBE_OEMBED,
    "m.youtube.com": _YOUTUBE_OEMBED,
    "youtu.be": _YOUTUBE_OEMBED,
    "tiktok.com": _TIKTOK_OEMBED,
    "www.tiktok.com": _TIKTOK_OEMBED,
    "m.tiktok.com": _TIKTOK_OEMBED,
}

_LINK = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""


class UnsafeLink(Exception):
    """A link this server will not fetch. The message says why, for the log."""


def first_link(text):
    """The first http(s) link in some text, or None.

    Trailing punctuation is dropped, so "see https://example.com." previews
    the page and not a URL ending in a full stop.
    """
    match = _LINK.search(text or "")
    if match is None:
        return None
    return match.group(0).rstrip(_TRAILING_PUNCTUATION) or None


def wants_preview(message):
    """Whether this message should carry a card at all.

    Not for a deleted message, not for one with no link, and not for one from
    somebody without an account - they have no setting to honour. Otherwise the
    sender's own "Show link previews" decides.
    """
    if message.is_deleted or message.sender_id is None:
        return False
    if first_link(message.text) is None:
        return False

    from user.models import MessagingSettings

    return MessagingSettings.of(message.sender).link_previews_enabled


def fetch_preview(url):
    """Fetch a link's card as a dict, or None if it has nothing to show.

    Four shapes, tried in this order:

        a Google Images result   the picture its link names
        an oEmbed provider       asked, not read (_OEMBED_PROVIDERS)
        a link to an image       the picture itself is the card
        any other page           read from its <head> tags

    Raises UnsafeLink for a link this server refuses to fetch, and lets
    `requests` errors through; `refresh_preview` treats both as "no card".
    """
    target = _google_images_target(url)
    if target is not None:
        image, source = target
        # Named in the link, so checked like any link: public, and really an
        # image.
        confirmed = _fetch(image, accept="image/*", kinds=("image/",), limit=0)
        if confirmed is None:
            return None
        return _card_from_image(url, confirmed[0], source or "Google Images")

    endpoint = _OEMBED_PROVIDERS.get((urlsplit(url).hostname or "").lower())
    if endpoint is not None:
        return _card_from_oembed(url, endpoint)

    fetched = _fetch(
        url,
        accept="text/html,application/xhtml+xml,image/*",
        kinds=("html", "image/"),
        limit=MAX_BYTES,
    )
    if fetched is None:
        return None
    final_url, content_type, html = fetched
    if content_type.startswith("image/"):
        return _card_from_image(url, final_url, urlsplit(final_url).hostname)
    return _card_from_html(html, link=url, page_url=final_url)


def _fetch(url, *, accept, kinds, limit):
    """GET a public URL the safe way. Returns (final_url, content_type, text), or None.

    The ONE place this module touches the network, so every source - a page, an
    oEmbed endpoint, an image - gets the same rules: each hop checked before it
    is fetched, redirects followed by hand, a timeout, and no more than `limit`
    bytes read. `kinds` lists what the Content-Type may contain ("html",
    "json", "image/"). An image is confirmed, never downloaded: its body is not
    read, so `text` is empty for one.
    """
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        _check_public(current)
        response = requests.get(
            current,
            stream=True,
            timeout=TIMEOUT,
            allow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": accept},
        )
        try:
            if response.status_code in _REDIRECTS:
                location = response.headers.get("Location")
                if not location:
                    return None
                current = urljoin(current, location)
                continue
            if response.status_code != 200:
                return None
            content_type = response.headers.get("Content-Type", "").lower()
            if not any(kind in content_type for kind in kinds):
                return None
            text = (
                ""
                if content_type.startswith("image/")
                else _read_capped(response, content_type, limit)
            )
        finally:
            response.close()
        return current, content_type, text
    return None


def refresh_preview(message_id):
    """Work out a message's card, save it, and tell everyone who can see it.

    Safe to run for any message at any time: it adds a card, replaces one for a
    changed link, or clears one the message should no longer have. Nothing is
    broadcast when nothing changed.
    """
    from chat.models import Message
    from chat.services import broadcast_message_updated

    message = Message.objects.select_related("sender").filter(pk=message_id).first()
    if message is None:
        return None

    card = None
    if wants_preview(message):
        link = first_link(message.text)
        try:
            card = fetch_preview(link)
        except UnsafeLink as refusal:
            logger.info("No link preview for message %s: %s", message_id, refusal)
        except (requests.RequestException, ValueError) as failure:
            logger.info("No link preview for message %s: %s", message_id, failure)

    if card == message.link_preview:
        return card

    # Matched on the text as it was read, so an edit that landed while the
    # page was being fetched is never given the card for its old link.
    updated = Message.objects.filter(pk=message_id, text=message.text).update(
        link_preview=card
    )
    if updated:
        fresh = Message.objects.filter(pk=message_id).first()
        if fresh is not None:
            broadcast_message_updated(fresh)
    return card


def schedule_link_preview(message):
    """Work out this message's card after the transaction commits.

    Called when a message is sent and when its text is edited. Returns at once
    when there is nothing to add or clear, so a message with no link never
    starts any work.
    """
    if first_link(message.text) is None and not message.link_preview:
        return
    message_id = message.pk
    transaction.on_commit(lambda: _start(message_id))


def _start(message_id):
    """On a Celery worker when there is one, otherwise on a background thread.

    Not inline, unlike the mail and SMS fan-outs: this runs from on_commit on
    the request that sent the message, and a slow page would hold that send
    for the whole timeout.
    """
    from loopd.celery import worker_available

    if worker_available():
        try:
            from chat.tasks import refresh_link_preview

            refresh_link_preview.delay(message_id)
            return
        except Exception:
            logger.exception(
                "Could not queue a link preview for message %s; using a thread",
                message_id,
            )

    threading.Thread(
        target=_refresh_in_thread,
        args=(message_id,),
        name=f"link-preview-{message_id}",
        daemon=True,
    ).start()


def _refresh_in_thread(message_id):
    try:
        refresh_preview(message_id)
    except Exception:
        logger.exception("Link preview failed for message %s", message_id)
    finally:
        # A thread gets its own database connection, and nothing else closes it.
        connection.close()


def _check_public(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeLink("not an http(s) link")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeLink("the port is not a number") from exc
    if port not in (None, 80, 443):
        raise UnsafeLink("not on a standard port")
    if parts.username or parts.password:
        raise UnsafeLink("the link carries credentials")

    try:
        infos = socket.getaddrinfo(
            parts.hostname,
            port or (443 if parts.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise UnsafeLink("the host does not resolve") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise UnsafeLink("the host does not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise UnsafeLink("the host is not a public address")


def _read_capped(response, content_type, limit):
    body = b""
    for chunk in response.iter_content(8192):
        body += chunk
        if len(body) >= limit:
            body = body[:limit]
            break
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class _HeadReader(HTMLParser):
    """Collects <meta> tags and the <title>, and stops caring at <body>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.title = ""
        self._in_title = False
        self._done = False

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        if tag == "meta":
            values = {name.lower(): (value or "") for name, value in attrs}
            key = (values.get("property") or values.get("name") or "").strip().lower()
            if key and key not in self.meta and values.get("content"):
                self.meta[key] = values["content"]
        elif tag == "title":
            self._in_title = True
        elif tag == "body":
            self._done = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self._done = True

    def handle_data(self, data):
        if self._in_title and not self._done:
            self.title += data


def _card_from_html(html, link, page_url):
    reader = _HeadReader()
    reader.feed(html)
    meta = reader.meta

    title = _clean(meta.get("og:title") or meta.get("twitter:title") or reader.title, 200)
    description = _clean(
        meta.get("og:description")
        or meta.get("twitter:description")
        or meta.get("description"),
        300,
    )
    site_name = _clean(meta.get("og:site_name"), 100)

    image_url = None
    image = meta.get("og:image") or meta.get("og:image:url") or meta.get("twitter:image")
    if image:
        candidate = urljoin(page_url, image.strip())
        if urlsplit(candidate).scheme == "https":
            image_url = candidate[:1000]

    if not (title or description or image_url):
        return None
    return {
        "url": link[:2000],
        "title": title,
        "description": description,
        "image_url": image_url,
        "site_name": site_name or urlsplit(page_url).hostname,
    }


def _google_images_target(url):
    """(image_url, source_host) for a Google Images result link, or None.

    Google Images shares a result as google.<tld>/imgres?imgurl=...&imgrefurl=...
    Its page says only "Google Images"; the picture, and the page it came from,
    are named in the link itself.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host.startswith(("google.", "www.google.", "images.google.")):
        return None
    if parts.path != "/imgres":
        return None
    query = parse_qs(parts.query)
    image = (query.get("imgurl") or [""])[0]
    if not image:
        return None
    page = (query.get("imgrefurl") or [""])[0]
    return image, (urlsplit(page).hostname if page else None)


def _card_from_image(link, image_url, source):
    """A card that is just the picture, for a link to an image.

    Only https, like every picture here. No title: an image link has none, and
    a filename is not one.
    """
    if urlsplit(image_url).scheme != "https":
        return None
    return {
        "url": link[:2000],
        "title": None,
        "description": None,
        "image_url": image_url[:1000],
        "site_name": _clean(source, 100),
    }


def _card_from_oembed(link, endpoint):
    """A card from an oEmbed answer: the title, who published it, the thumbnail.

    oEmbed has no description, so the second line is the author - for YouTube,
    the channel - which is what a reader wants under a video title anyway. A
    removed or private video answers with an error, and gets no card.
    """
    fetched = _fetch(
        f"{endpoint}?{urlencode({'url': link, 'format': 'json'})}",
        accept="application/json",
        kinds=("json",),
        limit=MAX_OEMBED_BYTES,
    )
    if fetched is None:
        return None
    try:
        data = json.loads(fetched[2])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    thumbnail = data.get("thumbnail_url")
    image_url = (
        thumbnail[:1000]
        if isinstance(thumbnail, str) and urlsplit(thumbnail).scheme == "https"
        else None
    )
    title = _clean(data.get("title"), 200)
    if not (title or image_url):
        return None
    return {
        "url": link[:2000],
        "title": title,
        "description": _clean(data.get("author_name"), 300),
        "image_url": image_url,
        "site_name": _clean(data.get("provider_name"), 100)
        or urlsplit(link).hostname,
    }


def _clean(value, limit):
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] or None
