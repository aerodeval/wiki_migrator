import frappe


def execute():
	"""Items from before `synced_on` existed: their `modified` ranked a route's migrations."""
	item = frappe.qb.DocType("Wiki Migration Item")
	(
		frappe.qb.update(item)
		.set(item.synced_on, item.modified)
		.where(item.article.isnotnull() & (item.article != ""))
		.where(item.synced_on.isnull())
		.run()
	)
