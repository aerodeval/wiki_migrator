"""HTTP plumbing the wiki and helpdesk clients share.

A client's key only goes to its own origin. Only the connect probe, which carries no
key, follows redirects: it is how a client finds its site's canonical address.
"""

from urllib.parse import urlsplit

import frappe
import requests
from frappe import _
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "Frappe Wiki Migrator"
TIMEOUT = 30
RETRY = Retry(
	total=3,
	backoff_factor=1,
	status_forcelist=[429, 500, 502, 503, 504],
	raise_on_status=False,
)


def new_session() -> requests.Session:
	"""A session that names itself and retries rate limits, server errors and drops."""
	session = requests.Session()
	session.headers["User-Agent"] = USER_AGENT
	session.mount("https://", HTTPAdapter(max_retries=RETRY))
	session.mount("http://", HTTPAdapter(max_retries=RETRY))
	return session


def send(
	session: requests.Session,
	method: str,
	url: str,
	origin: str,
	auth: str | None,
	probe: bool = False,
	**kwargs,
) -> requests.Response:
	"""`method` `url` for the site at `origin`; a request that can't complete is thrown
	as a readable error."""
	headers = {}
	if auth and not probe and get_origin(url) == origin:
		headers["Authorization"] = auth
	try:
		return session.request(method, url, headers=headers, timeout=TIMEOUT, allow_redirects=probe, **kwargs)
	except requests.exceptions.SSLError:
		frappe.throw(_("Couldn't verify the SSL certificate of {0}.").format(origin))
	except requests.exceptions.Timeout:
		frappe.throw(_("{0} took too long to respond. Try again later.").format(origin))
	except requests.exceptions.RequestException:
		frappe.throw(_("Couldn't reach {0}. Check the URL and try again.").format(origin))


def get_origin(url: str) -> str:
	parts = urlsplit(url)
	return f"{parts.scheme}://{parts.netloc}"


def normalize_url(url: str) -> str:
	"""`url` with a scheme, cut down to its origin; throws when it isn't a web address."""
	url = (url or "").strip()
	if "://" not in url:
		url = f"https://{url}"
	parts = urlsplit(url)
	if parts.scheme not in ("http", "https") or not parts.hostname:
		frappe.throw(_("Enter a valid URL, like https://site.example.com."))
	return get_origin(url)
