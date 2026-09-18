"""
Transport client for Tremol ZFPLabServer's DirectAPI.

Wraps the raw HTTP calls with: a Redis lock (the CU handles one client at
a time), an explicit timeout (FP_core.py's urlopen has none and will hang
a worker forever), and XML response parsing.
"""

import time
import frappe
import requests
import xml.etree.ElementTree as ET

LOCK_KEY = "tremol_tims:device_lock"
LOCK_TIMEOUT = 30  # seconds — max time a single fiscal op should hold the lock


class TremolError(Exception):
	def __init__(self, code, err_type, message):
		self.code = code
		self.err_type = err_type
		self.message = message
		super().__init__(f"[{code}] {err_type}: {message}")


class TremolClient:
	def __init__(self):
		settings = frappe.get_single("Tremol TIMS Settings")
		self.base_url = (settings.zfplabserver_url or "http://localhost:4444").rstrip("/")
		self.timeout = settings.request_timeout or 15
		self.password = settings.get_password("password")
		self.ip, self.port = settings.get_active_device()
		if not self.ip:
			frappe.throw(f"No IP configured for the active environment ({settings.environment}) in Tremol TIMS Settings")

	def _lock(self):
		client = frappe.cache()
		acquired = client.set(LOCK_KEY, "1", nx=True, ex=LOCK_TIMEOUT)
		if not acquired:
			for _ in range(10):
				time.sleep(1)
				if client.set(LOCK_KEY, "1", nx=True, ex=LOCK_TIMEOUT):
					return
			frappe.throw("Tremol device is busy with another transaction. Try again shortly.")

	def _unlock(self):
		frappe.cache().delete_value(LOCK_KEY)

	def _call(self, command):
		"""command e.g. 'ReadStatus()' or 'Settings(com=,baud=,tcp=1,ip=...,port=...,password=...)'"""
		url = f"{self.base_url}/{command}"
		try:
			resp = requests.get(url, timeout=self.timeout)
		except requests.exceptions.Timeout:
			raise TremolError("TIMEOUT", "ClientTimeout", f"No response from ZFPLabServer within {self.timeout}s")
		except requests.exceptions.ConnectionError as e:
			raise TremolError("CONN_ERROR", "ClientConnectionError", str(e))

		root = ET.fromstring(resp.text)
		code = root.attrib.get("Code")
		if code != "0":
			err = root.find("Err")
			err_type = err.attrib.get("Type") if err is not None else "Unknown"
			msg_el = err.find("Message") if err is not None else None
			message = msg_el.text if msg_el is not None else resp.text
			raise TremolError(code, err_type, message)
		return root

	def ensure_connected(self):
		cmd = f"Settings(com=,baud=,tcp=1,ip={self.ip},port={self.port},password={self.password})"
		return self._call(cmd)

	def read_status(self):
		return self._call("ReadStatus()")

	def cancel_receipt(self):
		return self._call("CancelReceipt()")

	def run_locked(self, fn, *args, **kwargs):
		self._lock()
		try:
			self.ensure_connected()
			return fn(self, *args, **kwargs)
		finally:
			try:
				status = self.read_status()
				opened = status.find(".//Res[@Name='Opened_Fiscal_Receipt']")
				if opened is not None and opened.attrib.get("Value") == "1":
					self.cancel_receipt()
			except Exception:
				frappe.log_error(title="Tremol TIMS: cleanup check failed")
			self._unlock()
