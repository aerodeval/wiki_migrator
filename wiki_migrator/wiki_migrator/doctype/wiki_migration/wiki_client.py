"""Reads a Frappe Wiki site, with or without an API key.

Without credentials the wiki's public `llms.txt` indexes give the spaces and their
trees; with an API key and secret the REST API does, and private spaces show too.
Page HTML comes from the same guest-allowed endpoint in both modes.
"""

import re
from urllib.parse import unquote, urljoin, urlsplit

import frappe
import requests
from frappe import _
from frappe.core.api.file import get_max_file_size

from wiki_migrator.wiki_migrator.doctype.wiki_migration.http import get_origin, new_session, send

PAGE_DATA_METHOD = "wiki.frappe_wiki.doctype.wiki_document.wiki_document.get_page_data"
TREE_METHOD = "wiki.api.wiki_space.get_wiki_tree"
# a route no wiki has: the wiki answers "Page not found", any other site something else
PROBE_ROUTE = "wiki-migrator-probe"
DOWNLOAD_CHUNK = 64 * 1024
MAX_REDIRECTS = 5
# `  - [Label](url)` or `- **Label**`: indent, then a link or a bold group label
INDEX_LINE = re.compile(
	r"^(?P<indent>\s*)- (?:\[(?P<label>.*)\]\((?P<url>[^)]*)\)|\*\*(?P<group>.*)\*\*)\s*$"
)


