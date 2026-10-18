import json
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import frappe
import requests
from frappe.core.doctype.file.utils import get_content_hash
from frappe.tests import IntegrationTestCase
from rq.timeouts import JobTimeoutException

from wiki_migrator.api import (
	connect,
	get_categories,
	get_import,
	get_recent,
	get_tree,
	retry_failed,
	start_import,
)
from wiki_migrator.wiki_migrator.doctype.wiki_migration.wiki_migration import (
	WikiMigration,
	run_migration,
)

WIKI_URL = "https://wiki.example.com"
HELPDESK_URL = "https://helpdesk.example.com"
HELPDESK_AUTH = "token hd-key:hd-secret"
ARTICLE, CATEGORY = "HD Article", "HD Article Category"
ROOT_INDEX = f"""# Example wiki

## Spaces

- [Handbook]({WIKI_URL}/handbook/llms.txt)
- [Partner]({WIKI_URL}/partner/llms.txt)
"""
HANDBOOK_INDEX = f"""# Handbook

## Handbook

- **Engineering**
  - [Debugging]({WIKI_URL}/handbook/debugging.md)
  - [Release]({WIKI_URL}/handbook/release.md)
- [Sales]({WIKI_URL}/handbook/sales.md)
  - [Introduction]({WIKI_URL}/handbook/sales.md)
"""
PUBLIC_DOCUMENTS = {"/llms.txt": ROOT_INDEX, "/handbook/llms.txt": HANDBOOK_INDEX}
IS_JOB_ENQUEUED = "wiki_migrator.wiki_migrator.doctype.wiki_migration.wiki_migration.is_job_enqueued"


def make_response(status: int, body, url: str) -> requests.Response:
	response = requests.Response()
	response.status_code, response.url = status, url
	response._content_consumed = True
	if isinstance(body, dict | list):
		body = json.dumps(body)
	response._content = body if isinstance(body, bytes) else body.encode()
	return response


def mock_wiki(case, pages: dict | None = None, files: dict | None = None, documents=None, error=None):
	"""Answer the wiki client's requests for `WIKI_URL`, so no test reaches the network;
	the rest go to the test's helpdesk.

	`pages` maps a route to its rendered HTML or to an HTTP status the page fails with.
	`files` maps a `/files/...` path to its bytes. `documents` maps any other path
	(`/llms.txt`, an API resource) to its text or JSON. Anything else is a 404.
	`error` is raised for every wiki request instead.
	"""
	pages, files, documents = pages or {}, files or {}, documents or {}

	def request(method, url, params=None, **kwargs):
		if not url.startswith(WIKI_URL):
			return case.helpdesk.respond(method, url, params=params, **kwargs)
		if error:
			raise error
		path = url.removeprefix(WIKI_URL)
		if path.endswith(".get_page_data"):
			page = pages.get(params["route"])
			if page is None:
				return make_response(404, {"exc_type": "DoesNotExistError"}, url)
			if isinstance(page, int):
				return make_response(page, "Server error", url)
			return make_response(200, {"message": {"rendered_content": page}}, url)
		if path in files:
			return make_response(200, files[path], url)
		if path in documents:
			return make_response(200, documents[path], url)
		return make_response(404, "Not found", url)

	return case.enterContext(patch.object(requests.Session, "request", side_effect=request))


