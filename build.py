#!/usr/bin/env python3
"""Parttime Monster — a tiny static HTML5 blog generator.

No dependencies, no framework: Python 3.11+ standard library only.
Reads content from ./content, templates from ./templates, writes ./site.

All internal links are document-relative, so the site works on any domain,
under any sub-path (or even from a file system) without rebuilding.

Usage:
    python3 build.py                # build into ./site
    python3 build.py --serve        # build, then preview on http://localhost:8000
    python3 build.py --drafts       # include posts marked draft: true
    python3 build.py --output docs  # alternative output dir
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import re
import shutil
import sys
import tomllib
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# helpers


def e(text: object) -> str:
    """HTML-escape any value."""
    return html.escape(str(text), quote=True)


def strip_tags(fragment: str) -> str:
    """Crude tag stripper for search previews."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def legacy_key(url: str) -> str:
    """Normalise an old site URL to a lookup key: host and .html stripped,
    e.g. http://www.salttype.com/2016/05/blog-post_9.html -> 2016/05/blog-post_9"""
    path = urllib.parse.urlparse(url).path.rstrip("/")
    if path.endswith(".html"):
        path = path[:-5]
    return path.lstrip("/")


INTERNAL_LINK_RE = re.compile(
    r'href="(https?://(?:www\.)?salttype\.(?:com|blogspot\.com)/[^"]*)"', re.I)


def rewrite_internal_links(fragment: str, resolve) -> str:
    """Point old absolute salttype.com / salttype.blogspot.com links at their
    imported counterparts, resolved relative to the page being rendered."""
    def repl(m: re.Match) -> str:
        target = resolve(legacy_key(m.group(1)))
        if not target:
            return m.group(0)
        url = target + ("#" + urllib.parse.urlparse(m.group(1)).fragment
                        if urllib.parse.urlparse(m.group(1)).fragment else "")
        return f'href="{url}"'
    return INTERNAL_LINK_RE.sub(repl, fragment)