class WikiClient:
	def __init__(self, base_url: str, api_key: str | None = None, api_secret: str | None = None):
		self.base_url = base_url
		self.auth = f"token {api_key}:{api_secret}" if api_key and api_secret else None
		self.session = new_session()

	def resolve_base_url(self) -> str:
		"""Check the site runs Frappe Wiki, following a redirect to its canonical origin."""
		response = self.get(
			f"/api/method/{PAGE_DATA_METHOD}",
			params={"route": PROBE_ROUTE},
			check=False,
			probe=True,
		)
		if not _is_page_not_found(response):
			frappe.throw(_("{0} is not a Frappe Wiki site.").format(self.base_url))
		self.base_url = get_origin(response.url)
		return self.base_url

	def get_spaces(self) -> list[dict]:
		spaces = self.get_api_spaces() if self.auth else self.get_public_spaces()
		if not spaces:
			frappe.throw(_("This wiki has no spaces to import."))
		return spaces

	def get_api_spaces(self) -> list[dict]:
		rows = self.get(
			"/api/resource/Wiki Space",
			params={
				"fields": '["name","space_name","route","root_group"]',
				"limit_page_length": 0,
			},
		).json()["data"]
		return [
			{
				"id": row["name"],
				"title": row["space_name"] or row["name"],
				"route": row["route"],
			}
			for row in rows
			if row.get("root_group")
		]

	def get_public_spaces(self) -> list[dict]:
		response = self.get("/llms.txt", check=False)
		if not response.ok:
			frappe.throw(
				_("This wiki doesn't publish a public index. Add an API key and secret to import from it.")
			)
		spaces = []
		for d in parse_index(response.text):
			route = urlsplit(d["url"] or "").path.strip("/").removesuffix("llms.txt").strip("/")
			if route:
				spaces.append({"id": route, "title": d["title"], "route": route})
		return spaces

	def get_tree(self, space: dict) -> list[dict]:
		if self.auth:
			tree = self.get(f"/api/method/{TREE_METHOD}", params={"space_id": space["id"]}).json()
			nodes = [_api_node(d) for d in tree["message"]["children"]]
		else:
			nodes = _index_nodes(parse_index(self.get(f"/{space['route']}/llms.txt").text))
		return drop_duplicate_routes(nodes)

	def get_page(self, route: str) -> dict:
		"""A published page's `title` and `rendered_content`."""
		response = self.get(f"/api/method/{PAGE_DATA_METHOD}", params={"route": route}, check=False)
		if _is_page_not_found(response):
			frappe.throw(_("The page isn't published in the wiki anymore."))
		self.check(response)
		return response.json()["message"]

	def download(self, url: str) -> bytes:
		"""The file at `url`, refused once it grows past the site's upload limit."""
		limit = get_max_file_size()
		content = bytearray()
		with self.get(url, stream=True) as response:
			for chunk in response.iter_content(DOWNLOAD_CHUNK):
				content += chunk
				if len(content) > limit:
					frappe.throw(_("The file is larger than {0} MB.").format(limit // (1024 * 1024)))
		return bytes(content)

	def get(self, path: str, check: bool = True, probe: bool = False, **kwargs) -> requests.Response:
		"""GET `path` on the wiki. Only the connect `probe` may be redirected to another
		site; every other request stays on the wiki's origin, key or not."""
		url = path if path.startswith(("http://", "https://")) else self.base_url + path
		response = send(self.session, "GET", url, self.base_url, self.auth, probe, **kwargs)
		redirects = 0
		while response.is_redirect and not probe and redirects < MAX_REDIRECTS:
			url = urljoin(url, response.headers["Location"])
			if get_origin(url) != self.base_url:
				frappe.throw(_("The wiki redirected {0} to another site.").format(response.url))
			response = send(self.session, "GET", url, self.base_url, self.auth, **kwargs)
			redirects += 1
		if check:
			self.check(response)
		return response

	def check(self, response: requests.Response) -> None:
		if response.status_code in (401, 403):
			if self.auth:
				frappe.throw(_("The wiki didn't accept the API key and secret."))
			frappe.throw(_("The wiki denied access to {0}.").format(response.url))
		if not response.ok or response.is_redirect:
			frappe.throw(_("The wiki answered {0} for {1}.").format(response.status_code, response.url))


def parse_index(text: str) -> list[dict]:
	"""The nested list of an llms.txt as `{"title", "url", "children"}` nodes."""
	root = {"children": []}
	stack = [(-1, root)]
	for line in text.splitlines():
		match = INDEX_LINE.match(line)
		if not match:
			continue
		node = {
			"title": match["label"] if match["group"] is None else match["group"],
			"url": match["url"],
			"children": [],
		}
		depth = len(match["indent"].expandtabs(2))
		while stack[-1][0] >= depth:
			stack.pop()
		stack[-1][1]["children"].append(node)
		stack.append((depth, node))
	return root["children"]


def drop_duplicate_routes(nodes: list[dict], seen: set | None = None) -> list[dict]:
	"""A page listed twice is imported once, where it first appears."""
	seen = set() if seen is None else seen
	kept = []
	for node in nodes:
		if node["is_group"]:
			node["children"] = drop_duplicate_routes(node["children"], seen)
		elif node["route"] in seen:
			continue
		else:
			seen.add(node["route"])
		kept.append(node)
	return kept


def _index_nodes(items: list[dict], parent_key: str | None = None, keys: set | None = None) -> list[dict]:
	"""llms.txt items as tree nodes: an item with children is a group, its own link the
	group's landing page, which the children already list."""
	keys = set() if keys is None else keys
	nodes = []
	for d in items:
		if d["children"]:
			key = _unique_key(f"{parent_key or 'group:'}{'/' if parent_key else ''}{d['title']}", keys)
			children = _index_nodes(d["children"], key, keys)
			nodes.append(_node(key, d["title"], None, True, children))
		elif d["url"]:
			route = unquote(urlsplit(d["url"]).path).strip("/").removesuffix(".md")
			nodes.append(_node(route, d["title"], route, True))
	return nodes


def _unique_key(key: str, keys: set) -> str:
	"""Sibling groups can share a title; the second becomes `key#2`."""
	unique, count = key, 1
	while unique in keys:
		count += 1
		unique = f"{key}#{count}"
	keys.add(unique)
	return unique


def _api_node(d: dict) -> dict:
	if d["is_group"]:
		children = [_api_node(child) for child in d["children"]]
		return _node(d["name"], d["title"], None, bool(d["is_published"]), children)
	return _node(d["route"], d["title"], d["route"], bool(d["is_published"]))


def _node(key: str, title: str, route: str | None, published: bool, children=None) -> dict:
	return {
		"key": key,
		"title": title,
		"is_group": children is not None,
		"route": route,
		"published": published,
		"children": children or [],
	}


def _is_page_not_found(response: requests.Response) -> bool:
	try:
		return response.status_code == 404 and response.json().get("exc_type") == "DoesNotExistError"
	except ValueError:
		return False
