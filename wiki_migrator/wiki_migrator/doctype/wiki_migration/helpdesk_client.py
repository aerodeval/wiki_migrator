"""Writes to a Frappe Helpdesk site over its REST API, as the user of an API key.

Every request stays on the helpdesk's own origin: a redirect is refused, except for
the connect probe that finds the site's canonical address before any key is sent.
"""

import json
from urllib.parse import quote

import frappe
import requests
from frappe import _
from frappe.utils import strip_html

from wiki_migrator.wiki_migrator.doctype.wiki_migration.wiki_client import (
	TIMEOUT,
	get_origin,
	new_session,
)

ARTICLE = "HD Article"
CATEGORY = "HD Article Category"
# names per `in` filter, so a large wiki's lookups stay within URL limits
BATCH_SIZE = 100


class HelpdeskClient:
	def __init__(self, base_url: str, api_key: str, api_secret: str):
		self.base_url = base_url
		self.auth = f"token {api_key}:{api_secret}"
		self.session = new_session()

	def resolve_base_url(self) -> str:
		"""Check the site runs Frappe, following a redirect to its canonical origin."""
		response = self.send("GET", self.base_url + "/api/method/ping", probe=True)
		if not response.ok or _get_json(response).get("message") != "pong":
			frappe.throw(_("{0} is not a Frappe site.").format(self.base_url))
		self.base_url = get_origin(response.url)
		return self.base_url

	def validate(self) -> bool:
		"""Check the key's user can import articles; whether categories nest."""
		self.get("/api/method/frappe.auth.get_logged_user")
		if self.can(ARTICLE, "read") is None:
			frappe.throw(_("{0} is not a Frappe Helpdesk site.").format(self.base_url))
		for doctype in (ARTICLE, CATEGORY):
			if not self.can(doctype, "create"):
				frappe.throw(
					_(
						"The API key's user can't create {0} records. Give them the Agent Manager role."
					).format(doctype)
				)
		return self.supports_subcategories()

	def can(self, doctype: str, permission: str) -> bool | None:
		"""Whether the user has `permission` on `doctype`; None when it doesn't exist."""
		response = self.request(
			"GET",
			"/api/method/frappe.client.has_permission",
			check=False,
			params={"doctype": doctype, "docname": "", "perm_type": permission},
		)
		if response.status_code == 404:
			return None
		self.check(response)
		return response.json()["message"]["has_permission"]

	def supports_subcategories(self) -> bool:
		meta = self.get("/api/method/frappe.desk.form.load.getdoctype", params={"doctype": CATEGORY})["docs"][
			0
		]
		return any(d["fieldname"] == "parent_category" for d in meta["fields"])

	def get_categories(self, with_parent: bool = True) -> list[dict]:
		fields = ["name", "category_name"] + (["parent_category"] if with_parent else [])
		rows = self.get_list(CATEGORY, fields)
		return [{"parent_category": None, **d} for d in rows]

	def get_articles(self, names: list[str], fields: list[str], *filters) -> list[dict]:
		names = sorted(set(names))
		rows = []
		for start in range(0, len(names), BATCH_SIZE):
			batch = names[start : start + BATCH_SIZE]
			rows += self.get_list(ARTICLE, fields, ["name", "in", batch], *filters)
		return rows

	def has_articles(self, category: str) -> bool:
		return bool(self.get_list(ARTICLE, ["name"], ["category", "=", category], limit=1))

	def get_file_urls(self, article: str) -> dict[str, str]:
		"""The files already on `article`, by content hash."""
		rows = self.get_list(
			"File",
			["file_url", "content_hash"],
			["attached_to_doctype", "=", ARTICLE],
			["attached_to_name", "=", article],
		)
		return {d["content_hash"]: d["file_url"] for d in rows if d["content_hash"]}

	def get_list(self, doctype: str, fields: list[str], *filters, limit: int = 0) -> list:
		return self.get(
			"/api/method/frappe.client.get_list",
			params={
				"doctype": doctype,
				"fields": json.dumps(fields),
				"filters": json.dumps(filters),
				"limit_page_length": limit,
			},
		)["message"]

	def insert(self, doctype: str, values: dict) -> str:
		return self.request("POST", _resource(doctype), json=values).json()["data"]["name"]

	def update(self, doctype: str, name: str, values: dict) -> None:
		self.request("PUT", _resource(doctype, name), json=values)

	def delete(self, doctype: str, name: str) -> None:
		self.request("DELETE", _resource(doctype, name))

	def upload_file(self, article: str, file_name: str, content: bytes) -> dict:
		"""A public file attached to `article`: its `name` and `file_url`."""
		return self.request(
			"POST",
			"/api/method/upload_file",
			files={"file": (file_name, content)},
			data={"doctype": ARTICLE, "docname": article, "is_private": 0},
		).json()["message"]

	def get(self, path: str, **kwargs) -> dict:
		return self.request("GET", path, **kwargs).json()

	def request(self, method: str, path: str, check: bool = True, **kwargs) -> requests.Response:
		response = self.send(method, self.base_url + path, **kwargs)
		if check:
			self.check(response)
		return response

	def send(self, method: str, url: str, probe: bool = False, **kwargs) -> requests.Response:
		# the probe may follow a redirect anywhere, so it carries no key
		headers = {} if probe else {"Authorization": self.auth}
		try:
			return self.session.request(
				method,
				url,
				headers=headers,
				timeout=TIMEOUT,
				allow_redirects=probe,
				**kwargs,
			)
		except requests.exceptions.SSLError:
			frappe.throw(_("Couldn't verify the SSL certificate of {0}.").format(self.base_url))
		except requests.exceptions.Timeout:
			frappe.throw(_("{0} took too long to respond. Try again later.").format(self.base_url))
		except requests.exceptions.RequestException:
			frappe.throw(_("Couldn't reach {0}. Check the URL and try again.").format(self.base_url))

	def check(self, response: requests.Response) -> None:
		if response.status_code == 401:
			frappe.throw(_("The helpdesk didn't accept the API key and secret."))
		if response.is_redirect:
			frappe.throw(_("The helpdesk redirected {0} to another address.").format(response.url))
		if not response.ok:
			frappe.throw(
				get_server_message(response)
				or _("The helpdesk answered {0} for {1}.").format(response.status_code, response.url)
			)


def get_server_message(response: requests.Response) -> str | None:
	"""The message a Frappe site put in its error response, as plain text."""
	data = _get_json(response)
	if messages := data.get("_server_messages"):
		message = json.loads(json.loads(messages)[-1]).get("message")
		return strip_html(message) if message else None
	if exception := data.get("exception"):
		return exception.split(": ", 1)[-1]


def _get_json(response: requests.Response) -> dict:
	try:
		data = response.json()
	except ValueError:
		return {}
	return data if isinstance(data, dict) else {}


def _resource(doctype: str, name: str | None = None) -> str:
	path = f"/api/resource/{quote(doctype)}"
	return f"{path}/{quote(name, safe='')}" if name else path
