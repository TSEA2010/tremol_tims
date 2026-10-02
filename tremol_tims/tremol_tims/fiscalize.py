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
	else:
		doc.db_set("is_filed", 1, update_modified=False)
		doc.db_set("etr_invoice_number", log.cu_invoice_number, update_modified=False)
		doc.db_set("cu_link", log.qr_code.strip(), update_modified=False)
		doc.db_set("cu_invoice_date", frappe.utils.nowdate(), update_modified=False)
		_attach_qr_image(doc, log.qr_code.strip(), fieldname="etr_qr_image")
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
			f"CompanyName={_clean(doc.customer_name)[:36]},ClientPINnum=,"
			f"HeadQuarters=,Address=,PostalCodeAndCity=,ExemptionNum=,TraderSystemInvNum={doc.name[:15]})"
		)
		opened = True

		for item in doc.items:
			rate = get_item_vat_rate(item.item_tax_template)
			vat_class = get_vat_class(item.item_tax_template, rate)
			call(
				"SellPLUfromExtDB("
				f"NamePLU={_clean(item.item_name)[:36]},OptionVATClass={vat_class},"
				f"Price={flt(item.rate):.2f},MeasureUnit={(item.uom or 'pcs')[:3]},"
				f"HSCode=,HSName=,VATGrRate={rate:.2f},"
				f"Quantity={flt(item.qty)},DiscAddP=)"
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

def _attach_qr_image(doc, url, fieldname):
	import io
	import qrcode

	img = qrcode.make(url)
	buf = io.BytesIO()
	img.save(buf, format="PNG")

	file_doc = frappe.get_doc({
		"doctype": "File",
		"file_name": f"{doc.name}_qr.png",
		"attached_to_doctype": doc.doctype,
		"attached_to_name": doc.name,
		"attached_to_field": fieldname,
		"content": buf.getvalue(),
		"is_private": 0,
	})
	file_doc.insert(ignore_permissions=True)

	doc.db_set(fieldname, file_doc.file_url, update_modified=False)