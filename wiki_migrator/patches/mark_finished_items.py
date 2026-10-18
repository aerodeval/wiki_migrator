import frappe


def execute():
	"""Items from before `finished` existed: the status said whether their article was done."""
	item = frappe.qb.DocType("Wiki Migration Item")
	(
		frappe.qb.update(item)
		.set(item.finished, 1)
		.where(item.status.isin(["Imported", "Updated", "Skipped"]))
		.where(item.article.isnotnull() & (item.article != ""))
		.run()
	)
