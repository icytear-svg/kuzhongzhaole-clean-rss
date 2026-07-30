#!/usr/bin/env python3
"""Build a static RSS feed with complete Xiaoyuzhou show notes.

The generated feed keeps every episode GUID, publication date, enclosure URL,
length, and MIME type unchanged. Full rich show notes are read from each public
episode page, cleaned of Xiaoyuzhou calls to action, and written to both
``description`` and ``content:encoded``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import escape as escape_html
from pathlib import Path
from typing import Any

from lxml import etree, html


ROOT = Path(__file__).resolve().parent
DEFAULT_FEED_URL = "https://feed.xyzfm.space/77h77qmhwntd"
DEFAULT_OUTPUT_DIR = ROOT / "clean_feed" / "output"
DEFAULT_CACHE_DIR = ROOT / "clean_feed" / "cache"
DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:8765"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/136 Safari/537.36 clean-rss/1.0"
)

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_TAG = f"{{{CONTENT_NS}}}encoded"
ATOM_LINK_TAG = f"{{{ATOM_NS}}}link"

TRACKING_PHRASES = (
    "去小宇宙查看完整单集简介",
    "在小宇宙查看该单集文稿",
    "在小宇宙查看完整单集简介",
    "在小宇宙查看完整内容",
)
XIAOYUZHOU_HOSTS = {
    "www.xiaoyuzhoufm.com",
    "xiaoyuzhoufm.com",
    "oia.xiaoyuzhoufm.com",
}
DROP_TAGS = {"script", "style", "iframe", "object", "embed", "form", "input"}
ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "div",
    "em",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "span",
    "strong",
    "ul",
}
IMAGE_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@dataclass(frozen=True)
class FeedEpisode:
    position: int
    guid: str
    title: str
    link: str
    description: str
    enclosure_url: str
    enclosure_length: str
    enclosure_type: str


@dataclass
class PageRecord:
    guid: str
    title: str
    source_url: str
    source_kind: str
    fetched_at: str
    shownotes_html: str
    image_urls: list[str]
    error: str = ""


@dataclass
class CleanResult:
    html: str
    plain_text: str
    image_urls: list[str]
    localized_images: int
    external_images: int
    xiaoyuzhou_links_removed: int
    tracking_phrases_removed: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.part")
    temporary.write_bytes(content)
    temporary.replace(path)


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


def request(
    url: str,
    *,
    timeout: int,
    retries: int,
    method: str = "GET",
) -> tuple[bytes, dict[str, str]]:
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            req = urllib.request.Request(
                url,
                method=method,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                headers = {key.lower(): value for key, value in response.headers.items()}
                return response.read(), headers
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.25 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_feed(raw_feed: bytes) -> tuple[etree._Element, list[FeedEpisode]]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    root = etree.fromstring(raw_feed, parser=parser)
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS feed does not contain a channel element")

    episodes: list[FeedEpisode] = []
    for position, item in enumerate(channel.findall("item"), start=1):
        guid = normalize_space(item.findtext("guid") or "")
        if not guid:
            raise RuntimeError(f"RSS item #{position} has no GUID")
        enclosure = item.find("enclosure")
        episodes.append(
            FeedEpisode(
                position=position,
                guid=guid,
                title=normalize_space(item.findtext("title") or ""),
                link=normalize_space(item.findtext("link") or ""),
                description=item.findtext("description") or "",
                enclosure_url=enclosure.get("url", "") if enclosure is not None else "",
                enclosure_length=enclosure.get("length", "") if enclosure is not None else "",
                enclosure_type=enclosure.get("type", "") if enclosure is not None else "",
            )
        )
    return root, episodes


def episode_page_url(episode: FeedEpisode) -> str:
    parsed = urllib.parse.urlparse(episode.link)
    if parsed.netloc.lower() in XIAOYUZHOU_HOSTS and "/episode/" in parsed.path:
        return urllib.parse.urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path, "", "", ""))
    return f"https://www.xiaoyuzhoufm.com/episode/{episode.guid}"


def extract_next_data(raw_page: bytes) -> dict[str, Any]:
    document = html.fromstring(raw_page)
    values = document.xpath('//script[@id="__NEXT_DATA__"]/text()')
    if not values:
        raise ValueError("Page does not contain __NEXT_DATA__")
    return json.loads(values[0])


def extract_image_urls(raw_html: str) -> list[str]:
    if not raw_html.strip():
        return []
    try:
        wrapper = html.fragment_fromstring(raw_html, create_parent="div")
    except (etree.ParserError, ValueError):
        return []
    return list(
        dict.fromkeys(
            source.strip()
            for source in wrapper.xpath(".//img/@src")
            if source.strip().startswith(("http://", "https://"))
        )
    )


def load_cached_page(path: Path, expected_guid: str) -> PageRecord | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        record = PageRecord(**data)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if record.guid != expected_guid or not record.shownotes_html.strip():
        return None
    record.source_kind = "page-cache"
    return record


def fetch_page_record(
    episode: FeedEpisode,
    cache_dir: Path,
    *,
    refresh: bool,
    timeout: int,
    retries: int,
) -> PageRecord:
    cache_path = cache_dir / "episodes" / f"{episode.guid}.json"
    if not refresh:
        cached = load_cached_page(cache_path, episode.guid)
        if cached is not None:
            return cached

    source_url = episode_page_url(episode)
    try:
        raw_page, _ = request(source_url, timeout=timeout, retries=retries)
        next_data = extract_next_data(raw_page)
        page_episode = next_data["props"]["pageProps"]["episode"]
        page_guid = str(page_episode.get("eid") or "")
        if page_guid != episode.guid:
            raise ValueError(f"EID mismatch: expected {episode.guid}, got {page_guid}")
        rich_html = str(
            page_episode.get("shownotes")
            or page_episode.get("description")
            or episode.description
            or ""
        )
        record = PageRecord(
            guid=episode.guid,
            title=str(page_episode.get("title") or episode.title),
            source_url=source_url,
            source_kind="public-page",
            fetched_at=utc_now(),
            shownotes_html=rich_html,
            image_urls=extract_image_urls(rich_html),
        )
        write_json(cache_path, asdict(record))
        return record
    except Exception as exc:
        fallback = episode.description
        return PageRecord(
            guid=episode.guid,
            title=episode.title,
            source_url=source_url,
            source_kind="rss-fallback",
            fetched_at=utc_now(),
            shownotes_html=fallback,
            image_urls=extract_image_urls(fallback),
            error=str(exc),
        )


def fetch_all_page_records(
    episodes: list[FeedEpisode],
    cache_dir: Path,
    *,
    workers: int,
    refresh: bool,
    timeout: int,
    retries: int,
) -> dict[str, PageRecord]:
    records: dict[str, PageRecord] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                fetch_page_record,
                episode,
                cache_dir,
                refresh=refresh,
                timeout=timeout,
                retries=retries,
            ): episode
            for episode in episodes
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            episode = futures[future]
            try:
                record = future.result()
            except Exception as exc:  # defensive batch fallback
                record = PageRecord(
                    guid=episode.guid,
                    title=episode.title,
                    source_url=episode_page_url(episode),
                    source_kind="rss-fallback",
                    fetched_at=utc_now(),
                    shownotes_html=episode.description,
                    image_urls=extract_image_urls(episode.description),
                    error=str(exc),
                )
            records[episode.guid] = record
            status = "FALLBACK" if record.error else "OK"
            print(
                f"[pages {completed:03d}/{len(episodes):03d}] {status} "
                f"{episode.guid} {episode.title[:42]}",
                flush=True,
            )
    return records


def extension_from_url(url: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed, _ = mimetypes.guess_type(url)
    return IMAGE_CONTENT_TYPE_EXTENSIONS.get(guessed or "", ".bin")


def asset_filename(url: str, extension: str | None = None) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return f"{digest}{extension or extension_from_url(url)}"


def download_asset(
    url: str,
    assets_dir: Path,
    *,
    timeout: int,
    retries: int,
) -> dict[str, Any]:
    initial_name = asset_filename(url)
    initial_path = assets_dir / initial_name
    if initial_path.exists() and initial_path.stat().st_size > 0:
        return {
            "original_url": url,
            "filename": initial_name,
            "bytes": initial_path.stat().st_size,
            "content_type": mimetypes.guess_type(initial_name)[0] or "",
            "status": "cached",
            "error": "",
        }

    try:
        content, headers = request(url, timeout=timeout, retries=retries)
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        extension = IMAGE_CONTENT_TYPE_EXTENSIONS.get(content_type, extension_from_url(url))
        filename = asset_filename(url, extension)
        path = assets_dir / filename
        if not path.exists() or path.stat().st_size == 0:
            atomic_write_bytes(path, content)
        return {
            "original_url": url,
            "filename": filename,
            "bytes": len(content),
            "content_type": content_type,
            "status": "downloaded",
            "error": "",
        }
    except Exception as exc:
        return {
            "original_url": url,
            "filename": "",
            "bytes": 0,
            "content_type": "",
            "status": "failed",
            "error": str(exc),
        }


def download_assets(
    urls: list[str],
    assets_dir: Path,
    *,
    workers: int,
    timeout: int,
    retries: int,
) -> dict[str, dict[str, Any]]:
    assets_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                download_asset,
                url,
                assets_dir,
                timeout=timeout,
                retries=retries,
            ): url
            for url in urls
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            url = futures[future]
            result = future.result()
            results[url] = result
            print(
                f"[images {completed:03d}/{len(urls):03d}] "
                f"{result['status'].upper()} {result['filename'] or url}",
                flush=True,
            )
    return results


def is_xiaoyuzhou_url(value: str) -> bool:
    try:
        return urllib.parse.urlparse(value).netloc.lower() in XIAOYUZHOU_HOSTS
    except ValueError:
        return False


def scrub_tracking_text(value: str | None) -> tuple[str | None, int]:
    if not value:
        return value, 0
    cleaned = value
    removed = 0
    for phrase in TRACKING_PHRASES:
        count = cleaned.count(phrase)
        if count:
            cleaned = cleaned.replace(phrase, "")
            removed += count
    return cleaned, removed


def clean_shownotes(
    raw_html: str,
    *,
    localized_asset_urls: dict[str, str],
) -> CleanResult:
    try:
        wrapper = html.fragment_fromstring(raw_html or "", create_parent="div")
    except (etree.ParserError, ValueError):
        wrapper = html.Element("div")
        paragraph = html.SubElement(wrapper, "p")
        paragraph.text = raw_html or ""

    removed_links = 0
    removed_phrases = 0

    for node in list(wrapper.xpath(".//*")):
        tag = str(node.tag).lower() if isinstance(node.tag, str) else ""
        if tag in DROP_TAGS:
            node.drop_tree()

    for anchor in list(wrapper.xpath(".//a")):
        label = normalize_space("".join(anchor.itertext()))
        href = (anchor.get("href") or anchor.get("data-url") or "").strip()
        if any(phrase in label for phrase in TRACKING_PHRASES):
            removed_phrases += sum(label.count(phrase) for phrase in TRACKING_PHRASES)
            anchor.drop_tree()
            continue
        if not href:
            anchor.drop_tag()
            continue
        if is_xiaoyuzhou_url(href):
            removed_links += 1
            anchor.drop_tag()
            continue
        parsed = urllib.parse.urlparse(href)
        if parsed.scheme != "https":
            anchor.drop_tag()
            continue
        anchor.attrib.clear()
        anchor.set("href", href)

    for node in [wrapper, *list(wrapper.iterdescendants())]:
        node.text, removed = scrub_tracking_text(node.text)
        removed_phrases += removed
        node.tail, removed = scrub_tracking_text(node.tail)
        removed_phrases += removed

    for image in list(wrapper.xpath(".//img")):
        source = (image.get("src") or "").strip()
        if not source.startswith(("http://", "https://")):
            image.drop_tree()
            continue
        public_source = localized_asset_urls.get(source, source)
        alt = (image.get("alt") or "").strip()
        title = (image.get("title") or "").strip()
        image.attrib.clear()
        image.set("src", public_source)
        if alt:
            image.set("alt", alt)
        if title:
            image.set("title", title)

    for node in list(wrapper.iterdescendants()):
        if not isinstance(node.tag, str):
            continue
        tag = node.tag.lower()
        if tag not in ALLOWED_TAGS:
            node.drop_tag()
            continue
        if tag not in {"a", "img"}:
            node.attrib.clear()

    clean_html = "".join(
        etree.tostring(child, method="html", encoding="unicode")
        for child in wrapper
    ).strip()
    if not clean_html and wrapper.text:
        clean_html = f"<p>{escape_html(wrapper.text)}</p>"

    reparsed = html.fragment_fromstring(clean_html or "<p></p>", create_parent="div")
    plain_text = "\n".join(
        line.strip()
        for line in reparsed.text_content().replace("\r", "").split("\n")
        if line.strip()
    )
    image_urls = list(dict.fromkeys(reparsed.xpath(".//img/@src")))
    localized = sum(url in localized_asset_urls.values() for url in image_urls)
    external = len(image_urls) - localized
    return CleanResult(
        html=clean_html,
        plain_text=plain_text,
        image_urls=image_urls,
        localized_images=localized,
        external_images=external,
        xiaoyuzhou_links_removed=removed_links,
        tracking_phrases_removed=removed_phrases,
    )


def set_channel_urls(channel: etree._Element, public_base_url: str) -> None:
    if not public_base_url:
        return
    base = public_base_url.rstrip("/")
    channel_link = channel.find("link")
    if channel_link is not None:
        channel_link.text = base + "/"
    for atom_link in channel.findall(ATOM_LINK_TAG):
        if atom_link.get("rel") == "self":
            atom_link.set("href", base + "/feed.xml")


def build_output_feed(
    source_root: etree._Element,
    source_episodes: list[FeedEpisode],
    page_records: dict[str, PageRecord],
    localized_asset_urls: dict[str, str],
    output_dir: Path,
    *,
    public_base_url: str,
    strip_item_links: bool,
) -> tuple[bytes, list[dict[str, Any]]]:
    root = copy.deepcopy(source_root)
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS feed does not contain a channel element")
    set_channel_urls(channel, public_base_url)

    item_by_guid = {
        normalize_space(item.findtext("guid") or ""): item
        for item in channel.findall("item")
    }
    episode_audit: list[dict[str, Any]] = []
    shownotes_dir = output_dir / "shownotes"

    for episode in source_episodes:
        item = item_by_guid[episode.guid]
        record = page_records[episode.guid]
        cleaned = clean_shownotes(
            record.shownotes_html,
            localized_asset_urls=localized_asset_urls,
        )
        if not cleaned.html:
            raise RuntimeError(f"Clean show notes are empty for {episode.guid}")

        description = item.find("description")
        if description is None:
            description = etree.SubElement(item, "description")
        description.text = etree.CDATA(cleaned.html.replace("]]>", "]]&gt;"))

        encoded = item.find(CONTENT_TAG)
        if encoded is None:
            encoded = etree.SubElement(item, CONTENT_TAG)
        encoded.text = etree.CDATA(cleaned.html.replace("]]>", "]]&gt;"))

        if strip_item_links:
            item_link = item.find("link")
            if item_link is not None:
                item.remove(item_link)

        atomic_write_text(shownotes_dir / f"{episode.guid}.html", cleaned.html + "\n")
        episode_audit.append(
            {
                "position": episode.position,
                "guid": episode.guid,
                "title": episode.title,
                "source_kind": record.source_kind,
                "source_url": record.source_url,
                "fetch_error": record.error,
                "rss_description_chars": len(episode.description),
                "clean_html_chars": len(cleaned.html),
                "clean_text_chars": len(cleaned.plain_text),
                "image_references": len(cleaned.image_urls),
                "localized_images": cleaned.localized_images,
                "external_images": cleaned.external_images,
                "xiaoyuzhou_links_removed": cleaned.xiaoyuzhou_links_removed,
                "tracking_phrases_removed": cleaned.tracking_phrases_removed,
                "enclosure_url": episode.enclosure_url,
                "enclosure_length": episode.enclosure_length,
                "enclosure_type": episode.enclosure_type,
            }
        )

    result = etree.tostring(
        root,
        encoding="UTF-8",
        xml_declaration=True,
        pretty_print=True,
    )
    return result, episode_audit


def validate_output_feed(
    source_root: etree._Element,
    output_feed: bytes,
) -> dict[str, Any]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    output_root = etree.fromstring(output_feed, parser=parser)
    source_channel = source_root.find("channel")
    output_channel = output_root.find("channel")
    if source_channel is None or output_channel is None:
        raise RuntimeError("Source or output RSS has no channel")

    source_items = source_channel.findall("item")
    output_items = output_channel.findall("item")
    source_guids = [normalize_space(item.findtext("guid") or "") for item in source_items]
    output_guids = [normalize_space(item.findtext("guid") or "") for item in output_items]

    errors: list[str] = []
    if source_guids != output_guids:
        errors.append("Episode GUID sequence changed")
    if len(output_guids) != len(set(output_guids)):
        errors.append("Output feed contains duplicate GUIDs")

    source_by_guid = dict(zip(source_guids, source_items, strict=True))
    for item, guid in zip(output_items, output_guids, strict=True):
        source_item = source_by_guid[guid]
        source_enclosure = source_item.find("enclosure")
        output_enclosure = item.find("enclosure")
        source_attrs = dict(source_enclosure.attrib) if source_enclosure is not None else {}
        output_attrs = dict(output_enclosure.attrib) if output_enclosure is not None else {}
        if source_attrs != output_attrs:
            errors.append(f"{guid}: enclosure changed")

        source_pub_date = source_item.findtext("pubDate") or ""
        output_pub_date = item.findtext("pubDate") or ""
        if source_pub_date != output_pub_date:
            errors.append(f"{guid}: pubDate changed")

        description = item.findtext("description") or ""
        encoded = item.findtext(CONTENT_TAG) or ""
        if not description.strip():
            errors.append(f"{guid}: empty description")
        if description != encoded:
            errors.append(f"{guid}: description and content:encoded differ")
        if any(phrase in description for phrase in TRACKING_PHRASES):
            errors.append(f"{guid}: tracking phrase remains")
        if "xiaoyuzhoufm.com" in description:
            errors.append(f"{guid}: Xiaoyuzhou page link remains in show notes")

    return {
        "valid": not errors,
        "errors": errors,
        "source_items": len(source_items),
        "output_items": len(output_items),
        "unique_guids": len(set(output_guids)),
        "guids_preserved": source_guids == output_guids,
    }


def render_audit_markdown(audit: dict[str, Any]) -> str:
    validation = audit["validation"]
    totals = audit["totals"]
    lines = [
        "# 静态干净 RSS 审计报告",
        "",
        f"生成时间：{audit['generated_at']}",
        "",
        "## 结论",
        "",
        f"- XML 与保真检查：{'通过' if validation['valid'] else '失败'}",
        f"- 源 Feed 单集：{validation['source_items']} 期",
        f"- 输出 Feed 单集：{validation['output_items']} 期",
        f"- GUID 完整保留：{'是' if validation['guids_preserved'] else '否'}",
        f"- 公开页抓取成功：{totals['public_page_records']} 期",
        f"- RSS 回退：{totals['rss_fallback_records']} 期",
        f"- 图像引用：{totals['image_references']} 个",
        f"- 已本地化图像引用：{totals['localized_image_references']} 个",
        f"- 仍使用远程图像引用：{totals['external_image_references']} 个",
        f"- 移除小宇宙页面链接：{totals['xiaoyuzhou_links_removed']} 个",
        f"- 移除导流短语：{totals['tracking_phrases_removed']} 处",
        "",
        "## 文件",
        "",
        "- `feed.xml`：可发布的 RSS 2.0 Feed",
        "- `shownotes/*.html`：逐期清洗后的完整 HTML",
        "- `manifest.json`：逐期结构化审计数据",
        "- `asset-manifest.json`：图片本地化状态",
        "",
        "## 验证错误",
        "",
    ]
    if validation["errors"]:
        lines.extend(f"- {error}" for error in validation["errors"])
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "## 仍需注意",
            "",
            "- 当前 Feed 的音频 enclosure 仍指向小宇宙 CDN，这是本方案的预期取舍。",
            "- 未本地化的图片仍指向小宇宙图片 CDN；部署前可执行完整图片下载。",
            "- 不同播客客户端对内嵌图片和 HTML 标签的支持程度不同。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a clean static RSS feed with complete Xiaoyuzhou show notes."
    )
    parser.add_argument("--feed-url", default=DEFAULT_FEED_URL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--public-base-url", default=DEFAULT_PUBLIC_BASE_URL)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--refresh-pages", action="store_true")
    parser.add_argument("--download-images", action="store_true")
    parser.add_argument(
        "--image-episode-limit",
        type=int,
        default=0,
        help="Only localize images referenced by the first N episodes; 0 means all.",
    )
    parser.add_argument(
        "--keep-item-links",
        action="store_true",
        help="Keep source item links. By default Xiaoyuzhou item links are removed.",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    cache_dir = args.cache_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    raw_feed, feed_headers = request(
        args.feed_url,
        timeout=args.timeout,
        retries=args.retries,
    )
    source_root, episodes = parse_feed(raw_feed)
    guid_counts: dict[str, int] = {}
    for episode in episodes:
        guid_counts[episode.guid] = guid_counts.get(episode.guid, 0) + 1
    duplicate_guids = sorted(guid for guid, count in guid_counts.items() if count > 1)
    if duplicate_guids:
        raise RuntimeError(f"Source feed contains duplicate GUIDs: {duplicate_guids}")

    records = fetch_all_page_records(
        episodes,
        cache_dir,
        workers=args.workers,
        refresh=args.refresh_pages,
        timeout=args.timeout,
        retries=args.retries,
    )

    all_image_urls = list(
        dict.fromkeys(
            url
            for episode in episodes
            for url in records[episode.guid].image_urls
        )
    )
    selected_image_urls: list[str] = []
    if args.download_images:
        selected_episodes = episodes
        if args.image_episode_limit > 0:
            selected_episodes = episodes[: args.image_episode_limit]
        selected_image_urls = list(
            dict.fromkeys(
                url
                for episode in selected_episodes
                for url in records[episode.guid].image_urls
            )
        )

    assets_dir = output_dir / "assets"
    asset_results = (
        download_assets(
            selected_image_urls,
            assets_dir,
            workers=args.workers,
            timeout=args.timeout,
            retries=args.retries,
        )
        if selected_image_urls
        else {}
    )
    public_base = args.public_base_url.rstrip("/")
    localized_asset_urls = {
        url: f"{public_base}/assets/{result['filename']}"
        for url, result in asset_results.items()
        if public_base and result["status"] in {"downloaded", "cached"} and result["filename"]
    }

    feed_bytes, episode_audit = build_output_feed(
        source_root,
        episodes,
        records,
        localized_asset_urls,
        output_dir,
        public_base_url=args.public_base_url,
        strip_item_links=not args.keep_item_links,
    )
    feed_path = output_dir / "feed.xml"
    atomic_write_bytes(feed_path, feed_bytes)
    validation = validate_output_feed(source_root, feed_bytes)

    localized_unique = {
        url for url, result in asset_results.items() if result["status"] in {"downloaded", "cached"}
    }
    asset_manifest = {
        "schema_version": "1.0.0",
        "generated_at": utc_now(),
        "public_base_url": args.public_base_url,
        "unique_image_urls": len(all_image_urls),
        "localized_unique_images": len(localized_unique),
        "remaining_remote_unique_images": len(set(all_image_urls) - localized_unique),
        "assets": [
            asset_results.get(
                url,
                {
                    "original_url": url,
                    "filename": "",
                    "bytes": 0,
                    "content_type": "",
                    "status": "remote",
                    "error": "",
                },
            )
            for url in all_image_urls
        ],
    }
    write_json(output_dir / "asset-manifest.json", asset_manifest)

    totals = {
        "public_page_records": sum(
            record.source_kind in {"public-page", "page-cache"}
            for record in records.values()
        ),
        "rss_fallback_records": sum(
            record.source_kind == "rss-fallback" for record in records.values()
        ),
        "image_references": sum(item["image_references"] for item in episode_audit),
        "localized_image_references": sum(
            item["localized_images"] for item in episode_audit
        ),
        "external_image_references": sum(
            item["external_images"] for item in episode_audit
        ),
        "unique_image_urls": len(all_image_urls),
        "localized_unique_images": len(localized_unique),
        "xiaoyuzhou_links_removed": sum(
            item["xiaoyuzhou_links_removed"] for item in episode_audit
        ),
        "tracking_phrases_removed": sum(
            item["tracking_phrases_removed"] for item in episode_audit
        ),
    }
    audit = {
        "schema_version": "1.0.0",
        "generated_at": utc_now(),
        "feed_url": args.feed_url,
        "source_content_type": feed_headers.get("content-type", ""),
        "output_feed": str(feed_path),
        "public_base_url": args.public_base_url,
        "validation": validation,
        "totals": totals,
        "episodes": episode_audit,
    }
    write_json(output_dir / "manifest.json", audit)
    atomic_write_text(output_dir / "audit.md", render_audit_markdown(audit))

    summary = {
        "feed": str(feed_path),
        "episodes": len(episodes),
        "public_pages": totals["public_page_records"],
        "rss_fallbacks": totals["rss_fallback_records"],
        "unique_images": totals["unique_image_urls"],
        "localized_images": totals["localized_unique_images"],
        "valid": validation["valid"],
        "errors": len(validation["errors"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if validation["valid"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
