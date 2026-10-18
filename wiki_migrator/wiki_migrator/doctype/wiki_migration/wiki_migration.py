# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt

from urllib.parse import unquote, urlsplit

import frappe
import requests
from frappe import _
from frappe.core.doctype.file.utils import get_content_hash
from frappe.model.document import Document
from frappe.query_builder import DocType, Order
from frappe.utils import now_datetime, time_diff_in_seconds
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
RETRY_STATUSES = ("Failed", "Pending")
# enqueue_after_commit adds a start's job only after its commit; until then it's this new
ENQUEUE_GRACE = 10
# chosen per run in the planner, which holds their defaults
OPTIONS = ("article_status", "visibility", "on_existing")


class WikiMigration(Document):
	@property
	def job_id(self) -> str:
		return get_job_id(self.name)

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
		return self.status in ACTIVE_STATUSES and not self.is_running

	@property
	def is_running(self) -> bool:
		return is_run_active(self)

	@property
	def can_retry(self) -> bool:
		"""Pages left to import, or a run that stopped after them: in its link or
		cleanup step, or with its worker. A rerun skips the pages already done."""
		if self.is_running:
			return False
		return self.status in ("Failed", *ACTIVE_STATUSES) or any(
			d.status in RETRY_STATUSES for d in self.items
		)

	def start(self, plan: dict):
		# read before the lock, so no row stays locked on a network call
		categories = self.get_categories(self.get_helpdesk_client())
		# a second tab starting the same draft waits for the lock, then sees it started
		if self.lock_for_run().status != "Draft":
			frappe.throw(_("This migration has already started."))
		self.validate_plan(plan, categories)
		self.update(
			{
				"targets": frappe.as_json(plan["targets"]),
				"article_status": plan["article_status"],
				"visibility": plan["visibility"],
				"on_existing": plan["on_existing"],
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
		refusal = None if self.can_retry else _("There are no failed pages to retry.")
		self.rerun(refusal, RETRY_STATUSES)

	def sync(self):
		"""Run every page again: the articles of the pages that changed in the wiki are
		updated, the rest are left as they are."""
		# a migration never goes back to Draft, so its loaded status will do
		refusal = _("Start the migration before syncing it.") if self.status == "Draft" else None
		# a sync brings the pages imported before up to date
		self.rerun(refusal, on_existing="Update")

	def rerun(self, refusal: str | None, statuses: tuple[str, ...] | None = None, **values):
		"""Queue the items in `statuses` (all when None) again, unless it runs or `refusal` says why not."""
		if is_run_active(self.lock_for_run()):
			frappe.throw(_("This migration is already running."))
		if refusal:
			frappe.throw(refusal)
		# not a save: its items' `modified` must stay that of the run that last wrote their article
		item = DocType("Wiki Migration Item")
		query = (
			frappe.qb.update(item)
			.set(item.status, "Pending")
			.set(item.message, None)
			.where(item.parent == self.name)
		)
		if statuses:
			query = query.where(item.status.isin(statuses))
		query.run()
		# bumps the migration's `modified`, which ENQUEUE_GRACE counts from
		self.db_set({"status": "Queued", "error": None, **values})
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

	def lock_for_run(self) -> frappe._dict:
		"""Lock this pair's migrations and refuse while another one runs: two runs between
		the same sites would each import the pages the other hasn't yet. This migration's
		row as the lock read it, which only a locking read sees up to date."""
		migration = DocType("Wiki Migration")
		rows = (
			frappe.qb.from_(migration)
			.select(migration.name, migration.status, migration.modified)
			.where(migration.wiki_url == self.wiki_url)
			.where(migration.helpdesk_url == self.helpdesk_url)
			.for_update()
			.run(as_dict=True)
		)
		if any(d.name != self.name and is_run_active(d) for d in rows):
			frappe.throw(
				_("Another migration from this wiki to this helpdesk is running. Try again when it's done.")
			)
		return next(d for d in rows if d.name == self.name)

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
		for field in OPTIONS:
			options = self.meta.get_options(field).split("\n")
			if plan.get(field) not in options:
				frappe.throw(
					_("{0} must be one of {1}.").format(_(self.meta.get_label(field)), ", ".join(options))
				)

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
		categories = self.get_categories(helpdesk)
		targets = self.create_categories(helpdesk, categories)
		self.import_items(wiki, helpdesk, targets, categories)
		self.link_imported_articles(helpdesk, categories)
		self.delete_empty_categories(helpdesk, categories)
		failed = any(d.status == "Failed" for d in self.items)
		self.db_set("status", "Completed with Errors" if failed else "Completed")

	def create_categories(self, helpdesk: HelpdeskClient, categories: dict[str, dict]) -> dict[str, str]:
		"""Each plan target's category, made when the helpdesk doesn't have it yet."""
		targets = frappe.parse_json(self.targets)
		target_categories = {}
		# parents first: their subcategories link to them
		for key, target in sorted(targets.items(), key=lambda row: bool(row[1].get("parent"))):
			target_categories[key] = target.get("category") or self.get_category(
				helpdesk, categories, target["name"].strip(), target_categories.get(target.get("parent"))
			)
		return target_categories

	def get_category(
		self, helpdesk: HelpdeskClient, categories: dict[str, dict], category_name: str, parent: str | None
	) -> str:
		"""The category named so under `parent`; a top-level General is the helpdesk's own.
		One it makes is marked `created`, so it's deleted again if no page lands in it."""
		for name, d in categories.items():
			if (d["category_name"] or "").lower() == category_name.lower() and d["parent_category"] == parent:
				return name
		values = {"category_name": category_name}
		if parent:
			values["parent_category"] = parent
		name = helpdesk.insert(CATEGORY, values)
		categories[name] = {"name": name, "parent_category": parent, "created": True, **values}
		return name

	def import_items(
		self, wiki: WikiClient, helpdesk: HelpdeskClient, targets: dict[str, str], categories: dict[str, dict]
	):
		imported = self.get_imported_articles(helpdesk, categories)
		for d in self.items:
			if d.status != "Pending":
				continue
			# what the page makes on the helpdesk, deleted again if it fails
			created = frappe._dict(article=None, files=[])
			try:
				d.update(self.import_page(wiki, helpdesk, d, targets[d.target], imported, created))
				d.finished = int(bool(d.article) and d.status != "Failed")
			except Exception as e:
				undo_error = self.undo_page(helpdesk, created)
				# an article imported before stays on its item, as it was
				if created.article or not d.finished:
					# an article it couldn't remove stays on the item, so a retry finishes it
					d.article = created.article if undo_error else None
					d.category = None
					d.finished = 0
				if isinstance(e, JobTimeoutException):
					# the run stops here, so the item's article is saved now
					frappe.db.set_value(
						"Wiki Migration Item",
						d.name,
						{"article": d.article, "finished": d.finished},
						update_modified=False,
					)
					frappe.db.commit()
					raise
				d.status = "Failed"
				d.message = "\n".join(filter(None, [get_error_message(e, d.wiki_route), undo_error]))
			d.category_name, d.parent_category_name = get_category_names(categories, d.category)
			frappe.clear_messages()
			# the newest item of a route is the one whose hash its article has, which a
			# failed page didn't write: it keeps its `modified`
			frappe.db.set_value(
				"Wiki Migration Item",
				d.name,
				{
					field: d.get(field)
					for field in (
						"title",
						"status",
						"message",
						"article",
						"category",
						"category_name",
						"parent_category_name",
						"finished",
						"content_hash",
					)
				},
				update_modified=d.status != "Failed",
			)
			frappe.db.commit()

	def import_page(
		self,
		wiki: WikiClient,
		helpdesk: HelpdeskClient,
		d,
		category: str,
		imported: dict,
		created: frappe._dict,
	) -> dict:
		"""Copy one wiki page into an article; the item's new values. `created` holds the
		article the page makes or takes over, and its files."""
		existing = imported.get(d.wiki_route)
		finished = bool(existing) and existing.finished
		# what the item points at when the page leaves its article as it is
		kept = {"article": None, "category": None, "content_hash": None}
		if finished:
			kept = {field: existing[field] for field in kept}
		if finished and self.on_existing == "Skip":
			return {**kept, "status": "Skipped", "message": None}
		if existing and not finished:
			# a stopped run made it but never finished it: this page takes it over
			created.article = existing.article
		data = wiki.get_page(d.wiki_route)
		title = (data.get("title") or d.title)[:NAME_LENGTH]
		html = data.get("rendered_content") or ""
		content_hash = get_content_hash(f"{title}\n{html}")
		if finished and content_hash == existing.content_hash:
			# the article isn't written, so what agents changed in it stays
			return {**kept, "status": "Unchanged", "message": None}
		page = WikiPage(html, wiki.base_url, d.wiki_route)
		if page.is_empty:
			message = _("The page is empty in the wiki")
			# a draft a stopped run left goes too; until it can, the page fails and keeps it
			if created.article and (undo_error := self.undo_page(helpdesk, created)):
				return {
					"status": "Failed",
					"message": f"{message}\n{undo_error}",
					"article": created.article,
					"category": None,
					"content_hash": None,
				}
			# an article imported before keeps what it had
			return {**kept, "status": "Skipped", "message": message}
		if finished:
			category = existing.category
		elif not created.article:
			# a draft without a category until it's complete: the helpdesk refuses to
			# delete a category's last article, which undoing a failed page would be
			created.article = helpdesk.insert(ARTICLE, {"title": title, "status": "Draft"})
			# recorded at once, so a run stopped before the page is done finds it again
			frappe.db.set_value("Wiki Migration Item", d.name, "article", created.article)
			frappe.db.commit()
		article = existing.article if finished else created.article
		warnings = self.copy_assets(wiki, helpdesk, page, article, bool(existing), created.files)
		values = {"title": title, "content": page.html}
		if not finished:
			values.update(category=category, status=self.article_status, visibility=self.visibility)
		helpdesk.update(ARTICLE, article, values)
		return {
			"status": "Updated" if finished else "Imported",
			"message": "\n".join(warnings) or None,
			"article": article,
			"category": category,
			"title": title,
			"content_hash": content_hash,
		}

	def copy_assets(
		self,
		wiki: WikiClient,
		helpdesk: HelpdeskClient,
		page: WikiPage,
		article: str,
		existing: bool,
		new_files: list[str],
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
					file_name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
					file = helpdesk.upload_file(article, file_name, content)
					new_files.append(file["name"])
					file_urls[content_hash] = file["file_url"]
				urls[url] = file_urls[content_hash]
			# a stream can break midway, past the client's own error handling
			except (frappe.ValidationError, requests.RequestException) as e:
				warnings.append(_("Couldn't copy {0}: {1}").format(url, e))
		page.replace_urls(urls)
		return warnings

	def undo_page(self, helpdesk: HelpdeskClient, created: frappe._dict) -> str | None:
		"""The helpdesk can't roll back, so what a failed page made there is deleted.
		None when everything went, else why it couldn't."""
		try:
			for file in created.files:
				helpdesk.delete("File", file)
			if created.article:
				helpdesk.delete(ARTICLE, created.article)
		except frappe.ValidationError as e:
			return _("Couldn't remove what it made on the helpdesk: {0}").format(e)

	def link_imported_articles(self, helpdesk: HelpdeskClient, categories: dict[str, dict]):
		"""Point links between pages of the wiki at their articles, now that they all exist."""
		imported = self.get_imported_articles(helpdesk, categories)
		# an unfinished article is a category-less draft, never a link target
		routes = {route: row.article for route, row in imported.items() if row.finished}
		articles = helpdesk.get_articles(
			list(routes.values()),
			["name", "content"],
			["content", "like", "%data-wiki-route%"],
		)
		for article in articles:
			if content := rewrite_wiki_links(article["content"], routes):
				helpdesk.update(ARTICLE, article["name"], {"content": content})

	def delete_empty_categories(self, helpdesk: HelpdeskClient, categories: dict[str, dict]):
		# subcategories were made after their parents, so they go first
		for category in reversed([name for name, d in categories.items() if d.get("created")]):
			has_subcategories = any(d["parent_category"] == category for d in categories.values())
			if has_subcategories or helpdesk.has_articles(category):
				continue
			helpdesk.delete(CATEGORY, category)
			del categories[category]

	def get_imported_articles(
		self, helpdesk: HelpdeskClient, categories: dict[str, dict]
	) -> dict[str, frappe._dict]:
		"""The article each page of this wiki was last migrated to on this helpdesk,
		when it's still there. One whose page never finished is not `finished`; `content_hash`
		is of the page as its article last took it."""
		item = DocType("Wiki Migration Item")
		migration = DocType("Wiki Migration")
		rows = (
			frappe.qb.from_(item)
			.join(migration)
			.on(migration.name == item.parent)
			.select(item.wiki_route, item.article, item.finished, item.content_hash)
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
				finished=bool(row.finished),
				content_hash=row.content_hash,
			)
		return imported


def get_job_id(name: str) -> str:
	return f"wiki-migration-{name}"


def is_run_active(migration) -> bool:
	"""Whether a migration (or its row) runs: its job is there, or it was queued too
	recently for the job to show yet."""
	if migration.status not in ACTIVE_STATUSES:
		return False
	just_queued = (
		migration.status == "Queued"
		and time_diff_in_seconds(now_datetime(), migration.modified) < ENQUEUE_GRACE
	)
	return just_queued or is_job_enqueued(get_job_id(migration.name))


def run_migration(name: str):
	frappe.get_doc("Wiki Migration", name).run()


def sync_migrations():
	"""Hourly: sync the migrations kept in sync. The least recently changed go first, so
	one that had to wait for another run between its sites goes ahead next time."""
	for name in frappe.get_all(
		"Wiki Migration",
		filters={"keep_in_sync": 1, "status": ("!=", "Draft")},
		order_by="modified asc",
		pluck="name",
	):
		try:
			frappe.get_doc("Wiki Migration", name).sync()
			frappe.db.commit()
		except frappe.ValidationError:
			# it, or another run between its sites, is running: the next hour tries again
			frappe.db.rollback()
			frappe.clear_messages()


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
