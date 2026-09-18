import frappe
from frappe.model.document import Document

class TremolFiscalLog(Document):
	pass


def get_existing_log(sales_invoice):
	"""Idempotency check: if this invoice is already fiscalized, return the log, don't re-send."""
	name = frappe.db.exists("Tremol Fiscal Log", {"sales_invoice": sales_invoice, "status": "Success"})
	if name:
		return frappe.get_doc("Tremol Fiscal Log", name)
	return None
