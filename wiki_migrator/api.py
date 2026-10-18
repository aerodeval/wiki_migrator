"""Migrate a Frappe Wiki space into a Frappe Helpdesk knowledge base.

The app connects to both sites, picks a space, maps its groups to categories and
starts a background job (`WikiMigration.run`) that copies the pages over.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.utils import sbool

from wiki_migrator.wiki_migrator.doctype.wiki_migration.helpdesk_client import (
	HelpdeskClient,
)
from wiki_migrator.wiki_migrator.doctype.wiki_migration.http import normalize_url
from wiki_migrator.wiki_migrator.doctype.wiki_migration.wiki_client import WikiClient

ITEM_FIELDS = [
	"wiki_route",
	"title",
	"status",
	"message",
	"article",
	"category",
	"category_name",
	"parent_category_name",
]
MIGRATION_FIELDS = [
	"name",
	"wiki_url",
	"helpdesk_url",
	"space",
	"space_title",
	"status",
	"error",
	"article_status",
	"visibility",
	"on_existing",
]
COUNTED_STATUSES = {
	"imported": "Imported",
	"updated": "Updated",
	"unchanged": "Unchanged",
	"skipped": "Skipped",
	"failed": "Failed",
	"pending": "Pending",
}


@frappe.whitelist(methods=["POST"])
def connect(
	wiki_url: str,
	helpdesk_url: str,
	helpdesk_api_key: str,
	helpdesk_api_secret: str,
	wiki_api_key: str | None = None,
	wiki_api_secret: str | None = None,
) -> dict:
	frappe.only_for("System Manager")
	with _site("Wiki"):
		if bool(wiki_api_key) != bool(wiki_api_secret):
			frappe.throw(_("Enter both the API key and the API secret, or neither."))
		wiki = WikiClient(normalize_url(wiki_url), wiki_api_key, wiki_api_secret)
		wiki.resolve_base_url()
		spaces = wiki.get_spaces()
	with _site("Helpdesk"):
		if not (helpdesk_api_key and helpdesk_api_secret):
			frappe.throw(_("Enter the API key and the API secret."))
		helpdesk = HelpdeskClient(normalize_url(helpdesk_url), helpdesk_api_key, helpdesk_api_secret)
		helpdesk.resolve_base_url()
		supports_subcategories = helpdesk.validate()
	migration = _get_draft(wiki.base_url, helpdesk.base_url)
	migration.update(
		{
			"wiki_api_key": wiki_api_key,
			"wiki_api_secret": wiki_api_secret,
			"helpdesk_api_key": helpdesk_api_key,
			"helpdesk_api_secret": helpdesk_api_secret,
			"supports_subcategories": supports_subcategories,
		}
	)
	migration.save()
	return {
		"name": migration.name,
		"wiki_url": wiki.base_url,
		"helpdesk_url": helpdesk.base_url,
		"spaces": spaces,
		"space": _get_linked_space(wiki_url, spaces),
		"supports_subcategories": supports_subcategories,
	}


@frappe.whitelist()
def get_tree(name: str, space: str) -> dict:
	migration = _get_migration(name)
	wiki = migration.get_wiki_client()
	space_row = next((d for d in wiki.get_spaces() if d["id"] == space), None)
	if not space_row:
		frappe.throw(_("Space {0} not found in the wiki.").format(space))
	nodes = wiki.get_tree(space_row)
	helpdesk = migration.get_helpdesk_client()
	imported = migration.get_imported_articles(helpdesk, migration.get_categories(helpdesk))
	_mark_imported(nodes, imported)
	migration.db_set({"space": space, "space_title": space_row["title"]})
	return {"space": space_row, "nodes": nodes}


@frappe.whitelist()
def get_categories(name: str) -> list[dict]:
	migration = _get_migration(name, "read")
	return list(migration.get_categories(migration.get_helpdesk_client()).values())


@frappe.whitelist(methods=["POST"])
def start_import(name: str, plan: dict | str) -> dict:
	_get_migration(name).start(frappe.parse_json(plan))
	return get_import(name)


@frappe.whitelist(methods=["POST"])
def retry_failed(name: str) -> dict:
	_get_migration(name).retry()
	return get_import(name)


@frappe.whitelist(methods=["POST"])
def sync_now(name: str) -> dict:
	_get_migration(name).sync()
	return get_import(name)


@frappe.whitelist(methods=["POST"])
def set_keep_in_sync(name: str, keep_in_sync: bool) -> dict:
	# not a save: that would write back the items while a run updates them
	_get_migration(name).db_set("keep_in_sync", int(sbool(keep_in_sync)))
	return get_import(name)


@frappe.whitelist()
def get_import(name: str) -> dict:
	migration = _get_migration(name, "read")
	article_url = f"{migration.helpdesk_url}/helpdesk/kb/articles/"
	items = [
		{
			**{field: d.get(field) for field in ITEM_FIELDS},
			"article_url": article_url + d.article if d.article else None,
		}
		for d in migration.items
	]
	return {
		**{field: migration.get(field) for field in MIGRATION_FIELDS},
		"supports_subcategories": bool(migration.supports_subcategories),
		"keep_in_sync": bool(migration.keep_in_sync),
		"running": migration.is_running,
		"stale": migration.is_stale,
		"can_retry": migration.can_retry,
		"counts": _get_counts([d.status for d in migration.items]),
		"items": items,
	}


@frappe.whitelist()
def get_recent(limit: int = 5) -> list[dict]:
	frappe.only_for("System Manager")
	migrations = frappe.get_all(
		"Wiki Migration",
		filters={"status": ("!=", "Draft")},
		fields=[
			"name",
			"space_title",
			"wiki_url",
			"helpdesk_url",
			"status",
			"modified",
		],
		order_by="modified desc",
		limit=limit,
	)
	if not migrations:
		return []
	statuses = frappe.get_all(
		"Wiki Migration Item",
		filters={"parent": ("in", [d.name for d in migrations])},
		fields=["parent", "status"],
	)
	for migration in migrations:
		migration["counts"] = _get_counts([d.status for d in statuses if d.parent == migration.name])
	return migrations


@contextmanager
def _site(label: str) -> Iterator[None]:
	"""Name the site an error came from: "Wiki: …" or "Helpdesk: …"."""
	try:
		yield
	except frappe.ValidationError as e:
		frappe.clear_messages()
		frappe.throw(_("{0}: {1}").format(_(label), e), exc=type(e))


def _get_migration(name: str, permission: str = "write"):
	frappe.only_for("System Manager")
	migration = frappe.get_doc("Wiki Migration", name)
	migration.check_permission(permission)
	return migration


def _get_draft(wiki_url: str, helpdesk_url: str):
	"""The caller's unstarted migration between these sites, so reconnecting doesn't pile up drafts."""
	filters = {
		"wiki_url": wiki_url,
		"helpdesk_url": helpdesk_url,
		"owner": frappe.session.user,
		"status": "Draft",
	}
	if name := frappe.db.get_value("Wiki Migration", filters, "name", order_by="creation desc"):
		return frappe.get_doc("Wiki Migration", name)
	return frappe.new_doc("Wiki Migration", wiki_url=wiki_url, helpdesk_url=helpdesk_url)


def _get_linked_space(wiki_url: str, spaces: list[dict]) -> str | None:
	"""The space a pasted page URL sits in, longest route first so nested routes win."""
	path = urlsplit(wiki_url if "://" in wiki_url else f"https://{wiki_url}").path.strip("/")
	for space in sorted(spaces, key=lambda d: len(d["route"] or ""), reverse=True):
		route = (space["route"] or "").strip("/")
		if route and (path == route or path.startswith(f"{route}/")):
			return space["id"]


def _mark_imported(nodes: list[dict], imported: dict) -> None:
	for node in nodes:
		row = imported.get(node["route"]) if node["route"] else None
		# a page whose article a stopped run never finished isn't imported yet
		node["imported"] = row if row and row.finished else None
		_mark_imported(node["children"], imported)


def _get_counts(statuses: list[str]) -> dict[str, int]:
	return {
		"total": len(statuses),
		**{key: statuses.count(status) for key, status in COUNTED_STATUSES.items()},
	}