class FakeHelpdesk:
	"""An in-memory helpdesk answering the migrator's REST calls for `HELPDESK_URL`.

	Like the real one it refuses to delete a category's last article, and deleting an
	article deletes its files. `permissions` maps `(doctype, permission)` to False
	(denied) or None (no such doctype); `rejects` maps `(method, doctype)` to the
	message the helpdesk refuses that call with.
	"""

	def __init__(self, case, supports_subcategories: bool = True):
		self.supports_subcategories = supports_subcategories
		self.permissions, self.rejects = {}, {}
		self.records = {ARTICLE: {}, CATEGORY: {}, "File": {}}
		self.general = self.insert(CATEGORY, {"category_name": "General"})
		case.enterContext(patch.object(requests.Session, "request", side_effect=self.respond))

	def respond(self, method, url, headers=None, params=None, **kwargs):
		path = unquote(urlsplit(url).path)
		if not url.startswith(HELPDESK_URL):
			return make_response(404, "Not found", url)
		if path == "/api/method/ping":
			return make_response(200, {"message": "pong"}, url)
		if (headers or {}).get("Authorization") != HELPDESK_AUTH:
			return make_response(401, {"exc_type": "AuthenticationError"}, url)
		doctype, _, name = path.removeprefix("/api/resource/").partition("/")
		if message := self.rejects.get((method, doctype)):
			return self.error(message, url)
		try:
			body = self.answer(method, path, doctype, name, params or {}, **kwargs)
		except frappe.ValidationError as e:
			return self.error(str(e), url)
		if body is None:
			return make_response(404, {"exc_type": "DoesNotExistError"}, url)
		return make_response(200, body, url)

	def answer(self, method, path, doctype, name, params, json=None, data=None, files=None, **kwargs):
		if path == "/api/method/frappe.auth.get_logged_user":
			return {"message": "Administrator"}
		if path == "/api/method/frappe.client.has_permission":
			allowed = self.permissions.get((params["doctype"], params["perm_type"]), True)
			return None if allowed is None else {"message": {"has_permission": allowed}}
		if path == "/api/method/frappe.desk.form.load.getdoctype":
			fields = ["category_name"] + (["parent_category"] if self.supports_subcategories else [])
			return {"docs": [{"fields": [{"fieldname": d} for d in fields]}]}
		if path == "/api/method/frappe.client.get_list":
			return {"message": self.get_list(params)}
		if path == "/api/method/upload_file":
			return {"message": self.upload_file(data, *files["file"])}
		if method == "POST":
			return {"data": {"name": self.insert(doctype, json)}}
		if method == "PUT":
			self.records[doctype][name].update(json)
			return {"data": self.records[doctype][name]}
		if method == "DELETE":
			self.delete(doctype, name)
			return {"message": "ok"}

	def get_list(self, params: dict) -> list[dict]:
		fields, filters = json.loads(params["fields"]), json.loads(params["filters"])
		rows = [
			{field: d.get(field) for field in fields}
			for d in self.records[params["doctype"]].values()
			if all(self.matches(d.get(field), *rest) for field, *rest in filters)
		]
		return rows[: params["limit_page_length"] or None]

	def matches(self, actual, operator: str, value) -> bool:
		if operator == "in":
			return actual in value
		if operator == "like":
			return value.strip("%") in (actual or "")
		return actual == value

	def insert(self, doctype: str, values: dict) -> str:
		name = frappe.generate_hash(length=10)
		self.records[doctype][name] = {"name": name, "parent_category": None, **values}
		return name

	def upload_file(self, data: dict, file_name: str, content: bytes) -> dict:
		name = self.insert(
			"File",
			{
				"file_url": f"/files/{file_name}",
				"content_hash": get_content_hash(content),
				"attached_to_doctype": data["doctype"],
				"attached_to_name": data["docname"],
			},
		)
		return self.records["File"][name]

	def delete(self, doctype: str, name: str) -> None:
		if doctype == ARTICLE:
			category = self.records[ARTICLE][name].get("category")
			if category and len(self.articles(category=category)) == 1:
				frappe.throw("Category must have at least one article")
			for file in self.files(name):
				del self.records["File"][file["name"]]
		del self.records[doctype][name]

	def error(self, message: str, url: str) -> requests.Response:
		messages = json.dumps([json.dumps({"message": message})])
		return make_response(417, {"_server_messages": messages}, url)

	def articles(self, **filters) -> list[dict]:
		return [
			d
			for d in self.records[ARTICLE].values()
			if all(d.get(key) == value for key, value in filters.items())
		]

	def files(self, article: str) -> list[dict]:
		return [d for d in self.records["File"].values() if d["attached_to_name"] == article]

	def category(self, category_name: str) -> dict | None:
		return next(
			(d for d in self.records[CATEGORY].values() if d["category_name"] == category_name),
			None,
		)


