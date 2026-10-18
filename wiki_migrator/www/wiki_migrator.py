import frappe

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/wiki-migrator"
		raise frappe.Redirect

	# Read by frappe-ui's jinjaBootData script in the built index.html.
	context.boot = {"csrf_token": frappe.sessions.get_csrf_token(), "site_name": frappe.local.site}
	# GET requests roll back; commit so the new CSRF token persists in the session.
	frappe.db.commit()
