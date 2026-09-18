"""
Sales Invoice on_submit -> Tremol fiscal receipt.

Sequence per runbook section 08:
  OpenInvoiceWithFreeCustomerData -> SellPLUfromExtDB (per line) ->
  ReadVATrates -> ReadCurrentReceiptInfo -> CloseReceipt -> ReadDateTime
On any failure between open and close: CancelReceipt (handled by
TremolClient.run_locked's cleanup, plus explicit cancel here on exception).
"""

import frappe
from frappe.utils import flt
from tremol_tims.tremol_tims.utils.transport import TremolClient, TremolError
from tremol_tims.tremol_tims.doctype.tremol_fiscal_log.tremol_fiscal_log import get_existing_log

# Rate -> class is only safe for A/B/D. C (zero-rated) and E (exempt) are
# both 0% -- per runbook section 09, this MUST key off Item Tax Template,
# not rate. This is a first-pass default: exempt only detected by template
# name containing "exempt". Refine once real templates are confirmed.
RATE_TO_CLASS = {16: "A", 8: "B", 0: "C"}


def get_vat_class(item_tax_template, rate):
	if item_tax_template and "exempt" in item_tax_template.lower():
		return "E"
	return RATE_TO_CLASS.get(int(rate), "D")


def get_item_vat_rate(item_tax_template):
	if not item_tax_template:
		return 16
	template = frappe.get_cached_doc("Item Tax Template", item_tax_template)
	if template.taxes:
		return flt(template.taxes[0].tax_rate)
	return 16


def on_submit_sales_invoice(doc, method=None):
	if get_existing_log(doc.name):
		return  # idempotent -- already fiscalized, never re-send

	log = frappe.get_doc({
		"doctype": "Tremol Fiscal Log",
		"sales_invoice": doc.name,
		"status": "Pending",
		"environment": frappe.get_single("Tremol TIMS Settings").environment,
	})
	log.insert(ignore_permissions=True)

	trace = []
	try:
		client = TremolClient()
		result = client.run_locked(_fiscalize_invoice, doc, trace)
		log.status = "Success"
		log.cu_invoice_number = result.get("cu_invoice_number")
		log.qr_code = result.get("qr_code")
	except TremolError as e:
		log.status = "Failed"
		log.error_message = str(e)
		frappe.log_error(title="Tremol TIMS fiscalization failed", message=str(e))
	finally:
		log.raw_response = "\n\n".join(trace)
		log.save(ignore_permissions=True)


def _fiscalize_invoice(client, doc, trace):
	opened = False

	def call(command):
		trace.append(f">>> {command}")
		try:
			resp = client._call(command)
			trace.append(f"<<< {_xml(resp)}")
			return resp
		except TremolError as e:
			trace.append(f"<<< ERROR: {e}")
			raise

	try:
		open_resp = call(
			"OpenInvoiceWithFreeCustomerData("
			f"OperNum=1,OperPass=,OptionInvoicePrintType=1,"
			f"CompanyName={_clean(doc.customer_name)[:36]},ClientPINnum=,"
			f"HeadQuarters=,Address=,PostalCodeAndCity=,ExemptionNum=)"
		)
		opened = True

		for item in doc.items:
			rate = get_item_vat_rate(item.item_tax_template)
			vat_class = get_vat_class(item.item_tax_template, rate)
			call(
				"SellPLUfromExtDB("
				f"NamePLU={_clean(item.item_name)[:36]},OptionVATClass1={vat_class},"
				f"Price={flt(item.rate):.2f},MeasureUnit={(item.uom or 'pcs')[:3]},"
				f"HSCode=,HSName=,Quantity={flt(item.qty)},DiscAddP=,DiscAddV=)"
			)

		call("ReadVATrates()")
		call("ReadCurrentReceiptInfo()")

		close_resp = call("CloseReceipt()")
		opened = False

		call("ReadDateTime()")

		cu_invoice_number = _extract(close_resp, "InvoiceNum")
		qr_code = _extract(close_resp, "QRcode")

		return {"cu_invoice_number": cu_invoice_number, "qr_code": qr_code}
	except Exception:
		if opened:
			try:
				call("CancelReceipt()")
			except Exception:
				pass
		raise


def _clean(text):
	return "".join(ch for ch in (text or "") if ch.isalnum() or ch == " ")


def _xml(element):
	import xml.etree.ElementTree as ET
	return ET.tostring(element, encoding="unicode")


def _extract(root, name):
	el = root.find(f".//Res[@Name='{name}']")
	return el.attrib.get("Value") if el is not None else None
