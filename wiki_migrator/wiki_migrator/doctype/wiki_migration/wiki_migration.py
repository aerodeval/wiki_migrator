# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt

from urllib.parse import unquote, urlsplit

import frappe
import requests
from frappe import _
from frappe.core.doctype.file.utils import get_content_hash
from frappe.model.document import Document
from frappe.query_builder import DocType, Order
from frappe.utils.background_jobs import is_job_enqueued
from rq.timeouts import JobTimeoutException

from wiki_migrator.wiki_migrator.doctype.wiki_migration.helpdesk_client import (
	ARTICLE,
	CATEGORY,
	HelpdeskClient,
)
from wiki_migrator.wiki_migrator.doctype.wiki_migration.wiki_client import WikiClient
from wiki_migrator.wiki_migrator.doctype.wiki_migration.wiki_html import (
	WikiPage,
	rewrite_wiki_links,
)

GENERAL = "General"
NAME_LENGTH = 140
JOB_TIMEOUT = 4 * 60 * 60
ACTIVE_STATUSES = ("Queued", "Running")


class WikiMigration(Document):
	@property
	def job_id(self) -> str:
		return f"wiki-migration-{self.name}"

	def get_wiki_client(self) -> WikiClient:
		secret = self.get_password("wiki_api_secret", raise_exception=False) if self.wiki_api_key else None
		return WikiClient(self.wiki_url, self.wiki_api_key, secret)

	def get_helpdesk_client(self) -> HelpdeskClient:
		secret = self.get_password("helpdesk_api_secret", raise_exception=False)
		return HelpdeskClient(self.helpdesk_url, self.helpdesk_api_key, secret)

	def get_categories(self, helpdesk: HelpdeskClient) -> dict[str, dict]:
		"""The helpdesk's categories by name."""
		rows = helpdesk.get_categories(with_parent=self.supports_subcategories)
		return {d["name"]: d for d in rows}

	@property
	def is_stale(self) -> bool:
		"""Queued or running on paper, but the worker that had the job is gone."""
		return self.status in ACTIVE_STATUSES and not is_job_enqueued(self.job_id)

	def start(self, plan: dict):
		# read before the lock, so no row stays locked on a network call
		categories = self.get_categories(self.get_helpdesk_client())
		# locked, so a second tab starting the same draft waits and then sees it started
		status = frappe.db.get_value("Wiki Migration", self.name, "status", for_update=True)
		if status != "Draft":
			frappe.throw(_("This migration has already started."))
		self.validate_plan(plan, categories)
		self.update(
			{
				"plan": frappe.as_json(plan),
				"article_status": plan.get("article_status") or self.article_status,
				"visibility": plan.get("visibility") or self.visibility,
				"on_existing": plan.get("on_existing") or self.on_existing,
				"status": "Queued",
				"error": None,
			}
		)
		self.set(
			"items",
			[
				{
					"wiki_route": d["route"],
					"title": d.get("title") or d["route"],
					"target": d["target"],
					"status": "Pending",
				}
				for d in plan["pages"]
			],
		)
		self.save()
		self.enqueue()

	def retry(self):
		if self.status in ACTIVE_STATUSES and not self.is_stale:
			frappe.throw(_("This migration is already running."))
		items = [d for d in self.items if d.status in ("Failed", "Pending")]
		if not items:
			frappe.throw(_("There are no failed pages to retry."))
		for d in items:
			d.update({"status": "Pending", "message": None})
		self.update({"status": "Queued", "error": None})
		self.save()
		self.enqueue()

	def enqueue(self):
		frappe.enqueue(
			run_migration,
			queue="long",
			timeout=JOB_TIMEOUT,
			job_id=self.job_id,
			deduplicate=True,
			enqueue_after_commit=True,
			name=self.name,
		)

	def validate_plan(self, plan: dict, categories: dict[str, dict]):
		pages = plan.get("pages") or []
		targets = plan.get("targets") or {}
		if not pages:
			frappe.throw(_("Pick at least one page to import."))
		routes = [d.get("route") for d in pages]
		if not all(routes):
			frappe.throw(_("Every page needs its wiki route."))
		if len(set(routes)) != len(routes):
			frappe.throw(_("A page can be imported only once."))
		for d in pages:
			if d.get("target") not in targets:
				frappe.throw(_("Pick a category for {0}.").format(d.get("title") or d.get("route")))
		for target in targets.values():
			self.validate_target(target, targets, categories)

	def validate_target(self, target: dict, targets: dict, categories: dict):
		if target.get("category"):
			if target["category"] not in categories:
				frappe.throw(_("Category {0} not found.").format(target["category"]))
			return
		name = (target.get("name") or "").strip()
		if not name:
			frappe.throw(_("A new category needs a name."))
		if len(name) > NAME_LENGTH:
			frappe.throw(_("Category names can be at most {0} characters.").format(NAME_LENGTH))
		if target.get("parent"):
			self.validate_parent(name, targets.get(target["parent"]), categories)

	def validate_parent(self, name: str, parent: dict | None, categories: dict):
		if not self.supports_subcategories:
			frappe.throw(
				_(
					"This helpdesk doesn't support subcategories, so {0} can't go under another category."
				).format(name)
			)
		if not parent:
			frappe.throw(_("The parent category of {0} isn't in the plan.").format(name))
		if name.lower() == GENERAL.lower():
			frappe.throw(_("General can't be a subcategory."))
		parent_name, grandparent = parent.get("name"), parent.get("parent")
		if parent.get("category"):
			row = categories.get(parent["category"]) or {}
			parent_name, grandparent = row.get("category_name"), row.get("parent_category")
		if grandparent:
			frappe.throw(
				_("{0} can't go under {1}: a subcategory can't have subcategories.").format(name, parent_name)
			)
		if (parent_name or "").strip().lower() == GENERAL.lower():
			frappe.throw(_("General can't have subcategories."))

	def run(self):
		self.created_categories = []
		self.db_set({"status": "Running", "error": None}, commit=True)
		try:
			self.migrate()
		except Exception as e:
			# each page commits on its own, so only the step that broke is lost
			frappe.db.rollback()
			self.db_set({"status": "Failed", "error": get_error_message(e, self.name)})
		frappe.db.commit()

	def migrate(self):
		wiki, helpdesk = self.get_wiki_client(), self.get_helpdesk_client()
		# a site that can't be reached fails the run, not each page in turn
		wiki.resolve_base_url()
		self.categories = self.get_categories(helpdesk)
		targets = self.create_categories(helpdesk)
		self.import_items(wiki, helpdesk, targets)
		self.link_imported_articles(helpdesk)
		self.delete_empty_categories(helpdesk)
		failed = any(d.status == "Failed" for d in self.items)
		self.db_set("status", "Completed with Errors" if failed else "Completed")

	def create_categories(self, helpdesk: HelpdeskClient) -> dict[str, str]:
		"""Each plan target's category, made when the helpdesk doesn't have it yet."""
		targets = frappe.parse_json(self.plan)["targets"]
		categories = {}
		# parents first: their subcategories link to them
		for key, target in sorted(targets.items(), key=lambda row: bool(row[1].get("parent"))):
			categories[key] = target.get("category") or self.get_category(
				helpdesk, target["name"].strip(), categories.get(target.get("parent"))
			)
		return categories

	def get_category(self, helpdesk: HelpdeskClient, category_name: str, parent: str | None) -> str:
		"""The category named so under `parent`; a top-level General is the helpdesk's own."""
		for name, d in self.categories.items():
			if (d["category_name"] or "").lower() == category_name.lower() and d["parent_category"] == parent:
				return name
		values = {"category_name": category_name}
		if parent:
			values["parent_category"] = parent
		name = helpdesk.insert(CATEGORY, values)
		self.categories[name] = {"name": name, "parent_category": parent, **values}
		self.created_categories.append(name)
		return name

	def import_items(self, wiki: WikiClient, helpdesk: HelpdeskClient, targets: dict[str, str]):
		imported = self.get_imported_articles(helpdesk, self.categories)
		for d in self.items:
			if d.status != "Pending":
				continue
			self.new_article, self.new_files = None, []
			try:
				d.status, d.message = self.import_page(wiki, helpdesk, d, targets[d.target], imported)
			except JobTimeoutException:
				self.undo_page(helpdesk)
				raise
			except Exception as e:
				message = get_error_message(e, d.wiki_route)
				d.status, d.message = "Failed", message + self.undo_page(helpdesk)
				d.article = d.category = None
			d.category_name, d.parent_category_name = get_category_names(self.categories, d.category)
			frappe.clear_messages()
			frappe.db.set_value(
				"Wiki Migration Item",
				d.name,
				{
					field: d.get(field)
					for field in (
						"status",
						"message",
						"article",
						"category",
						"category_name",
						"parent_category_name",
					)
				},
			)
			frappe.db.commit()

	def import_page(
		self,
		wiki: WikiClient,
		helpdesk: HelpdeskClient,
		d,
		category: str,
		imported: dict,
	) -> tuple:
		"""Copy one wiki page into an article; the item's new status and message."""
		d.article = d.category = None
		existing = imported.get(d.wiki_route)
		if existing and self.on_existing == "Skip":
			d.article, d.category = existing.article, existing.category
			return "Skipped", None
		page = WikiPage(wiki.get_page_html(d.wiki_route), wiki.base_url, d.wiki_route)
		if page.is_empty:
			return "Skipped", _("The page is empty in the wiki")
		title = d.title[:NAME_LENGTH]
		if existing:
			article, category = existing.article, existing.category
		else:
			# a draft without a category until it's complete: the helpdesk refuses to
			# delete a category's last article, which undoing a failed page would be
			article = self.new_article = helpdesk.insert(ARTICLE, {"title": title, "status": "Draft"})
		warnings = self.copy_assets(wiki, helpdesk, page, article, bool(existing))
		values = {"title": title, "content": page.html}
		if not existing:
			values.update(category=category, status=self.article_status, visibility=self.visibility)
		helpdesk.update(ARTICLE, article, values)
		d.article, d.category = article, category
		return ("Updated" if existing else "Imported"), "\n".join(warnings) or None

	def copy_assets(
		self,
		wiki: WikiClient,
		helpdesk: HelpdeskClient,
		page: WikiPage,
		article: str,
		existing: bool,
	) -> list[str]:
		"""Copy the wiki files a page uses onto its article; a warning for each that failed."""
		# a re-import brings the same files again; the article keeps one copy of each
		file_urls = helpdesk.get_file_urls(article) if existing else {}
		urls, warnings = {}, []
		for url in sorted(page.assets):
			try:
				content = wiki.download(url)
				content_hash = get_content_hash(content)
				if content_hash not in file_urls:
					file_urls[content_hash] = self.upload_file(helpdesk, article, url, content)
				urls[url] = file_urls[content_hash]
			# a stream can break midway, past the client's own error handling
			except (frappe.ValidationError, requests.RequestException) as e:
				warnings.append(_("Couldn't copy {0}: {1}").format(url, e))
		page.replace_urls(urls)
		return warnings

	def upload_file(self, helpdesk: HelpdeskClient, article: str, url: str, content: bytes) -> str:
		file_name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
		file = helpdesk.upload_file(article, file_name, content)
		self.new_files.append(file["name"])
		return file["file_url"]

	def undo_page(self, helpdesk: HelpdeskClient) -> str:
		"""The helpdesk can't roll back, so what a failed page made there is deleted.
		A note for the item's message when even that fails."""
		try:
			for file in self.new_files:
				helpdesk.delete("File", file)
			if self.new_article:
				helpdesk.delete(ARTICLE, self.new_article)
		except frappe.ValidationError as e:
			return "\n" + _("Couldn't remove what it made on the helpdesk: {0}").format(e)
		return ""

	def link_imported_articles(self, helpdesk: HelpdeskClient):
		"""Point links between pages of the wiki at their articles, now that they all exist."""
		routes = {
			route: row.article for route, row in self.get_imported_articles(helpdesk, self.categories).items()
		}
		articles = helpdesk.get_articles(
			list(routes.values()),
			["name", "content"],
			["content", "like", "%data-wiki-route%"],
		)
		for article in articles:
			if content := rewrite_wiki_links(article["content"], routes):
				helpdesk.update(ARTICLE, article["name"], {"content": content})

	def delete_empty_categories(self, helpdesk: HelpdeskClient):
		# subcategories were made after their parents, so they go first
		for category in reversed(self.created_categories):
			has_subcategories = any(d["parent_category"] == category for d in self.categories.values())
			if has_subcategories or helpdesk.has_articles(category):
				continue
			helpdesk.delete(CATEGORY, category)
			del self.categories[category]

	def get_imported_articles(
		self, helpdesk: HelpdeskClient, categories: dict[str, dict]
	) -> dict[str, frappe._dict]:
		"""The article each page of this wiki was last migrated to on this helpdesk,
		when it's still there."""
		item = DocType("Wiki Migration Item")
		migration = DocType("Wiki Migration")
		rows = (
			frappe.qb.from_(item)
			.join(migration)
			.on(migration.name == item.parent)
			.select(item.wiki_route, item.article)
			.where(migration.wiki_url == self.wiki_url)
			.where(migration.helpdesk_url == self.helpdesk_url)
			.where(item.article.isnotnull() & (item.article != ""))
			.orderby(item.modified, order=Order.desc)
			.run(as_dict=True)
		)
		articles = {
			d["name"]: d
			for d in helpdesk.get_articles([row.article for row in rows], ["name", "title", "category"])
		}
		imported = {}
		for row in rows:
			if row.wiki_route in imported or row.article not in articles:
				continue
			article = articles[row.article]
			category_name, parent_category_name = get_category_names(categories, article["category"])
			imported[row.wiki_route] = frappe._dict(
				article=row.article,
				title=article["title"],
				category=article["category"],
				category_name=category_name,
				parent_category_name=parent_category_name,
			)
		return imported


def run_migration(name: str):
	frappe.get_doc("Wiki Migration", name).run()


def get_category_names(categories: dict[str, dict], category: str | None) -> tuple[str | None, str | None]:
	"""A category's name and its parent's, from the helpdesk's categories."""
	row = categories.get(category) or {}
	parent = categories.get(row.get("parent_category")) or {}
	return row.get("category_name"), parent.get("category_name")


def get_error_message(exception: Exception, title: str) -> str:
	if isinstance(exception, frappe.ValidationError):
		return str(exception)
	frappe.log_error(title=_("Wiki migration failed: {0}").format(title))
	return _("Something went wrong. Check the Error Log for details.")
