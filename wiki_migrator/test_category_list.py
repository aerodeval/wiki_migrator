from unittest import TestCase
from unittest.mock import patch

import frappe

from wiki_migrator.api import get_import, preview_list, retry_failed, start_list_import, sync_now
from wiki_migrator.test_api import (
	ARTICLE,
	HELPDESK_URL,
	PUBLIC_DOCUMENTS,
	WIKI_URL,
	MigrationCase,
	mock_wiki,
)
from wiki_migrator.wiki_migrator.doctype.wiki_migration.category_list import CategoryList
from wiki_migrator.wiki_migrator.doctype.wiki_migration.wiki_migration import run_migration

OPTIONS = {"article_status": "Published", "visibility": "Public", "on_existing": "Update"}
PAGES = {
	"cloud/plans": {"title": "Wiki plans title", "rendered_content": "<p>Plans</p>"},
	"cloud/billing": "<p>Billing</p>",
	"hr/leave": "<p>Leave</p>",
}
LIST = f"""# Support portal

Nov 2 · @someone

## Cloud

- Billing
  - [Plans]({WIKI_URL}/cloud/plans)
  - [Invoices]({WIKI_URL}/cloud/billing)

## HR

- [Leave]({WIKI_URL}/hr/leave)
"""


class TestCategoryList(TestCase):
	def read(self, text: str) -> CategoryList:
		return CategoryList(text, WIKI_URL, HELPDESK_URL)

	def test_plan_keeps_reading_order_as_weight(self):
		plan = self.read(LIST).get_plan()

		self.assertEqual(
			plan["targets"],
			{
				"category-0": {"name": "Cloud", "parent": None},
				"category-0-0": {"name": "Billing", "parent": "category-0"},
				"category-1": {"name": "HR", "parent": None},
			},
		)
		self.assertEqual(
			[(d["title"], d["route"], d["target"], d["weight"]) for d in plan["pages"]],
			[
				("Plans", "cloud/plans", "category-0-0", 2),
				("Invoices", "cloud/billing", "category-0-0", 1),
				("Leave", "hr/leave", "category-1", 1),
			],
		)

	def test_links_are_read_by_where_they_point(self):
		pages = self.read(
			f"## One\n- Sub\n  - [Article]({HELPDESK_URL}/help/articles/abc123-a-title)\n"
			f"  - [Doc]({WIKI_URL}/space/nested/page.md#heading)\n"
		).get_plan()["pages"]

		self.assertEqual((pages[0]["article"], pages[1]["route"]), ("abc123", "space/nested/page"))

	def test_what_cant_be_used_is_reported(self):
		category_list = self.read(
			"## One\n"
			"Some text\n"
			"- Sub\n"
			"  - Nested\n"
			f"  - [Doc]({WIKI_URL}/doc)\n"
			f"  - [Doc again]({WIKI_URL}/doc)\n"
			"  - [Elsewhere](https://other.example.com/doc)\n"
			f"  - [Not an article]({HELPDESK_URL}/help/category/x)\n"
		)

		self.assertEqual(
			[d["title"] for d in category_list.skipped],
			["Some text", "Nested", "Doc again", "Elsewhere", "Not an article"],
		)
		self.assertEqual(len(category_list.get_plan()["pages"]), 1)

	def test_a_subcategory_without_pages_makes_nothing(self):
		plan = self.read(f"## Empty\n- Nothing\n## Full\n- [Doc]({WIKI_URL}/doc)\n").get_plan()

		self.assertEqual(plan["targets"], {"category-1": {"name": "Full", "parent": None}})


