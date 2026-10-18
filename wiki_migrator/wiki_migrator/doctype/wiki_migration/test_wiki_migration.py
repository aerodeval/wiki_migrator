# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

from frappe.tests.utils import FrappeTestCase

from wiki_migrator.wiki_migrator.doctype.wiki_migration.wiki_client import (
	_api_node,
	_index_nodes,
	drop_duplicate_routes,
	parse_index,
)
from wiki_migrator.wiki_migrator.doctype.wiki_migration.wiki_html import (
	WikiPage,
	rewrite_wiki_links,
)

BASE_URL = "https://wiki.example.com"
# trimmed from https://frappe.io/partner/llms.txt and /handbook/llms.txt
SPACE_INDEX = """# Partner

Append `.md` to any URL below for that page's raw markdown source.

## Partner

- **Partner Maturity Model**
  - [Partner Maturity Model](https://frappe.io/partner/introduction.md)
  - [Frappe Ecosystem](https://frappe.io/partner/frappe-ecosystem.md)
- [Implementation](https://frappe.io/partner/implementation.md)
  - [Implementation](https://frappe.io/partner/implementation.md)
- [Sales](https://frappe.io/handbook/sales.md)
  - [Sales Playbook](https://frappe.io/handbook/sales_playbook.md)
  - [Introduction](https://frappe.io/handbook/sales.md)
  - [Clubs @ Frappe](https://frappe.io/handbook/clubs_@_frappe.md)
  - [Engineering](https://frappe.io/introduction.md)
- [Loose Page](https://frappe.io/partner/loose.md)
"""


def convert(html: str, route: str = "docs/page") -> WikiPage:
	return WikiPage(html, BASE_URL, route)


class TestWikiIndex(FrappeTestCase):
	"""A space's llms.txt and its API tree read as the same nested groups and pages."""

	def test_index_items_with_children_become_groups(self):
		nodes = _index_nodes(parse_index(SPACE_INDEX))

		self.assertEqual(
			[(d["key"], d["is_group"]) for d in nodes],
			[
				("group:Partner Maturity Model", True),
				("group:Implementation", True),
				("group:Sales", True),
				("partner/loose", False),
			],
		)
		sales = nodes[2]
		# a linked group's own URL is its landing page, which its children list already
		self.assertIsNone(sales["route"])
		self.assertEqual(
			[d["route"] for d in sales["children"]],
			[
				"handbook/sales_playbook",
				"handbook/sales",
				"handbook/clubs_@_frappe",
				"introduction",
			],
		)
		self.assertEqual(nodes[1]["children"][0]["key"], "partner/implementation")
		self.assertTrue(all(d["published"] for d in sales["children"]))

	def test_nested_group_keys_carry_the_title_path(self):
		index = "- **Guides**\n  - **Setup**\n    - [Install](https://w.io/guides/install.md)\n"

		guides = _index_nodes(parse_index(index))[0]

		self.assertEqual(guides["children"][0]["key"], "group:Guides/Setup")

	def test_sibling_groups_with_one_title_get_distinct_keys(self):
		index = (
			"- **Guides**\n  - **Setup**\n    - [One](https://w.io/one.md)\n"
			"- **Guides**\n  - **Setup**\n    - [Two](https://w.io/two.md)\n"
		)

		first, second = _index_nodes(parse_index(index))

		self.assertEqual((first["key"], second["key"]), ("group:Guides", "group:Guides#2"))
		self.assertEqual(second["children"][0]["key"], "group:Guides#2/Setup")

	def test_a_page_listed_twice_is_kept_where_it_first_appears(self):
		index = (
			"- **One**\n  - [Intro](https://w.io/intro.md)\n"
			"- **Two**\n  - [Intro again](https://w.io/intro.md)\n  - [Other](https://w.io/other.md)\n"
		)

		nodes = drop_duplicate_routes(_index_nodes(parse_index(index)))

		self.assertEqual([d["route"] for d in nodes[0]["children"]], ["intro"])
		self.assertEqual([d["route"] for d in nodes[1]["children"]], ["other"])

	def test_api_tree_groups_are_keyed_by_document_name(self):
		group = {
			"name": "1upvra4uuj",
			"title": "Working at Frappe",
			"is_group": 1,
			"route": "handbook/working-at-frappe",
			"is_published": 1,
			"children": [
				{
					"name": "1uq09n9e2l",
					"title": "Introduction",
					"is_group": 0,
					"route": "handbook/policies",
					"is_published": 0,
					"children": [],
				}
			],
		}

		node = _api_node(group)

		self.assertEqual((node["key"], node["route"], node["is_group"]), ("1upvra4uuj", None, True))
		page = node["children"][0]
		self.assertEqual(
			(page["key"], page["route"], page["published"]),
			("handbook/policies", "handbook/policies", False),
		)


