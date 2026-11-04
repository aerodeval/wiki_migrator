"""Reads a hand-written list: `## Category`, `- Subcategory`, `  - [Article](url)`."""

import re
from urllib.parse import unquote, urlsplit

from frappe import _

from wiki_migrator.wiki_migrator.doctype.wiki_migration.http import get_origin
from wiki_migrator.wiki_migrator.doctype.wiki_migration.wiki_client import INDEX_LINE

TITLE_LENGTH = 140
HEADING = re.compile(r"^##\s+(?P<name>.+?)\s*$")
BULLET = re.compile(r"^(?P<indent>\s*)- (?P<text>.+?)\s*$")
# `/help/articles/<name>-<title slug>`: names never hold a hyphen
HELPDESK_ARTICLE = re.compile(r"/articles/(?P<name>[^/-]+)[^/]*/?$")


class CategoryList:
	def __init__(self, text: str, wiki_url: str, helpdesk_url: str):
		self.wiki_url, self.helpdesk_url = wiki_url, helpdesk_url
		self.categories, self.skipped, self.seen = [], [], set()
		self.category = self.group = None
		for line in (text or "").splitlines():
			self.read_line(line)

	def read_line(self, line: str):
		if heading := HEADING.match(line):
			self.category = self.group = self.new_group(heading["name"], self.categories)
		elif self.category and line.strip():
			self.read_entry(line)

	def read_entry(self, line: str):
		"""A line under a category; lines above the first one are the list's introduction."""
		bullet, link = BULLET.match(line), INDEX_LINE.match(line)
		if not bullet:
			self.skip(line.strip(), _("Not a category, subcategory or article"))
		elif link and link["url"]:
			self.group = self.group if bullet["indent"] else self.category
			self.add_page(link["label"].strip(), link["url"])
		elif bullet["indent"]:
			self.skip(bullet["text"], _("Subcategories can't have subcategories"))
		else:
			name = link["group"] if link else bullet["text"]
			self.group = self.new_group(name, self.category["subcategories"])

	def new_group(self, name: str, siblings: list[dict]) -> dict:
		group = {"name": name.strip(), "pages": [], "subcategories": []}
		siblings.append(group)
		return group

	def add_page(self, label: str, url: str):
		page = self.read_link(label[:TITLE_LENGTH], url)
		key = page and (page.get("route") or page["article"])
		if not page:
			self.skip(label, _("Not a page of this wiki or an article of this helpdesk"))
		elif key in self.seen:
			self.skip(label, _("Listed twice"))
		else:
			self.seen.add(key)
			self.group["pages"].append(page)

	def read_link(self, title: str, url: str) -> dict | None:
		origin, path = get_origin(url), urlsplit(url).path
		if origin == self.helpdesk_url and (article := HELPDESK_ARTICLE.search(path)):
			return {"title": title, "article": article["name"]}
		route = unquote(path).strip("/").removesuffix(".md")
		if origin == self.wiki_url and route:
			return {"title": title, "route": route}

	def skip(self, title: str, reason: str):
		self.skipped.append({"title": title, "reason": reason})

	@property
	def groups(self) -> list[dict]:
		return [group for category in self.categories for group in (category, *category["subcategories"])]

	@property
	def article_names(self) -> list[str]:
		return [d["article"] for group in self.groups for d in group["pages"] if d.get("article")]

	def remove_missing_articles(self, found: set[str]):
		"""Drop the helpdesk articles that don't exist."""
		for group in self.groups:
			for page in [d for d in group["pages"] if d.get("article") and d["article"] not in found]:
				group["pages"].remove(page)
				self.skip(page["title"], _("Not found on this helpdesk"))

	def get_plan(self) -> dict:
		"""The `targets` and `pages` of an import, heaviest first in each list."""
		targets, pages = {}, []
		for category_index, category in enumerate(self.categories):
			category_key = f"category-{category_index}"
			self.add_target(category, category_key, None, targets, pages)
			for index, subcategory in enumerate(category["subcategories"]):
				if subcategory["pages"]:
					targets.setdefault(category_key, {"name": category["name"], "parent": None})
				self.add_target(subcategory, f"{category_key}-{index}", category_key, targets, pages)
		return {"targets": targets, "pages": pages}

	def add_target(self, group: dict, key: str, parent: str | None, targets: dict, pages: list):
		count = len(group["pages"])
		if count:
			targets[key] = {"name": group["name"], "parent": parent}
		pages += [
			{**page, "target": key, "weight": count - index} for index, page in enumerate(group["pages"])
		]

	def as_dict(self) -> dict:
		return {
			"categories": self.categories,
			"skipped": self.skipped,
			"page_count": len(self.get_plan()["pages"]),
		}
