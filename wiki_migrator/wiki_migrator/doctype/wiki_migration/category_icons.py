"""Picks a helpdesk category icon from the category's name.

Only the icons in the helpdesk's `CATEGORY_ICONS` (shared/src/categoryIcons.ts) render
in its portal, so each one here is from that list, with the words that suggest it.
"""

import re

DEFAULT_ICON = "lucide-folder"
# first icon with a matching word wins, so the more specific ones go first
ICON_KEYWORDS = {
	"lucide-rocket": [
		"getting started",
		"get started",
		"onboarding",
		"launch",
		"quickstart",
		"quick start",
		"introduction",
		"intro",
	],
	"lucide-graduation-cap": ["tutorial", "course", "learning", "training", "academy", "lesson"],
	"lucide-circle-help": ["faq", "question", "help", "how to", "howto"],
	"lucide-life-buoy": ["support", "troubleshooting", "issue", "problem", "debugging", "error"],
	"lucide-lightbulb": ["tip", "idea", "best practice", "concept"],
	"lucide-book-open": ["guide", "manual", "handbook", "documentation", "docs", "reference", "wiki"],
	"lucide-file-text": ["article", "policy", "policies", "terms", "legal", "document", "template"],
	"lucide-sparkles": ["what's new", "release", "release notes", "changelog", "update", "new feature"],
	"lucide-megaphone": ["announcement", "news", "marketing", "campaign", "press"],
	"lucide-credit-card": ["billing", "payment", "credit card", "subscription", "pricing"],
	"lucide-receipt": ["invoice", "receipt", "tax", "refund", "accounting"],
	"lucide-wallet": ["wallet", "finance", "money", "expense", "budget", "payroll"],
	"lucide-shopping-cart": [
		"shop",
		"store",
		"cart",
		"order",
		"checkout",
		"ecommerce",
		"e commerce",
		"selling",
		"sale",
	],
	"lucide-package": ["product", "package", "inventory", "stock", "item", "warehouse"],
	"lucide-truck": ["shipping", "delivery", "logistics", "return", "fulfillment", "fulfilment"],
	"lucide-users": [
		"team",
		"users",
		"people",
		"member",
		"community",
		"partner",
		"hr",
		"employee",
		"human resource",
	],
	"lucide-user": ["account", "profile", "user", "customer"],
	"lucide-building": ["company", "organization", "organisation", "office", "enterprise", "about"],
	"lucide-briefcase": ["business", "work", "career", "job", "hiring", "project"],
	"lucide-mail": ["email", "mail", "newsletter"],
	"lucide-message-circle": ["chat", "message", "conversation", "feedback", "comment"],
	"lucide-phone": ["phone", "call", "telephony", "contact"],
	"lucide-headphones": ["service", "helpdesk", "agent", "ticket"],
	"lucide-bell": ["notification", "alert", "reminder"],
	"lucide-inbox": ["inbox", "request", "queue"],
	"lucide-shield": ["security", "privacy", "compliance", "gdpr", "safety", "permission"],
	"lucide-lock": ["password", "login", "authentication", "sign in", "access"],
	"lucide-key": ["api key", "token", "license", "licence"],
	"lucide-code": ["api", "developer", "code", "sdk", "programming", "engineering", "frontend"],
	"lucide-terminal": ["cli", "command", "terminal", "script", "shell"],
	"lucide-database": ["database", "data", "data import", "import", "export", "backup", "migration"],
	"lucide-server": [
		"server",
		"hosting",
		"deployment",
		"infrastructure",
		"backend",
		"self hosting",
		"self hosted",
	],
	"lucide-cloud": ["cloud", "saas", "sync"],
	"lucide-plug": ["integration", "plugin", "connect", "webhook"],
	"lucide-puzzle": [
		"app",
		"extension",
		"add on",
		"addon",
		"module",
		"customization",
		"customisation",
		"customizing",
	],
	"lucide-zap": ["automation", "workflow", "performance", "shortcut"],
	"lucide-wrench": ["setup", "set up", "installation", "install", "maintenance", "repair", "tool", "fix"],
	"lucide-settings": [
		"setting",
		"configuration",
		"configuring",
		"admin",
		"administration",
		"preference",
		"option",
	],
	"lucide-smartphone": ["mobile", "android", "ios", "phone app"],
	"lucide-monitor": ["desktop", "dashboard", "interface", "screen", "ui"],
	"lucide-chart-bar": [
		"analytics",
		"chart",
		"metric",
		"insight",
		"statistics",
		"kpi",
		"report",
		"reporting",
	],
	"lucide-calendar": ["calendar", "event", "schedule", "booking", "appointment", "holiday", "leave"],
	"lucide-clock": ["time", "sla", "hour", "attendance", "timesheet", "time tracking"],
	"lucide-map-pin": ["location", "address", "map", "region", "country"],
	"lucide-globe": ["website", "web", "language", "translation", "international", "domain", "seo"],
	"lucide-link": ["link", "url", "redirect"],
	"lucide-camera": ["photo", "camera"],
	"lucide-image": ["image", "media", "design", "brand", "asset", "logo"],
	"lucide-video": ["video", "webinar", "recording", "stream"],
	"lucide-tag": ["tag", "label", "discount", "coupon"],
	"lucide-star": ["feature", "highlight", "premium", "popular"],
	"lucide-heart": ["culture", "value", "wellness", "wellbeing", "benefit"],
	"lucide-flag": ["goal", "milestone", "roadmap"],
}


def match_icon(category_name: str) -> str:
	"""The first icon with a word or phrase in the name, singular or plural, so
	"Integrations" matches "integration" but "Appointments" doesn't match "app"."""
	# ponytail: keyword lookup, not fuzzy matching; add words when a name misses
	name = " ".join(re.findall(r"[a-z0-9']+", category_name.lower()))
	for icon, keywords in ICON_KEYWORDS.items():
		if any(re.search(rf"\b{re.escape(keyword)}(s|es)?\b", name) for keyword in keywords):
			return icon
	return DEFAULT_ICON
