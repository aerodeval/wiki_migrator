"""Turns a wiki page's rendered HTML into HTML the article editor keeps.

The wiki renders callouts, video and PDF blocks, mermaid diagrams and task lists
as its own markup, and the article editor only knows plain rich text, so each
becomes its closest plain equivalent. Iframes would not survive the sanitiser on
save, so an embedded video becomes a link to it.
"""

import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from wiki_migrator.wiki_migrator.doctype.wiki_migration.wiki_client import get_origin

HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
FILE_PATHS = ("/files/", "/private/files/")
YOUTUBE_EMBED = re.compile(r"^https?://(?:www\.)?youtube(?:-nocookie)?\.com/embed/(?P<id>[\w-]+)")
UNCHANGED_LINKS = ("#", "mailto:", "tel:")
# the title YouTube's embed snippet gives every video says nothing about this one
GENERIC_IFRAME_TITLE = "YouTube video player"


class WikiPage:
	"""One page's converted HTML, and the wiki files it points at."""

	def __init__(self, html: str, base_url: str, route: str):
		self.base_url = base_url
		self.page_url = f"{base_url}/{route}"
		self.soup = BeautifulSoup(html or "", "html.parser")
		self.assets: set[str] = set()
		self.convert()

	def convert(self):
		self.convert_callouts()
		self.convert_media()
		self.convert_task_lists()
		self.shift_headings()
		self.resolve_urls()
		self.strip_styling()
		self.remove_empty_paragraphs()

	@property
	def html(self) -> str:
		return str(self.soup).strip()

	@property
	def is_empty(self) -> bool:
		return not self.soup.get_text(strip=True) and not self.soup.find(["img", "video"])

	def replace_urls(self, urls: dict[str, str]) -> None:
		"""Point the copied assets at their new home."""
		for tag in self.soup.find_all(["a", "img", "video"]):
			attribute = "href" if tag.name == "a" else "src"
			if tag.get(attribute) in urls:
				tag[attribute] = urls[tag[attribute]]

	def convert_callouts(self):
		for aside in self.soup.select("aside.callout"):
			title = aside.select_one(".callout-title")
			content = aside.select_one(".callout-content")
			blockquote = self.new_tag("blockquote")
			if title and title.get_text(strip=True):
				strong = self.new_tag("strong", title.get_text(strip=True))
				blockquote.append(self.wrap("p", strong))
			if content:
				blockquote.extend(list(content.contents))
			aside.replace_with(blockquote)

	def convert_media(self):
		for picture in self.soup.find_all("picture"):
			picture.replace_with(picture.find("img") or "")
		for block in self.soup.select('[data-type="video-block"]'):
			block.replace_with(self.new_tag("video", src=block.get("data-src"), controls=""))
		for block in self.soup.select(".wiki-pdf-embed"):
			name = block.select_one(".wiki-pdf-name")
			link = self.new_tag(
				"a",
				name.get_text(strip=True) if name else "Document.pdf",
				href=block.get("data-src"),
			)
			block.replace_with(self.wrap("p", link))
		for block in self.soup.select(".mermaid"):
			code = self.new_tag("code", block.get_text(), **{"class": "language-mermaid"})
			block.replace_with(self.wrap("pre", code))
		for iframe in self.soup.find_all("iframe"):
			self.convert_iframe(iframe)
		for svg in self.soup.find_all("svg"):
			svg.decompose()

	def convert_iframe(self, iframe: Tag):
		src = (iframe.get("src") or "").replace("\\_", "_")
		if match := YOUTUBE_EMBED.match(src):
			src = f"https://www.youtube.com/watch?v={match['id']}"
		title = iframe.get("title")
		if not title or title == GENERIC_IFRAME_TITLE:
			title = "Watch the video"
		link = self.new_tag("a", title, href=src)
		iframe.replace_with(link if iframe.parent.name == "p" else self.wrap("p", link))

	def convert_task_lists(self):
		# a tight list puts the checkbox in the item, a loose one in its paragraph
		for checkbox in self.soup.select('li > input[type="checkbox"], li > p > input[type="checkbox"]'):
			item = checkbox.find_parent("li")
			item.parent["data-type"] = "taskList"
			item["data-type"] = "taskItem"
			item["data-checked"] = "true" if checkbox.has_attr("checked") else "false"
			checkbox.decompose()
			self.wrap_inline_content(item)

	def wrap_inline_content(self, item: Tag):
		"""A task item holds paragraphs, so its leading text moves into one."""
		paragraph = self.new_tag("p")
		for child in list(item.contents):
			if isinstance(child, Tag) and child.name in ("p", "ul", "ol"):
				break
			paragraph.append(child.extract())
		if paragraph.get_text(strip=True) or paragraph.find(True):
			item.insert(0, paragraph)
		first = item.find("p")
		if first and isinstance(text := next(iter(first.contents), None), NavigableString):
			text.replace_with(text.lstrip())

	def shift_headings(self):
		"""The article title is the page's h1, so a body h1 steps down a level, and the rest with it."""
		if not self.soup.find("h1"):
			return
		for heading in self.soup.find_all(HEADINGS):
			heading.name = HEADINGS[min(HEADINGS.index(heading.name) + 1, 5)]

	def resolve_urls(self):
		for tag in self.soup.find_all(["img", "video"]):
			tag["src"] = self.resolve(tag.get("src"))
		for link in self.soup.find_all("a", href=True):
			self.resolve_link(link)

	def resolve_link(self, link: Tag):
		href = link["href"]
		if not href or href.startswith(UNCHANGED_LINKS):
			return
		link["href"] = self.resolve(href)
		parts = urlsplit(link["href"])
		if get_origin(link["href"]) == self.base_url and not parts.path.startswith(FILE_PATHS):
			if route := unquote(parts.path).strip("/"):
				link["data-wiki-route"] = route

	def resolve(self, url: str | None) -> str | None:
		"""`url` made absolute; a file on the wiki is noted to be copied over."""
		if not url:
			return url
		absolute = urljoin(self.page_url, url)
		if urlsplit(absolute).scheme not in ("http", "https"):
			return url
		if get_origin(absolute) != self.base_url:
			return url
		if urlsplit(absolute).path.startswith(FILE_PATHS):
			self.assets.add(absolute)
		return absolute

	def strip_styling(self):
		for tag in self.soup.find_all(True):
			tag.attrs.pop("style", None)
			classes = [c for c in tag.get("class", []) if tag.name == "code" and c.startswith("language-")]
			if classes:
				tag["class"] = classes
			else:
				tag.attrs.pop("class", None)

	def remove_empty_paragraphs(self):
		for paragraph in self.soup.find_all("p"):
			if not paragraph.get_text(strip=True) and not paragraph.find(["img", "video", "a"]):
				paragraph.decompose()

	def new_tag(self, name: str, text: str | None = None, **attrs) -> Tag:
		tag = self.soup.new_tag(name, attrs=attrs)
		if text:
			tag.string = text
		return tag

	def wrap(self, name: str, child: Tag) -> Tag:
		tag = self.new_tag(name)
		tag.append(child)
		return tag


def rewrite_wiki_links(html: str, articles: dict[str, str]) -> str | None:
	"""Point links at imported wiki pages to their articles; None when nothing changed."""
	soup = BeautifulSoup(html, "html.parser")
	changed = False
	for link in soup.find_all("a", attrs={"data-wiki-route": True}):
		article = articles.get(link["data-wiki-route"])
		if not article:
			continue
		fragment = urlsplit(link["href"]).fragment
		href = f"/help/articles/{article}" + (f"#{fragment}" if fragment else "")
		if link["href"] != href:
			link["href"] = href
			changed = True
	return str(soup) if changed else None