class TestListImport(MigrationCase):
	def setUp(self):
		super().setUp()
		self.wiki = mock_wiki(self, pages=dict(PAGES), documents=PUBLIC_DOCUMENTS)
		self.name = self.connect()["name"]
		self.addCleanup(frappe.clear_messages)

	def import_list(self, text: str = LIST, **options) -> dict:
		start_list_import(self.name, text, {**OPTIONS, **options})
		run_migration(self.name)
		return get_import(self.name)

	def new_helpdesk_article(self, title: str, category: str) -> str:
		return self.helpdesk.insert(ARTICLE, {"title": title, "category": category, "status": "Published"})

	def test_preview_changes_nothing(self):
		article = self.new_helpdesk_article("Existing", self.helpdesk.general)
		text = LIST + f"- [Moved]({HELPDESK_URL}/help/articles/{article})\n"
		text += f"- [Gone]({HELPDESK_URL}/help/articles/missing)\n"

		preview = preview_list(self.name, text)

		self.assertEqual(preview["page_count"], 4)
		self.assertEqual(preview["already_imported"], 0)
		self.assertTrue(preview["supports_subcategories"])
		self.assertEqual(preview["skipped"], [{"title": "Gone", "reason": "Not found on this helpdesk"}])
		self.assertEqual(len(self.helpdesk.records[ARTICLE]), 1)
		self.assertEqual(get_import(self.name)["status"], "Draft")

	def test_import_builds_the_list_with_its_labels_and_order(self):
		result = self.import_list()

		self.assertEqual(result["status"], "Completed", result["error"])
		cloud = self.helpdesk.category("Cloud")
		billing = self.helpdesk.category("Billing")
		self.assertEqual(billing["parent_article_category"], cloud["name"])
		plans = self.helpdesk.articles(title="Plans")[0]
		self.assertEqual((plans["category"], plans["weight"]), (billing["name"], 2))
		self.assertEqual(self.helpdesk.articles(title="Invoices")[0]["weight"], 1)
		self.assertFalse(self.helpdesk.articles(title="Wiki plans title"))

	def test_a_helpdesk_article_is_moved_not_copied(self):
		old = self.helpdesk.insert("HD Article Category", {"category_name": "Old"})
		self.new_helpdesk_article("Stays behind", old)
		article = self.new_helpdesk_article("Existing", old)
		text = f"## Cloud\n- Billing\n  - [Plans]({WIKI_URL}/cloud/plans)\n"
		text += f"  - [Label]({HELPDESK_URL}/help/articles/{article})\n"

		result = self.import_list(text)

		record = self.helpdesk.records[ARTICLE][article]
		self.assertEqual(result["status"], "Completed", result["items"])
		self.assertEqual(record["category"], self.helpdesk.category("Billing")["name"])
		self.assertEqual((record["weight"], record["title"]), (1, "Existing"))
		self.assertEqual(result["counts"]["updated"], 1)

	def test_a_refused_move_keeps_the_article_and_retries(self):
		old = self.helpdesk.insert("HD Article Category", {"category_name": "Old"})
		article = self.new_helpdesk_article("Last in its category", old)
		text = LIST + f"- [Label]({HELPDESK_URL}/help/articles/{article})\n"

		result = self.import_list(text)

		self.assertEqual(result["status"], "Completed with Errors")
		item = next(d for d in result["items"] if d["status"] == "Failed")
		self.assertEqual(item["article"], article)
		self.assertIn("only article", item["message"])

		self.new_helpdesk_article("Another", old)
		retry_failed(self.name)
		run_migration(self.name)
		self.assertEqual(get_import(self.name)["status"], "Completed")
		self.assertEqual(
			self.helpdesk.records[ARTICLE][article]["category"], self.helpdesk.category("HR")["name"]
		)

	def test_sync_keeps_what_agents_changed_and_the_labels(self):
		plans = next(d for d in self.import_list()["items"] if d["title"] == "Plans")["article"]
		self.helpdesk.records[ARTICLE][plans].update(category=self.helpdesk.general, weight=9)
		changed_page = {"title": "Wiki plans title", "rendered_content": "<p>New plans</p>"}
		mock_wiki(self, pages={**PAGES, "cloud/plans": changed_page}, documents=PUBLIC_DOCUMENTS)

		sync_now(self.name)
		run_migration(self.name)

		record = self.helpdesk.records[ARTICLE][plans]
		self.assertEqual(record["content"], "<p>New plans</p>")
		self.assertEqual(
			(record["title"], record["category"], record["weight"]),
			("Plans", self.helpdesk.general, 9),
		)

	def test_a_new_list_moves_pages_imported_before(self):
		plans = next(d for d in self.import_list()["items"] if d["title"] == "Plans")["article"]
		self.name = self.connect()["name"]

		text = f"## HR\n- [Plans]({WIKI_URL}/cloud/plans)\n- [Leave]({WIKI_URL}/hr/leave)\n"
		result = self.import_list(text, on_existing="Skip")

		self.assertEqual(result["status"], "Completed", result["items"])
		record = self.helpdesk.records[ARTICLE][plans]
		self.assertEqual((record["category"], record["weight"]), (self.helpdesk.category("HR")["name"], 2))

	def test_a_category_of_failed_pages_leaves_nothing_empty(self):
		text = f"## Lost\n- Gone\n  - [Missing]({WIKI_URL}/cloud/missing)\n" + LIST

		result = self.import_list(text)

		self.assertEqual(result["status"], "Completed with Errors", result["error"])
		self.assertIsNone(self.helpdesk.category("Lost"))
		self.assertIsNone(self.helpdesk.category("Gone"))
		self.assertIsNotNone(self.helpdesk.category("Billing"))

	def test_a_full_helpdesk_queue_is_waited_for(self):
		self.helpdesk.overloaded_saves = 3

		with patch("time.sleep") as sleep:
			result = self.import_list()

		self.assertEqual(result["status"], "Completed", result["error"])
		self.assertEqual(sleep.call_count, 3)


class TestListImportOnFlatHelpdesk(MigrationCase):
	supports_subcategories = False

	def test_subcategories_are_refused_and_previewed(self):
		mock_wiki(self, documents=PUBLIC_DOCUMENTS)
		name = self.connect()["name"]

		self.assertFalse(preview_list(name, LIST)["supports_subcategories"])
		with self.assertRaises(frappe.ValidationError):
			start_list_import(name, LIST, OPTIONS)