def simulate_commits(case) -> None:
	"""Let a job that commits and rolls back run inside a test: a commit becomes a
	savepoint and a rollback returns to it, and the test's transaction rolls back at
	the end as usual."""
	savepoint, rollback = frappe.db.savepoint, frappe.db.rollback
	savepoint("wiki_migrator_test_commit")
	case.enterContext(
		patch.object(
			frappe.db,
			"commit",
			side_effect=lambda *args, **kwargs: savepoint("wiki_migrator_test_commit"),
		)
	)
	case.enterContext(
		patch.object(
			frappe.db,
			"rollback",
			side_effect=lambda *args, **kwargs: rollback(save_point="wiki_migrator_test_commit"),
		)
	)


class MigrationCase(IntegrationTestCase):
	supports_subcategories = True

	def setUp(self):
		self.enqueue = self.enterContext(patch("frappe.enqueue"))
		simulate_commits(self)
		self.helpdesk = FakeHelpdesk(self, self.supports_subcategories)

	def connect(self, url: str = f"{WIKI_URL}/handbook/debugging", **kwargs) -> dict:
		credentials = {"helpdesk_api_key": "hd-key", "helpdesk_api_secret": "hd-secret"}
		return connect(url, HELPDESK_URL, **{**credentials, **kwargs})

	def migrate(self, name: str, pages: list[dict], targets: dict, **options) -> dict:
		plan = {
			"article_status": "Published",
			"visibility": "Public",
			"on_existing": "Update",
			**options,
			"targets": targets,
			"pages": pages,
		}
		start_import(name, plan)
		run_migration(name)
		return get_import(name)


class TestAccess(MigrationCase):
	def test_only_a_system_manager_can_migrate(self):
		mock_wiki(self, documents=PUBLIC_DOCUMENTS)
		name = self.connect()["name"]
		user = frappe.get_doc(
			{"doctype": "User", "email": "wiki-migrator-user@example.com", "first_name": "Wiki"}
		).insert(ignore_permissions=True)

		with self.set_user(user.name):
			for call in (
				lambda: self.connect(),
				lambda: get_tree(name, "handbook"),
				lambda: get_import(name),
				lambda: start_import(name, {}),
				lambda: retry_failed(name),
				lambda: get_recent(),
			):
				with self.assertRaises(frappe.PermissionError):
					call()


class TestConnect(MigrationCase):
	def test_connect_lists_spaces_and_reuses_the_draft(self):
		session_request = mock_wiki(self, documents=PUBLIC_DOCUMENTS)

		result = self.connect("wiki.example.com/handbook/debugging")

		self.assertEqual(
			(result["wiki_url"], result["helpdesk_url"], result["space"]),
			(WIKI_URL, HELPDESK_URL, "handbook"),
		)
		self.assertEqual(result["spaces"][0], {"id": "handbook", "title": "Handbook", "route": "handbook"})
		self.assertTrue(result["supports_subcategories"])
		self.assertEqual(self.connect(WIKI_URL)["name"], result["name"])
		# the helpdesk key never goes to the wiki, and its secret is stored encrypted
		wiki_calls = [c for c in session_request.call_args_list if c.args[1].startswith(WIKI_URL)]
		self.assertTrue(wiki_calls)
		self.assertFalse([c for c in wiki_calls if c.kwargs.get("headers")])
		self.assertNotEqual(
			frappe.db.get_value("Wiki Migration", result["name"], "helpdesk_api_secret"),
			"hd-secret",
		)

	def test_errors_name_the_site_that_failed(self):
		mock_wiki(self)
		with self.assertRaisesRegex(frappe.ValidationError, "^Wiki: .*public index"):
			self.connect()

		mock_wiki(self, documents=PUBLIC_DOCUMENTS)
		with self.assertRaisesRegex(frappe.ValidationError, "^Helpdesk: .*didn't accept"):
			self.connect(helpdesk_api_secret="wrong")

		self.helpdesk.permissions[(CATEGORY, "create")] = False
		with self.assertRaisesRegex(frappe.ValidationError, "can't create HD Article Category"):
			self.connect()

		self.helpdesk.permissions[(ARTICLE, "read")] = None
		with self.assertRaisesRegex(frappe.ValidationError, "not a Frappe Helpdesk"):
			self.connect()


