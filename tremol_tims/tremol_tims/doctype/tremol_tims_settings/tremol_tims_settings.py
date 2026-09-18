import frappe
from frappe.model.document import Document

class TremolTIMSSettings(Document):
	def get_active_device(self):
		"""Returns (ip, port) for whichever environment is active."""
		if self.environment == "Prod":
			return self.prod_ip, self.prod_port
		return self.dev_ip, self.dev_port