class TestWikiPage(FrappeTestCase):
	"""Wiki markup becomes the plain rich text the article editor keeps."""

	def test_callout_becomes_a_titled_blockquote(self):
		page = convert(
			'<aside class="callout callout-tip">\n<div class="callout-header">\n'
			'<span class="callout-icon"><svg width="16"><path d="M8 1"/></svg></span>\n'
			'<span class="callout-title">Remember</span>\n</div>\n'
			'<div class="callout-content"><p>Save <strong>often</strong>.</p></div>\n</aside>'
		)

		self.assertEqual(
			page.html,
			"<blockquote><p><strong>Remember</strong></p><p>Save <strong>often</strong>.</p></blockquote>",
		)

	def test_youtube_iframe_becomes_a_watch_link(self):
		# handbook/scripting-training: the wiki escapes underscores in the src
		page = convert(
			'<p><iframe allow="autoplay" allowfullscreen="" frameborder="0" height="315" '
			'src="https://www.youtube.com/embed/\\_eg2OylJgIw" title="YouTube video player" width="560"></iframe></p>'
			'<iframe src="https://player.example.com/v/1" title="Product tour"></iframe>'
		)

		self.assertEqual(
			page.html,
			'<p><a href="https://www.youtube.com/watch?v=_eg2OylJgIw">Watch the video</a></p>'
			'<p><a href="https://player.example.com/v/1">Product tour</a></p>',
		)

	def test_task_list_becomes_editor_task_items(self):
		page = convert(
			'<ul class="contains-task-list">\n'
			'<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" '
			'disabled="disabled" type="checkbox"> Ship it</li>\n'
			'<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" '
			'type="checkbox"> Tell <em>everyone</em></li>\n</ul>'
		)

		self.assertEqual(
			page.html,
			'<ul data-type="taskList">\n'
			'<li data-checked="true" data-type="taskItem"><p>Ship it</p></li>\n'
			'<li data-checked="false" data-type="taskItem"><p>Tell <em>everyone</em></p></li>\n</ul>',
		)

	def test_loose_task_list_keeps_its_paragraphs(self):
		# markdown-it puts the checkbox in a paragraph when items are blank-line separated
		page = convert(
			'<ul class="contains-task-list">\n<li class="task-list-item">\n'
			'<p><input class="task-list-item-checkbox" checked="checked" type="checkbox"> Ship it</p>\n'
			"<p>Then rest.</p>\n</li>\n</ul>"
		)

		self.assertEqual(
			page.html,
			'<ul data-type="taskList">\n<li data-checked="true" data-type="taskItem">'
			"<p>Ship it</p>\n<p>Then rest.</p>\n</li>\n</ul>",
		)

	def test_body_h1_steps_every_heading_down(self):
		# partner/5-layers-of-maturity-model opens with an h1
		page = convert('<h1 id="five-layers">Five Layers</h1><h2 id="one">One</h2><h6 id="deep">Deep</h6>')

		self.assertEqual(
			page.html,
			'<h2 id="five-layers">Five Layers</h2><h3 id="one">One</h3><h6 id="deep">Deep</h6>',
		)
		# without an h1 the levels are already the editor's
		self.assertEqual(convert('<h2 id="a">A</h2>').html, '<h2 id="a">A</h2>')

	def test_blank_lines_styling_and_classes_are_dropped(self):
		page = convert(
			'<p>Text</p>\n<p class="wiki-blank-line" aria-hidden="true"><br></p>\n<p> </p>'
			'<table border="1" style="width: 100%"><thead><tr><th style="text-align: left">Plan</th></tr>'
			'</thead><tbody><tr><td rowspan="2">Free</td></tr></tbody></table>'
			'<pre><code class="language-bash hljs">ls</code></pre>'
		)

		self.assertEqual(
			page.html,
			'<p>Text</p>\n\n<table border="1"><thead><tr><th>Plan</th></tr></thead>'
			'<tbody><tr><td rowspan="2">Free</td></tr></tbody></table>'
			'<pre><code class="language-bash">ls</code></pre>',
		)

	def test_wiki_files_are_collected_and_made_absolute(self):
		# partner-sop/support-templates: an absolute same-origin image with inline styling
		page = convert(
			'<p><img src="/files/a%20b.png" alt="A"/></p>'
			f'<p><img src="{BASE_URL}/private/files/c.png" alt="C" width="800" style="border: 1px solid"/></p>'
			'<p><img src="https://i.imgur.com/Eqd7NZa.png" alt="External"/></p>'
			'<p><a href="/files/guide.pdf">Guide</a></p>'
		)

		self.assertEqual(
			page.assets,
			{
				f"{BASE_URL}/files/a%20b.png",
				f"{BASE_URL}/private/files/c.png",
				f"{BASE_URL}/files/guide.pdf",
			},
		)
		self.assertIn(
			f'<img alt="C" src="{BASE_URL}/private/files/c.png" width="800"/>',
			page.html,
		)
		self.assertIn('src="https://i.imgur.com/Eqd7NZa.png"', page.html)
		# a file link is an asset, not a page to link to an article
		self.assertNotIn("data-wiki-route", page.html)

		page.replace_urls({f"{BASE_URL}/files/a%20b.png": "/files/a b.png"})
		self.assertIn('<img alt="A" src="/files/a b.png"/>', page.html)

	def test_links_to_wiki_pages_carry_their_route(self):
		page = convert(
			'<p><a href="/handbook/sales%20faq#pricing">Sales</a> '
			'<a href="sibling">Sibling</a> '
			f'<a href="{BASE_URL}/handbook/legal">Legal</a> '
			'<a href="https://example.org/x">External</a> '
			'<a href="mailto:team@example.com">Mail</a> '
			'<a href="#section">Section</a></p>',
			route="handbook/support",
		)

		self.assertEqual(
			page.html,
			f'<p><a data-wiki-route="handbook/sales faq" href="{BASE_URL}/handbook/sales%20faq#pricing">Sales</a> '
			f'<a data-wiki-route="handbook/sibling" href="{BASE_URL}/handbook/sibling">Sibling</a> '
			f'<a data-wiki-route="handbook/legal" href="{BASE_URL}/handbook/legal">Legal</a> '
			'<a href="https://example.org/x">External</a> '
			'<a href="mailto:team@example.com">Mail</a> '
			'<a href="#section">Section</a></p>',
		)

	def test_media_blocks_become_plain_elements(self):
		page = convert(
			'<p><picture><source srcset="/files/dark.png" media="(prefers-color-scheme: dark)">'
			'<img src="https://cdn.example.com/light.png" alt="Shot" /></picture></p>'
			'<div data-type="video-block" data-src="https://cdn.example.com/demo.mp4">'
			'<video src="https://cdn.example.com/demo.mp4" controls preload="metadata"></video></div>'
			'<div class="wiki-pdf-embed" data-type="pdf-block" data-src="https://cdn.example.com/terms.pdf">'
			'<div class="wiki-pdf-card"><div class="wiki-pdf-header"><svg class="wiki-pdf-icon"></svg>'
			'<span class="wiki-pdf-name">Terms.pdf</span></div></div></div>'
			'<pre class="mermaid">graph TD; A--&gt;B</pre>'
		)

		self.assertEqual(
			page.html,
			'<p><img alt="Shot" src="https://cdn.example.com/light.png"/></p>'
			'<video controls="" src="https://cdn.example.com/demo.mp4"></video>'
			'<p><a href="https://cdn.example.com/terms.pdf">Terms.pdf</a></p>'
			'<pre><code class="language-mermaid">graph TD; A--&gt;B</code></pre>',
		)

	def test_page_without_text_or_media_is_empty(self):
		self.assertTrue(convert('<p class="wiki-blank-line"><br></p><p></p>').is_empty)
		self.assertFalse(convert('<p><img src="https://cdn.example.com/a.png"/></p>').is_empty)

	def test_wiki_links_point_at_imported_articles(self):
		html = (
			f'<a data-wiki-route="handbook/sales" href="{BASE_URL}/handbook/sales#pricing">Sales</a>'
			f'<a data-wiki-route="app/todo" href="{BASE_URL}/app/todo">Todo</a>'
		)

		rewritten = rewrite_wiki_links(html, {"handbook/sales": "a1b2c3"})

		self.assertIn('href="/help/articles/a1b2c3#pricing"', rewritten)
		# a route that wasn't imported keeps pointing at the wiki
		self.assertIn(f'href="{BASE_URL}/app/todo"', rewritten)
		# nothing left to change is not a change
		self.assertIsNone(rewrite_wiki_links(rewritten, {"handbook/sales": "a1b2c3"}))