class TestGetTree(MigrationCase):
	def test_tree_marks_pages_migrated_before(self):
		mock_wiki(self, documents=PUBLIC_DOCUMENTS, pages={"handbook/debugging": "<p>Debug</p>"})
		self.migrate(
			self.connect()["name"],
			[{"route": "handbook/debugging", "title": "Debugging", "target": "t"}],
			{"t": {"name": "Engineering"}},
		)
		name = self.connect()["name"]

		engineering, _sales = get_tree(name, "handbook")["nodes"]

		debugging, release = engineering["children"]
		self.assertEqual(
			(debugging["imported"]["title"], debugging["imported"]["category_name"]),
			("Debugging", "Engineering"),
		)
		self.assertIsNone(release["imported"])
		self.assertEqual({d["category_name"] for d in get_categories(name)}, {"General", "Engineering"})

		# an article deleted on the helpdesk is no longer a migrated page
		self.helpdesk.records[ARTICLE].clear()
		self.assertIsNone(get_tree(name, "handbook")["nodes"][0]["children"][0]["imported"])


class TestPlanValidation(MigrationCase):
	def setUp(self):
		super().setUp()
		mock_wiki(self, documents=PUBLIC_DOCUMENTS)
		self.name = self.connect()["name"]
		self.page = {"route": "handbook/debugging", "title": "Debugging", "target": "t"}

	def assert_rejected(self, message: str, targets: dict, pages: list | None = None):
		plan = {"targets": targets, "pages": [self.page] if pages is None else pages}
		with self.assertRaisesRegex(frappe.ValidationError, message):
			start_import(self.name, plan)
		self.enqueue.assert_not_called()

	def test_plan_needs_pages_with_known_targets(self):
		self.assert_rejected("at least one page", {"t": {"name": "A"}}, pages=[])
		self.assert_rejected("Pick a category for Debugging", {"other": {"name": "A"}})
		self.assert_rejected("needs a name", {"t": {"name": "  "}})
		self.assert_rejected("at most 140", {"t": {"name": "x" * 141}})
		self.assert_rejected("not found", {"t": {"category": "no-such-category"}})

	def test_categories_go_one_level_deep(self):
		parent = self.helpdesk.insert(CATEGORY, {"category_name": "Parent"})
		child = self.helpdesk.insert(CATEGORY, {"category_name": "Child", "parent_category": parent})

		self.assert_rejected(
			"can't have subcategories",
			{
				"top": {"name": "Top"},
				"mid": {"name": "Mid", "parent": "top"},
				"t": {"name": "Leaf", "parent": "mid"},
			},
		)
		self.assert_rejected(
			"can't have subcategories",
			{"sub": {"category": child}, "t": {"name": "Leaf", "parent": "sub"}},
		)

	def test_general_is_never_a_subcategory_or_a_parent(self):
		self.assert_rejected(
			"General can't be a subcategory",
			{"top": {"name": "Top"}, "t": {"name": "general", "parent": "top"}},
		)
		self.assert_rejected(
			"General can't have subcategories",
			{
				"general": {"category": self.helpdesk.general},
				"t": {"name": "Leaf", "parent": "general"},
			},
		)

	def test_deeply_nested_group_keys_fit(self):
		# public-mode group keys carry their whole title path
		key = "group:" + "/".join(["A long group title"] * 20)

		start_import(self.name, {"targets": {key: {"name": "A"}}, "pages": [{**self.page, "target": key}]})

		self.assertEqual(frappe.get_doc("Wiki Migration", self.name).items[0].target, key)

	def test_only_a_draft_can_start(self):
		plan = {"targets": {"t": {"name": "A"}}, "pages": [self.page]}
		start_import(self.name, plan)
		with self.assertRaisesRegex(frappe.ValidationError, "already started"):
			start_import(self.name, plan)
		self.enqueue.assert_called_once()


