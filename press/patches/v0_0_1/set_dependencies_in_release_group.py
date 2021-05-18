# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# Proprietary License. See license.txt

from __future__ import unicode_literals
import frappe


def execute():
	frappe.reload_doctype("Release Group Dependency")
	frappe.reload_doctype("Release Group")

	for name in frappe.db.get_all("Release Group", pluck="name"):
		release_group = frappe.get_doc("Release Group", name)
		release_group.extend(
			"dependencies",
			[
				{"dependency": "NVM_VERSION", "version": "0.36.0"},
				{"dependency": "NODE_VERSION", "version": "12.19.0"},
				{"dependency": "PYTHON_VERSION", "version": "3.7"},
				{"dependency": "WKHTMLTOPDF_VERSION", "version": "0.12.5"},
			],
		)
		release_group.db_update_all()