# /static/... inside imported post bodies is a site-root placeholder: the
# builder rewrites it to a document-relative path for the page being rendered.
BODY_STATIC_RE = re.compile(r"""(?<=["'(\s,])/static/""")
BODY_TOKEN_RE = re.compile(r"<a\b[^>]*>|</a>|<img\b[^>]*>", re.I)
IMG_SRC_RE = re.compile(r'\bsrc="([^"]*)"', re.I)
ANCHOR_HREF_RE = re.compile(r'\bhref="([^"]*)"', re.I)
ATTR_TARGET_RE = re.compile(r'\s+target="[^"]*"', re.I)
ATTR_REL_RE = re.compile(r'\s+rel="[^"]*"', re.I)
BLANK_RE = re.compile(r"(?:&nbsp;|&#160;|\xa0|\s)*")
CLOSE_ANCHOR_RE = re.compile(r"(?:&nbsp;|&#160;|\xa0|\s)*</a>", re.I)
IMAGE_URL_RE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|svgz?|avif|bmp|tiff?)(?:[?#].*)?$", re.I)


def is_image_url(url: str) -> bool:
    return bool(url) and ("/static/" in url or IMAGE_URL_RE.search(url) is not None)


def linkify_images(fragment: str) -> str:
    """Make images clickable: a bare image gets wrapped in a link to itself;
    an image that is already a link target keeps that target (the full-size
    copy). Either way the link opens in a new tab."""
    out: list[str] = []
    pos = 0
    anchor_at = None   # index of the still-open <a ...> token in `out`
    clean = False      # only whitespace between that <a ...> and the next tag

    for m in BODY_TOKEN_RE.finditer(fragment):
        gap = fragment[pos:m.start()]
        if BLANK_RE.fullmatch(gap) is None:
            clean = False
        out.append(gap)
        token = m.group(0)
        low = token.lower()

        if low.startswith("<a"):
            anchor_at = len(out)
            out.append(token)
            clean = True
        elif low == "</a>":
            out.append(token)
            anchor_at = None
            clean = False
        else:  # <img ...>
            src_m = IMG_SRC_RE.search(token)
            src = src_m.group(1) if src_m else ""
            if anchor_at is None:
                if src:
                    out.append(
                        f'<a href="{src}" target="_blank" rel="noopener">{token}</a>')
                else:
                    out.append(token)
            else:
                if clean and CLOSE_ANCHOR_RE.match(fragment, m.end()):
                    tag = out[anchor_at]
                    href_m = ANCHOR_HREF_RE.search(tag)
                    if href_m and is_image_url(href_m.group(1)):
                        tag = ATTR_TARGET_RE.sub("", tag)
                        tag = ATTR_REL_RE.sub("", tag)
                        out[anchor_at] = (
                            tag[:-1] + ' target="_blank" rel="noopener">')
                out.append(token)
        pos = m.end()

    out.append(fragment[pos:])
    return "".join(out)


class TS:
    """Tiny template: replaces {{ key }} with values. Unknown keys are fatal."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.cache: dict[str, str] = {}

    def render(self, name: str, **kw: str) -> str:
        if name not in self.cache:
            self.cache[name] = (self.dir / name).read_text("utf-8")
        text = self.cache[name]

        def sub(m: re.Match) -> str:
            key = m.group(1).strip()
            if key not in kw:
                raise KeyError(f"template {name}: missing variable {key!r}")
            return kw[key]

        return re.sub(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", sub, text)


# ---------------------------------------------------------------------------
# content model

FRONT_MATTER_RE = re.compile(r"^\ufeff?---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


def parse_document(path: Path) -> tuple[dict, str]:
    """Split a content file into (front matter dict, body text)."""
    text = path.read_text("utf-8")
    m = FRONT_MATTER_RE.match(text)
    if not m:
        raise ValueError(f"{path}: missing front matter")
    meta: dict = {}
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value.startswith('"'):
            value = json.loads(value)  # quoted values may contain : and escapes
        meta[key] = value
    return meta, m.group(2)


def md_to_html(md: str) -> str:
    """A deliberately small Markdown flavour.

    - blank line separates paragraphs and blockquotes
    - consecutive lines are joined with <br>
    - > starts a blockquote, > lines stack together
    - **bold**, *italic*, ~~strike~~, `code`, [links](https://…), ![images](…)
    - bare URLs are linked
    - a line starting with < is passed through as raw HTML (author trust)
    """
    blocks: list[str] = []
    para: list[str] = []
    quote: list[str] = []

    def flush_para() -> None:
        if para:
            body = "<br />\n".join(inline(x) for x in para)
            blocks.append(f"<p>{body}</p>")
            para.clear()

    def flush_quote() -> None:
        if quote:
            body = "<br />\n".join(inline(x) for x in quote)
            blocks.append(f"<blockquote>\n{body}\n</blockquote>")
            quote.clear()

    for raw in md.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        s = line.strip()
        if not s:
            flush_para()
            flush_quote()
        elif s.startswith("<"):
            flush_para()
            flush_quote()
            blocks.append(line)
        elif s.startswith(">"):
            flush_para()
            quote.append(s[1:].lstrip())
        elif s == "---":
            flush_para()
            flush_quote()
            blocks.append("<hr />")
        elif s.startswith("#"):  # headings, just in case
            flush_para()
            flush_quote()
            level = len(s) - len(s.lstrip("#"))
            blocks.append(f"<h{level}>{inline(s[level:].strip())}</h{level}>")
        else:
            flush_quote()
            para.append(s)
    flush_para()
    flush_quote()
    return "\n".join(blocks)


def inline(text: str) -> str:
    t = html.escape(text, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*(\S.*?)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"~~(.+?)~~", r"<del>\1</del>", t)
    t = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", r'<img src="\2" alt="\1" />', t)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r"(?<![\"'>=])(https?://[^\s<]+)", r'<a href="\1">\1</a>', t)
    return t


def default_slug(path: Path) -> str:
    """2026-10-18-hello.md -> hello; plain-stem files stay as they are."""
    m = re.match(r"\d{4}-\d{2}-\d{2}-(.+)$", path.stem)
    return m.group(1) if m else path.stem


class Post:
    def __init__(self, path: Path, meta: dict, body: str):
        self.path = path
        self.slug = str(meta.get("slug") or default_slug(path))
        self.title = str(meta.get("title") or self.slug)
        self.date = dt.datetime.fromisoformat(meta["date"]) if meta.get("date") else None
        self.legacy = str(meta.get("legacy", "") or "")
        self.comments_count = int(meta.get("comments_count", 0) or 0)
        self.draft = str(meta.get("draft", "false")).lower() in ("1", "true", "yes")
        if path.suffix == ".md":
            self.body_html = md_to_html(body)
        else:
            self.body_html = body.strip()
        self.url_path = f"/posts/{self.slug}/"
        self.archived_comments: list[dict] = []

    @property
    def sort_key(self) -> dt.datetime:
        return self.date or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


class Page:
    def __init__(self, path: Path, meta: dict, body: str):
        self.slug = str(meta.get("slug") or default_slug(path))
        self.title = str(meta.get("title") or self.slug)
        self.draft = str(meta.get("draft", "false")).lower() in ("1", "true", "yes")
        self.body_html = md_to_html(body) if path.suffix == ".md" else body.strip()
        self.url_path = f"/{self.slug}/"


# ---------------------------------------------------------------------------
# builder

class Builder:
    def __init__(self, root: Path, config: dict, include_drafts: bool):
        self.root = root
        self.cfg = config
        self.include_drafts = include_drafts

        site = config["site"]
        # site_url is optional: when set it is used for canonical/og:url and
        # sitemap only — every actual link stays relative either way.
        self.site_url = str(site.get("url", "") or "").rstrip("/")
        self.out_root = root / config["build"]["output"]

        self.tpl = TS(root / "templates")
        self.posts: list[Post] = []
        self.pages: list[Page] = []
        self.legacy_map: dict[str, Post] = {}
        self.prefix = ""  # relative prefix ("../../") for the page being rendered

    # -- url helpers ---------------------------------------------------------

    def set_ctx(self, url_path: str) -> None:
        """Remember how deep the page being rendered sits, so that links
        written by href() are relative to it."""
        if url_path in ("/", "/404.html"):
            self.prefix = ""
            return
        depth = len([s for s in url_path.strip("/").split("/") if s])
        if url_path.endswith(".html"):
            depth = max(depth - 1, 0)
        self.prefix = "../" * depth

    def href(self, path: str) -> str:
        """Resolve a site-internal path to a document-relative URL."""
        if path.startswith(("http://", "https://", "mailto:")):
            return path
        if path == "/":
            return self.prefix or "./"
        return self.prefix + path.lstrip("/")

    def canonical(self, path: str) -> str:
        """Absolute URL for metadata — empty unless [site] url is configured."""
        return f"{self.site_url}{path}" if self.site_url else ""

    def resolve_legacy(self, key: str) -> str:
        post = self.legacy_map.get(key)
        return self.href(post.url_path) if post else ""

    def resolve_static_assets(self, fragment: str) -> str:
        """Make /static/... asset references document-relative for the page
        currently in context."""
        return BODY_STATIC_RE.sub(lambda m: self.prefix + "static/", fragment)

    def prepare_body(self, fragment: str) -> str:
        """Render a post/page body for the page currently in context: old
        absolute links resolved to their imported counterparts, /static/
        assets made document-relative, images made clickable (a click opens
        the image — full size when one is linked — in a new tab)."""
        fragment = rewrite_internal_links(fragment, self.resolve_legacy)
        fragment = self.resolve_static_assets(fragment)
        return linkify_images(fragment)

    # -- loading --------------------------------------------------------------

    def load_content(self) -> None:
        posts_dir = self.root / "content" / "posts"
        for f in sorted(posts_dir.glob("*.[mh][dt]*")):  # .md, .html, .htm
            if f.suffix.lower() not in (".md", ".html", ".htm"):
                continue
            meta, body = parse_document(f)
            post = Post(f, meta, body)
            if post.draft and not self.include_drafts:
                continue
            self.posts.append(post)
        self.posts.sort(key=lambda p: p.sort_key, reverse=True)

        pages_dir = self.root / "content" / "pages"
        if pages_dir.exists():
            for f in sorted(pages_dir.iterdir()):
                if f.suffix.lower() not in (".md", ".html"):
                    continue
                meta, body = parse_document(f)
                page = Page(f, meta, body)
                if page.draft and not self.include_drafts:
                    continue
                self.pages.append(page)

        # map old URLs (blogspot.com / salttype.com) to imported posts
        for post in self.posts:
            if post.legacy:
                self.legacy_map[legacy_key(post.legacy)] = post

        # archived (Blogger) comments, one json file per post slug
        data_dir = self.root / "data" / "archived-comments"
        for post in self.posts:
            f = data_dir / f"{post.slug}.json"
            if f.exists():
                post.archived_comments = json.loads(f.read_text("utf-8"))
                post.comments_count = max(post.comments_count, len(post.archived_comments))

    # -- fragments -------------------------------------------------------------

    def archived_comments_html(self, post: Post) -> str:
        if not post.archived_comments:
            return ""
        items = []
        for c in post.archived_comments:
            author = e(c.get("author", "Unknown"))
            if c.get("author_url"):
                author = f'<a href="{e(c["author_url"])}" rel="nofollow">{author}</a>'
            when = e(c.get("date_text", ""))
            depth = int(c.get("depth", 0))
            style = f' style="margin-left:{min(depth, 4) * 24}px"' if depth else ""
            items.append(
                f'<li class="comment" id="blogger-{e(c.get("id", ""))}"{style}>'
                f'<div class="comment-header"><cite class="user">{author}</cite>'
                f'<span class="comment-sep"> · </span>'
                f'<span class="datetime">{when}</span></div>'
                f'<div class="comment-content">{c.get("body_html", "")}</div></li>'
            )
        return (
            '<section class="archived-comments" id="archived-comments">'
            "<h3>Comments from Blogger</h3>"
            f'<ol class="comment-list">{"".join(items)}</ol>'
            "</section>"
        )

    def comments_html(self) -> str:
        c = self.cfg.get("comments", {})
        provider = str(c.get("provider", "none")).lower()
        if provider == "giscus":
            if not c.get("giscus_repo_id") or not c.get("giscus_category_id"):
                return (
                    '<div class="comments-note"><p>Comments are not configured yet — '
                    "add <code>giscus_repo_id</code> and <code>giscus_category_id</code> "
                    "to <code>site.toml</code> (see README → Comments).</p></div>"
                )
            attrs = {
                "src": "https://giscus.app/client.js",
                "data-repo": c["giscus_repo"],
                "data-repo-id": c["giscus_repo_id"],
                "data-category": c["giscus_category"],
                "data-category-id": c["giscus_category_id"],
                "data-mapping": "pathname",
                "data-strict": "0",
                "data-reactions-enabled": "1" if c.get("giscus_reactions") else "0",
                "data-emit-metadata": "0",
                "data-input-position": "bottom",
                "data-theme": c.get("giscus_theme", "light"),
                "data-lang": "ru",
                "data-loading": "lazy",
                "crossorigin": "anonymous",
                "async": "",
            }
            tag = " ".join(f'{k}="{e(v)}"' if v else e(k) for k, v in attrs.items())
            return f'<div class="giscus-slot"><script {tag}></script></div>'
        if provider == "cf":
            return (
                '<div id="cf-comments" class="cf-comments">'
                '<p class="comments-note">Loading comments…</p></div>'
            )
        return ""

    def head_extra(self) -> str:
        """Per-page inline config + optional anonymous-comment assets."""
        c = self.cfg.get("comments", {})
        provider = str(c.get("provider", "none")).lower()
        lines = []
        if provider == "cf":
            lines.append(
                "<script>window.PM_COMMENTS = "
                + json.dumps(
                    {
                        "provider": "cf",
                        "api": c.get("cf_api", ""),
                        "sitekey": c.get("cf_turnstile_sitekey", ""),
                    },
                    ensure_ascii=False,
                )
                + ";</script>"
            )
            lines.append(
                '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" async defer></script>'
            )
        if str(self.cfg.get("search", {}).get("enabled", True)).lower() == "true":
            lines.append(
                "<script>window.PM_SEARCH = "
                + json.dumps({"index": self.href("/searchindex.json")})
                + ";</script>"
            )
        return "\n".join(lines)

    def sidebar_html(self) -> str:
        links = "".join(
            f'<li><a href="{e(self.href(l["href"]))}">{e(l["label"])}</a></li>'
            for l in self.cfg.get("sidebar", {}).get("links", [])
        )
        search = ""
        if str(self.cfg.get("search", {}).get("enabled", True)).lower() == "true":
            search = (
                '<form class="search-form" action="'
                + e(self.href("/search/"))
                + '" method="get" role="search">'
                f'<input type="search" name="q" placeholder="search…" aria-label="Search" />'
                "</form>"
            )
        return f"{search}<ul>{links}</ul>"

    def layout(self, *, title: str, description: str, content: str, path: str,
               body_class: str = "", body_extra: str = "", og_type: str = "website") -> str:
        self.set_ctx(path)
        cfg_site = self.cfg["site"]
        full_title = cfg_site["title"] if not title else f"{title} — {cfg_site['title']}"
        meta_urls = ""
        if self.site_url:
            meta_urls = (
                f'<link rel="canonical" href="{e(self.canonical(path))}">\n'
                f'<meta property="og:url" content="{e(self.canonical(path))}">'
            )
        return self.tpl.render(
            "base.html",
            lang=e(cfg_site.get("lang", "en")),
            title=e(full_title),
            description=e(description or cfg_site.get("description", "")),
            og_type=e(og_type),
            meta_urls=meta_urls,
            prefix=self.prefix,
            favicon=e(self.href("/static/favicon.svg")),
            feed_link=e(self.href("/feed.xml")),
            home=e(self.href("/")),
            site_title=e(cfg_site["title"]),
            sidebar=self.sidebar_html(),
            content=content,
            head_extra=self.head_extra(),
            body_extra=body_extra,
            body_class=e(body_class),
            footer=self.footer_html(),
        )

    def footer_html(self) -> str:
        site = self.cfg["site"]
        year = dt.date.today().year
        since = site.get("copyright_since")
        span = f"{since}–{year}" if since and int(since) < year else str(year)
        feed = f'<a href="{e(self.href("/feed.xml"))}">rss</a>'
        return f'© {span} {e(site.get("author", ""))} · {feed}'

    # -- page builders ----------------------------------------------------------

    def post_card(self, post: Post, *, comments_link: bool) -> str:
        title = e(post.title)
        url = e(self.href(post.url_path))
        body = self.prepare_body(post.body_html)
        comments = ""
        if comments_link:
            label = "comments"
            if post.comments_count:
                label += f" ({post.comments_count})"
            comments = (
                f'<footer class="post-footer"><span class="post-comment-link">'
                f'<a class="comment-link" href="{url}#comments">{label}</a>'
                f"</span></footer>"
            )
        else:
            comments = (
                '<section class="comments" id="comments">'
                '<h3 class="comments-title">Комментарии</h3>'
                f"{self.archived_comments_html(post)}"
                f"{self.comments_html()}"
                "</section>"
            )
        h = "h2"
        return (
            f'<article class="post hentry">'
            f'<{h} class="post-title entry-title"><a href="{url}">{title}</a></{h}>'
            f'<div class="post-body entry-content">{body}</div>'
            f"{comments}"
            f"</article>"
        )

    def build_posts(self) -> None:
        for post in self.posts:
            self.set_ctx(post.url_path)
            extra = ""
            provider = str(self.cfg.get("comments", {}).get("provider", "none")).lower()
            if provider == "cf":
                extra = f'<script src="{e(self.href("/static/js/comments.js"))}" defer></script>'
            body = self.post_card(post, comments_link=False)
            html_doc = self.layout(
                title=post.title,
                description=strip_tags(post.body_html)[:200],
                content=body,
                path=post.url_path,
                body_class="post-page",
                body_extra=extra,
                og_type="article",
            )
            self.write(post.url_path, html_doc)

    def build_index(self) -> None:
        per_page = int(self.cfg["site"].get("posts_per_page", 50))
        chunks = [self.posts[i:i + per_page] for i in range(0, len(self.posts), per_page)] or [[]]
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            path = "/" if i == 1 else f"/page/{i}/"
            self.set_ctx(path)
            cards = "\n".join(self.post_card(p, comments_link=True) for p in chunk)
            pager = []
            if i > 1:
                prev = "/" if i == 2 else f"/page/{i - 1}/"
                pager.append(f'<a class="pager-newer" href="{e(self.href(prev))}">Newer Posts</a>')
            if i < total:
                pager.append(
                    f'<a class="pager-older" href="{e(self.href(f"/page/{i + 1}/"))}">Older Posts</a>'
                )
            body = cards + f'<nav class="pager">{"".join(pager)}</nav>'
            self.write(path, self.layout(
                title="" if i == 1 else f"Page {i}",
                description=self.cfg["site"].get("description", ""),
                content=body,
                path=path,
                body_class="index-page",
            ))

    def build_pages(self) -> None:
        for page in self.pages:
            self.set_ctx(page.url_path)
            body = (
                f'<article class="page"><h2 class="page-title">{e(page.title)}</h2>'
                f'<div class="post-body">{self.prepare_body(page.body_html)}</div></article>'
            )
            self.write(page.url_path, self.layout(
                title=page.title,
                description=strip_tags(page.body_html)[:200],
                content=body,
                path=page.url_path,
                body_class="page",
            ))

    def build_archive(self) -> None:
        self.set_ctx("/archive/")
        groups: dict[int, dict[int, list[Post]]] = {}
        for p in self.posts:
            if not p.date:
                continue
            groups.setdefault(p.date.year, {}).setdefault(p.date.month, []).append(p)
        months = ["", "January", "February", "March", "April", "May", "June", "July",
                  "August", "September", "October", "November", "December"]
        out = ['<article class="archive"><h2 class="page-title">Archives</h2>']
        for year in sorted(groups, reverse=True):
            year_count = sum(len(v) for v in groups[year].values())
            out.append(f'<h3 class="archive-year">{year} <span class="count">({year_count})</span></h3>')
            for month in sorted(groups[year], reverse=True):
                posts = groups[year][month]
                out.append(
                    f'<h4 class="archive-month">{months[month]} <span class="count">({len(posts)})</span></h4>'
                )
                items = "".join(
                    f'<li><a href="{e(self.href(p.url_path))}">{e(p.title)}</a></li>'
                    for p in posts
                )
                out.append(f"<ul class=\"archive-posts\">{items}</ul>")
        out.append("</article>")
        path = "/archive/"
        self.write(path, self.layout(
            title="Archives",
            description="Все заметки по годам",
            content="\n".join(out),
            path=path,
            body_class="archive-page",
        ))

    def build_search(self) -> None:
        if str(self.cfg.get("search", {}).get("enabled", True)).lower() != "true":
            return
        self.set_ctx("/search/")
        entries = [
            {
                "u": self.href(p.url_path),
                "t": p.title,
                "d": strip_tags(p.body_html)[:420],
                "dt": p.date.date().isoformat() if p.date else "",
            }
            for p in self.posts
        ]
        (self.out_root / "searchindex.json").write_text(
            json.dumps(entries, ensure_ascii=False, separators=(",", ":")), "utf-8"
        )
        body = (
            '<article class="search"><h2 class="page-title">Search</h2>'
            '<p class="search-hint">Поиск по заметкам. Начните печатать — результаты '
            "появляются сразу, всё считается в браузере.</p>"
            '<p><input type="search" id="q" class="search-input" '
            'placeholder="search…" autofocus aria-label="Search" /></p>'
            '<div id="results" class="search-results" aria-live="polite"></div>'
            "</article>"
        )
        extra = f'<script src="{e(self.href("/static/js/search.js"))}" defer></script>'
        path = "/search/"
        self.write(path, self.layout(
            title="Search",
            description="Поиск по блогу",
            content=body,
            path=path,
            body_class="search-page",
            body_extra=extra,
        ))

    def build_feed(self) -> None:
        size = int(self.cfg["feed"].get("size", 25))
        site = self.cfg["site"]
        self.prefix = ""  # feed.xml sits in the site root
        items = []
        for p in self.posts[:size]:
            if not p.date:
                continue
            pub = email.utils.format_datetime(p.date)
            link = self.href(p.url_path)  # relative to /feed.xml
            body = self.prepare_body(p.body_html)
            body = body.replace("]]>", "]]&gt;")
            items.append(
                "<item>"
                f"<title>{e(p.title)}</title>"
                f"<link>{e(link)}</link>"
                f'<guid isPermaLink="false">{e(link)}</guid>'
                f"<pubDate>{pub}</pubDate>"
                f"<content:encoded><![CDATA[{body}]]></content:encoded>"
                "</item>"
            )
        latest = self.posts[0].date if self.posts and self.posts[0].date else dt.datetime.now(dt.timezone.utc)
        # RSS wants an absolute channel link; without a configured url we emit
        # a relative one (".") — readers resolve it against the feed location.
        channel_link = f"{self.site_url}/" if self.site_url else "."
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">\n'
            "<channel>"
            f"<title>{e(site['title'])}</title>"
            f"<link>{e(channel_link)}</link>"
            f"<description>{e(site.get('description', ''))}</description>"
            f"<language>{e(site.get('lang', 'en'))}</language>"
            f"<lastBuildDate>{email.utils.format_datetime(latest)}</lastBuildDate>"
            f"{''.join(items)}"
            "</channel></rss>\n"
        )
        (self.out_root / "feed.xml").write_text(xml, "utf-8")

    def build_sitemap(self) -> None:
        # a sitemap requires absolute URLs, so it only exists when the site
        # url is configured; without it the whole site is domain-agnostic
        if not self.site_url:
            return
        urls = [("/", None)]
        urls += [(p.url_path, p.date) for p in self.posts]
        urls += [(pg.url_path, None) for pg in self.pages]
        urls.append(("/archive/", None))
        if str(self.cfg.get("search", {}).get("enabled", True)).lower() == "true":
            urls.append(("/search/", None))
        parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for path, date in urls:
            lastmod = f"<lastmod>{date.date().isoformat()}</lastmod>" if date else ""
            parts.append(f"<url><loc>{e(self.canonical(path))}</loc>{lastmod}</url>")
        parts.append("</urlset>")
        (self.out_root / "sitemap.xml").write_text("\n".join(parts), "utf-8")

    def build_404(self) -> None:
        self.set_ctx("/404.html")
        # GitHub Pages serves this file for URLs at *any* depth, so plain
        # relative links cannot be computed at build time; a tiny script finds
        # the site root by probing upwards for feed.xml and fixes the links.
        probe = (
            "<script>(function(){"
            "function fix(d){var p=d?new Array(d+1).join('../'):'./';"
            "document.querySelectorAll('a[data-root]').forEach(function(a){"
            "a.href=p+a.getAttribute('data-root').replace(/^\\//,'');});}"
            "function probe(d){if(d>10){fix(0);return;}"
            "fetch((d?new Array(d+1).join('../'):'./')+'feed.xml',{method:'HEAD'})"
            ".then(function(r){r.ok?fix(d):probe(d+1);})"
            ".catch(function(){probe(d+1);});}"
            "probe(0);})();</script>"
        )
        body = (
            '<article class="page"><h2 class="page-title">404</h2>'
            "<p>Страница не найдена.</p>"
            f'<p><a href="{e(self.href("/"))}" data-root="/">На главную</a></p></article>'
        )
        self.write("/404.html", self.layout(
            title="404", description="Not found", content=body, path="/404.html",
            body_extra=probe,
        ))

    def copy_static(self) -> None:
        src = self.root / "static"
        dst = self.out_root / "static"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        (self.out_root / ".nojekyll").write_text("", "utf-8")
        robots = "User-agent: *\nAllow: /\n"
        if self.site_url:
            robots += f"Sitemap: {self.canonical('/sitemap.xml')}\n"
        (self.out_root / "robots.txt").write_text(robots, "utf-8")

    # -- output ------------------------------------------------------------------

    def write(self, url_path: str, html_doc: str) -> None:
        if url_path.endswith(".html"):
            f = self.out_root / url_path.lstrip("/")
        else:
            f = self.out_root / url_path.strip("/") / "index.html" if url_path.strip("/") \
                else self.out_root / "index.html"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(html_doc, "utf-8")

    def run(self) -> None:
        if self.out_root.exists():
            shutil.rmtree(self.out_root)
        self.out_root.mkdir(parents=True)
        self.load_content()
        self.build_posts()
        self.build_index()
        self.build_pages()
        self.build_archive()
        self.build_search()
        self.build_feed()
        self.build_sitemap()
        self.build_404()
        self.copy_static()

        warnings = []
        c = self.cfg.get("comments", {})
        provider = str(c.get("provider", "giscus")).lower()
        if provider == "giscus" and (not c.get("giscus_repo_id") or not c.get("giscus_category_id")):
            warnings.append("comments: giscus ids are not set yet (see README → Comments); "
                            "post pages currently show a setup note.")
        if provider == "cf" and not c.get("cf_api"):
            warnings.append("comments: cf_api is empty (see extras/cloudflare-comments/README.md).")
        print(f"built {len(self.posts)} posts, {len(self.pages)} pages -> {self.out_root}")
        if not self.site_url:
            print("note: [site] url is empty — all links are relative; "
                  "canonical/og:url and sitemap.xml are skipped.")
        for w in warnings:
            print(f"warning: {w}")


# ---------------------------------------------------------------------------
# preview server


def serve(directory: Path, port: int) -> None:
    import http.server
    import socketserver

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kw):
            super().__init__(*args, directory=str(directory), **kw)

        def log_message(self, fmt, *args):  # keep the noise down
            sys.stderr.write("%s\n" % (fmt % args))

    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"serving {directory} at http://localhost:{port}/  (Ctrl+C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="build the Parttime Monster blog")
    ap.add_argument("--config", default="site.toml")
    ap.add_argument("--output")
    ap.add_argument("--serve", action="store_true", help="preview locally after building")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--drafts", action="store_true", help="include drafts")
    args = ap.parse_args()

    config = tomllib.loads((ROOT / args.config).read_text("utf-8"))
    if args.output:
        config["build"]["output"] = args.output

    builder = Builder(ROOT, config, include_drafts=args.drafts)
    builder.run()

    if args.serve:
        serve(builder.out_root, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