class TestFlatHelpdesk(MigrationCase):
	"""A helpdesk whose categories don't nest takes flat plans only."""

	supports_subcategories = False

	def test_subcategories_are_rejected_and_flat_plans_migrate(self):
		mock_wiki(self, documents=PUBLIC_DOCUMENTS, pages={"handbook/debugging": "<p>Debug</p>"})
		connection = self.connect()
		self.assertFalse(connection["supports_subcategories"])
		page = {"route": "handbook/debugging", "title": "Debugging", "target": "t"}

		with self.assertRaisesRegex(frappe.ValidationError, "doesn't support subcategories"):
			start_import(
				connection["name"],
				{
					"targets": {"top": {"name": "Top"}, "t": {"name": "Sub", "parent": "top"}},
					"pages": [page],
				},
			)

		result = self.migrate(connection["name"], [page], {"t": {"name": "Engineering"}})

		self.assertEqual(result["status"], "Completed")
		self.assertFalse(result["supports_subcategories"])
		self.assertEqual(
			self.helpdesk.articles()[0]["category"],
			self.helpdesk.category("Engineering")["name"],
		)


class TestMigrationJob(MigrationCase):
	"""A run copies each page into an article, one page's failure never touching another."""

	def setUp(self):
		super().setUp()
		self.wiki_pages = {
			"handbook/debugging": (
				'<h1 id="debug">Debug</h1><p><img src="/files/shot.png" alt="Shot"/></p>'
				'<p>See <a href="/handbook/release#steps">release</a> and '
				'<img src="/files/missing.png" alt="Gone"/></p>'
			),
			"handbook/release": '<p>Release <a href="/handbook/debugging">debugging</a></p>',
			"handbook/guide": "<p>Guide</p>",
			"handbook/faq": "<p>FAQ</p>",
			"handbook/broken": 500,
			"handbook/blank": '<p class="wiki-blank-line"><br></p>',
		}
		mock_wiki(
			self,
			pages=self.wiki_pages,
			files={"/files/shot.png": b"png"},
			documents=PUBLIC_DOCUMENTS,
		)
		self.existing = self.helpdesk.insert(CATEGORY, {"category_name": "Existing"})
		self.helpdesk.insert(ARTICLE, {"title": "Old", "category": self.existing})
		self.targets = {
			"eng": {"name": "Engineering"},
			"backend": {"name": "Backend", "parent": "eng"},
			"existing": {"category": self.existing},
			# a new subcategory of a category that already exists
			"playbooks": {"name": "Playbooks", "parent": f"existing:{self.existing}"},
			f"existing:{self.existing}": {"category": self.existing},
			# a new top-level General is the helpdesk's own
			"general": {"name": "general"},
			"doomed": {"name": "Doomed"},
		}

	def pages(self) -> list[dict]:
		return [
			{"route": "handbook/debugging", "title": "Debugging", "target": "backend"},
			{"route": "handbook/release", "title": "Release", "target": "playbooks"},
			{"route": "handbook/guide", "title": "Guide", "target": "existing"},
			{"route": "handbook/faq", "title": "FAQ", "target": "general"},
			{"route": "handbook/broken", "title": "Broken", "target": "doomed"},
			{"route": "handbook/blank", "title": "Blank", "target": "eng"},
		]

	def items(self, result: dict) -> dict:
		return {d["wiki_route"]: frappe._dict(d) for d in result["items"]}

	def test_migration_creates_categories_articles_and_files(self):
		result = self.migrate(self.connect()["name"], self.pages(), self.targets)

		items = self.items(result)
		self.assertEqual(result["status"], "Completed with Errors")
		self.assertEqual(
			result["counts"],
			{"total": 6, "imported": 4, "updated": 0, "skipped": 1, "failed": 1, "pending": 0},
		)
		self.assertEqual(items["handbook/blank"].message, "The page is empty in the wiki")
		self.assertIn("500", items["handbook/broken"].message)

		engineering = self.helpdesk.category("Engineering")
		backend = self.helpdesk.category("Backend")
		self.assertEqual(backend["parent_category"], engineering["name"])
		self.assertEqual(self.helpdesk.category("Playbooks")["parent_category"], self.existing)
		self.assertEqual(items["handbook/guide"].category, self.existing)
		self.assertEqual(items["handbook/faq"].category, self.helpdesk.general)
		self.assertEqual(
			(items["handbook/debugging"].category_name, items["handbook/debugging"].parent_category_name),
			("Backend", "Engineering"),
		)
		# the category made only for the failed page is cleaned up
		self.assertIsNone(self.helpdesk.category("Doomed"))

		debugging = items["handbook/debugging"]
		self.assertEqual(debugging.article_url, f"{HELPDESK_URL}/helpdesk/kb/articles/{debugging.article}")
		article = self.helpdesk.records[ARTICLE][debugging.article]
		self.assertEqual(
			(article["title"], article["status"], article["visibility"], article["category"]),
			("Debugging", "Published", "Public", backend["name"]),
		)
		self.assertIn('<h2 id="debug">Debug</h2>', article["content"])
		(file,) = self.helpdesk.files(debugging.article)
		self.assertIn(f'src="{file["file_url"]}"', article["content"])
		# a file that couldn't be copied keeps pointing at the wiki, with a warning
		self.assertIn(f'src="{WIKI_URL}/files/missing.png"', article["content"])
		self.assertIn("missing.png", debugging.message)

		release = items["handbook/release"].article
		self.assertIn(f'href="/helpdesk/kb-public/articles/{release}#steps"', article["content"])
		self.assertIn(
			f'href="/helpdesk/kb-public/articles/{debugging.article}"',
			self.helpdesk.records[ARTICLE][release]["content"],
		)
		self.assertEqual(get_recent()[0]["counts"]["imported"], 4)

	def test_rerun_skips_or_updates_without_duplicates(self):
		first = self.items(self.migrate(self.connect()["name"], self.pages(), self.targets))
		debugging = first["handbook/debugging"].article
		self.helpdesk.records[ARTICLE][debugging].update(status="Draft", category=self.existing)
		self.wiki_pages["handbook/debugging"] = '<p>Rewritten <img src="/files/shot.png"/></p>'
		articles = len(self.helpdesk.articles())
		pages = self.pages()[:1]

		skipped = self.migrate(self.connect()["name"], pages, self.targets, on_existing="Skip")
		self.assertEqual(skipped["items"][0]["status"], "Skipped")
		self.assertNotIn("Rewritten", self.helpdesk.records[ARTICLE][debugging]["content"])

		pages[0]["title"] = "Debugging 2"
		updated = self.migrate(self.connect()["name"], pages, self.targets, on_existing="Update")
		item = updated["items"][0]
		self.assertEqual(
			(item["status"], item["article"], item["category"]),
			("Updated", debugging, self.existing),
		)
		article = self.helpdesk.records[ARTICLE][debugging]
		self.assertEqual(
			(article["title"], article["status"], article["category"]),
			("Debugging 2", "Draft", self.existing),
		)
		self.assertIn("Rewritten", article["content"])
		self.assertEqual(len(self.helpdesk.articles()), articles)
		# the same image again is not a second file
		self.assertEqual(len(self.helpdesk.files(debugging)), 1)

	def test_failed_page_leaves_nothing_on_the_helpdesk(self):
		self.helpdesk.rejects[("PUT", ARTICLE)] = "Rejected"
		pages = self.pages()[:1]

		item = self.migrate(self.connect()["name"], pages, self.targets)["items"][0]

		self.assertEqual((item["status"], item["message"]), ("Failed", "Rejected"))
		self.assertIsNone(item["article"])
		self.assertEqual([d["title"] for d in self.helpdesk.articles()], ["Old"])
		self.assertFalse(self.helpdesk.records["File"])
		self.assertIsNone(self.helpdesk.category("Backend"))
		self.assertIsNone(self.helpdesk.category("Engineering"))

	def test_retry_migrates_the_pages_that_failed(self):
		name = self.connect()["name"]
		self.migrate(name, self.pages(), self.targets)
		self.wiki_pages["handbook/broken"] = "<p>Fixed</p>"

		self.assertEqual(retry_failed(name)["status"], "Queued")
		run_migration(name)
		result = get_import(name)

		self.assertEqual(result["status"], "Completed")
		broken = self.items(result)["handbook/broken"]
		self.assertEqual((broken.status, broken.category_name), ("Imported", "Doomed"))

	def test_run_failing_after_its_pages_can_be_retried(self):
		name = self.connect()["name"]
		pages = self.pages()[:4]
		with patch.object(
			WikiMigration, "link_imported_articles", side_effect=frappe.ValidationError("Links broke")
		):
			result = self.migrate(name, pages, self.targets)
		self.assertEqual((result["status"], result["counts"]["imported"]), ("Failed", 4))
		self.assertTrue(result["can_retry"])

		self.assertEqual(retry_failed(name)["status"], "Queued")
		run_migration(name)
		result = get_import(name)

		self.assertEqual(result["status"], "Completed")
		self.assertFalse(result["can_retry"])
		release = self.helpdesk.articles(title="Release")[0]
		self.assertIn('href="/helpdesk/kb-public/articles/', release["content"])

	def test_retry_after_a_stopped_worker_finishes_the_same_article(self):
		name = self.connect()["name"]
		# the worker dies after the article is made, before the page is done
		with patch.object(WikiMigration, "copy_assets", side_effect=SystemExit):
			with self.assertRaises(SystemExit):
				self.migrate(name, self.pages()[:1], self.targets)
		(article,) = self.helpdesk.articles(title="Debugging")
		self.assertEqual(get_import(name)["items"][0]["article"], article["name"])

		with patch(IS_JOB_ENQUEUED, return_value=False):
			retry_failed(name)
		run_migration(name)

		item = get_import(name)["items"][0]
		self.assertEqual((item["status"], item["article"]), ("Imported", article["name"]))
		self.assertEqual(len(self.helpdesk.articles(title="Debugging")), 1)
		self.assertEqual(article["category"], self.helpdesk.category("Backend")["name"])

	def test_timed_out_page_is_undone_and_made_again_on_retry(self):
		name = self.connect()["name"]
		with patch.object(WikiMigration, "copy_assets", side_effect=JobTimeoutException):
			result = self.migrate(name, self.pages()[:1], self.targets)
		self.assertEqual(result["status"], "Failed")
		self.assertIsNone(result["items"][0]["article"])
		self.assertFalse(self.helpdesk.articles(title="Debugging"))

		retry_failed(name)
		run_migration(name)

		item = get_import(name)["items"][0]
		self.assertEqual(item["status"], "Imported")
		self.assertEqual([d["name"] for d in self.helpdesk.articles(title="Debugging")], [item["article"]])

	def test_only_one_run_between_two_sites_at_a_time(self):
		plan = {"targets": self.targets, "pages": self.pages()}
		start_import(self.connect()["name"], plan)
		second = self.connect()["name"]

		with patch(IS_JOB_ENQUEUED, return_value=True):
			with self.assertRaisesRegex(frappe.ValidationError, "Another migration"):
				start_import(second, plan)
		# the first run's worker is gone, so it no longer blocks
		with patch(IS_JOB_ENQUEUED, return_value=False):
			self.assertEqual(start_import(second, plan)["status"], "Queued")

	def test_unreachable_wiki_fails_the_whole_run(self):
		name = self.connect()["name"]
		mock_wiki(self, error=requests.exceptions.ConnectionError())

		result = self.migrate(name, self.pages(), self.targets)

		self.assertEqual(result["status"], "Failed")
		self.assertEqual(result["error"], f"Couldn't reach {WIKI_URL}. Check the URL and try again.")
		self.assertEqual(result["counts"]["pending"], 6)

	def test_stale_run_reports_itself_and_retries(self):
		name = self.connect()["name"]
		start_import(name, {"targets": self.targets, "pages": self.pages()})
		frappe.db.set_value("Wiki Migration", name, "status", "Running")

		with patch(IS_JOB_ENQUEUED, return_value=True):
			result = get_import(name)
			self.assertEqual((result["running"], result["stale"], result["can_retry"]), (True, False, False))
			with self.assertRaisesRegex(frappe.ValidationError, "already running"):
				retry_failed(name)

		with patch(IS_JOB_ENQUEUED, return_value=False):
			result = get_import(name)
			self.assertEqual((result["running"], result["stale"], result["can_retry"]), (False, True, True))
			self.assertEqual(retry_failed(name)["status"], "Queued")
		self.assertEqual(self.enqueue.call_count, 2)
