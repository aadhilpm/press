# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt
from __future__ import annotations

import json
import time
from enum import Enum
from typing import TYPE_CHECKING

import frappe
from frappe.core.utils import find
from frappe.model.document import Document

from press.press.doctype.ansible_console.ansible_console import AnsibleAdHoc

if TYPE_CHECKING:
	from press.infrastructure.doctype.virtual_machine_migration_step.virtual_machine_migration_step import (
		VirtualMachineMigrationStep,
	)


StepStatus = Enum("StepStatus", ["Pending", "Running", "Success", "Failure"])


class VirtualMachineMigration(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from press.infrastructure.doctype.virtual_machine_migration_mount.virtual_machine_migration_mount import (
			VirtualMachineMigrationMount,
		)
		from press.infrastructure.doctype.virtual_machine_migration_step.virtual_machine_migration_step import (
			VirtualMachineMigrationStep,
		)
		from press.infrastructure.doctype.virtual_machine_migration_volume.virtual_machine_migration_volume import (
			VirtualMachineMigrationVolume,
		)

		copied_virtual_machine: DF.Link | None
		duration: DF.Duration | None
		end: DF.Datetime | None
		machine_type: DF.Data
		mounts: DF.Table[VirtualMachineMigrationMount]
		name: DF.Int | None
		new_plan: DF.Link | None
		parsed_devices: DF.Code | None
		raw_devices: DF.Code | None
		start: DF.Datetime | None
		status: DF.Literal["Pending", "Running", "Success", "Failure"]
		steps: DF.Table[VirtualMachineMigrationStep]
		virtual_machine: DF.Link
		virtual_machine_image: DF.Link
		volumes: DF.Table[VirtualMachineMigrationVolume]
	# end: auto-generated types

	def before_insert(self):
		self.validate_aws_only()
		self.validate_existing_migration()
		self.add_steps()
		self.add_volumes()
		self.create_machine_copy()
		self.set_new_plan()

	def after_insert(self):
		self.add_devices()
		self.set_default_mounts()

	def add_devices(self):
		command = "lsblk --json --output name,type,uuid,mountpoint,size,label,fstype"
		inventory = f"{self.virtual_machine},"
		ansible = AnsibleAdHoc(sources=inventory)
		output = ansible.run(command, self.name)[0]["output"]

		"""Sample output of the command
		{
			"blockdevices": [
				{"name":"loop0", "type":"loop", "uuid":null, "mountpoint":"/snap/amazon-ssm-agent/9882", "size":"22.9M", "label":null, "fstype":null},
				{"name":"loop1", "type":"loop", "uuid":null, "mountpoint":"/snap/core20/2437", "size":"59.5M", "label":null, "fstype":null},
				{"name":"loop2", "type":"loop", "uuid":null, "mountpoint":"/snap/core22/1666", "size":"68.9M", "label":null, "fstype":null},
				{"name":"loop3", "type":"loop", "uuid":null, "mountpoint":"/snap/snapd/21761", "size":"33.7M", "label":null, "fstype":null},
				{"name":"loop4", "type":"loop", "uuid":null, "mountpoint":"/snap/lxd/29631", "size":"92M", "label":null, "fstype":null},
				{"name":"nvme0n1", "type":"disk", "uuid":null, "mountpoint":null, "size":"25G", "label":null, "fstype":null,
					"children": [
						{"name":"nvme0n1p1", "type":"part", "uuid":"b8932e17-9ed7-47b7-8bf3-75ff6669e018", "mountpoint":"/", "size":"24.9G", "label":"cloudimg-rootfs", "fstype":"ext4"},
						{"name":"nvme0n1p15", "type":"part", "uuid":"7569-BCF0", "mountpoint":"/boot/efi", "size":"99M", "label":"UEFI", "fstype":"vfat"}
					]
				},
				{"name":"nvme1n1", "type":"disk", "uuid":"41527fb0-f6e9-404e-9dba-0451dfa2195e", "mountpoint":"/opt/volumes/mariadb", "size":"10G", "label":null, "fstype":"ext4"}
			]
		}"""
		devices = json.loads(output)["blockdevices"]
		self.raw_devices = json.dumps(devices, indent=2)
		self.parsed_devices = json.dumps(self._parse_devices(devices), indent=2)
		self.save()

	def _parse_devices(self, devices):
		parsed = []
		for device in devices:
			# We only care about disks and partitions
			if device["type"] != "disk":
				continue

			# Disk has partitions. e.g root volume
			if "children" in device:
				for partition in device["children"]:
					if partition["type"] == "part":
						parsed.append(partition)
			else:
				# Single partition. e.g data volume
				parsed.append(device)
		return parsed

	def set_default_mounts(self):
		# Set root partition from old machine as the data partition in the new machine

		if self.mounts:
			# We've already set the mounts
			return

		parsed_devices = json.loads(self.parsed_devices)
		device = find(parsed_devices, lambda x: x["mountpoint"] == "/")
		if not device:
			# No root volume found
			return

		server_type = self.machine.get_server().doctype
		if server_type == "Server":
			target_mount_point = "/opt/volumes/benches"
		elif server_type == "Database Server":
			target_mount_point = "/opt/volumes/mariadb"
		else:
			# Data volumes are only supported for Server and Database Server
			return

		self.append(
			"mounts",
			{
				"uuid": device["uuid"],
				"source_mount_point": device.mountpoint,
				"target_mount_point": target_mount_point,
			},
		)
		self.save()

	def add_steps(self):
		for step in self.migration_steps:
			step.update({"status": "Pending"})
			self.append("steps", step)

	def add_volumes(self):
		# Prepare volumes to attach to new machine
		for index, volume in enumerate(self.machine.volumes):
			device_name_index = chr(ord("f") + index)
			self.append(
				"volumes",
				{
					"status": "Unattached",
					"volume_id": volume.volume_id,
					# This is the device name that will be used in the new machine
					# Only needed for the attach_volumes call
					"device_name": f"/dev/sd{device_name_index}",
				},
			)

	def create_machine_copy(self):
		# Create a copy of the current machine
		# So we don't lose the instance ids
		self.copied_virtual_machine = f"{self.virtual_machine}-copy"

		if frappe.db.exists("Virtual Machine", self.copied_virtual_machine):
			frappe.delete_doc("Virtual Machine", self.copied_virtual_machine)

		copied_machine = frappe.copy_doc(self.machine)
		copied_machine.insert(set_name=self.copied_virtual_machine)

	def set_new_plan(self):
		server = self.machine.get_server()
		old_plan = frappe.get_doc("Server Plan", server.plan)
		matching_plans = frappe.get_all(
			"Server Plan",
			{
				"enabled": True,
				"server_type": old_plan.server_type,
				"cluster": old_plan.cluster,
				"instance_type": self.machine_type,
				"premium": old_plan.premium,
			},
			pluck="name",
			limit=1,
		)
		if matching_plans:
			self.new_plan = matching_plans[0]

	def validate_aws_only(self):
		if self.machine.cloud_provider != "AWS EC2":
			frappe.throw("This feature is only available for AWS EC2")

	def validate_existing_migration(self):
		if existing := frappe.get_all(
			self.doctype,
			{
				"status": ("in", ["Pending", "Running"]),
				"virtual_machine": self.virtual_machine,
				"name": ("!=", self.name),
			},
			pluck="status",
			limit=1,
		):
			frappe.throw(f"An existing migration is already {existing[0].lower()}.")

	@property
	def machine(self):
		return frappe.get_doc("Virtual Machine", self.virtual_machine)

	@property
	def copied_machine(self):
		return frappe.get_doc("Virtual Machine", self.copied_virtual_machine)

	@property
	def migration_steps(self):
		return [
			{
				"step": self.update_partition_labels.__doc__,
				"method": self.update_partition_labels.__name__,
			},
			{
				"step": self.stop_machine.__doc__,
				"method": self.stop_machine.__name__,
				"wait_for_completion": True,
			},
			{
				"step": self.wait_for_machine_to_stop.__doc__,
				"method": self.wait_for_machine_to_stop.__name__,
				"wait_for_completion": True,
			},
			{
				"step": self.disable_delete_on_termination_for_all_volumes.__doc__,
				"method": self.disable_delete_on_termination_for_all_volumes.__name__,
			},
			{
				"step": self.terminate_previous_machine.__doc__,
				"method": self.terminate_previous_machine.__name__,
				"wait_for_completion": True,
			},
			{
				"step": self.wait_for_previous_machine_to_terminate.__doc__,
				"method": self.wait_for_previous_machine_to_terminate.__name__,
				"wait_for_completion": True,
			},
			{
				"step": self.reset_virtual_machine_attributes.__doc__,
				"method": self.reset_virtual_machine_attributes.__name__,
			},
			{
				"step": self.provision_new_machine.__doc__,
				"method": self.provision_new_machine.__name__,
			},
			{
				"step": self.wait_for_machine_to_start.__doc__,
				"method": self.wait_for_machine_to_start.__name__,
				"wait_for_completion": True,
			},
			{
				"step": self.attach_volumes.__doc__,
				"method": self.attach_volumes.__name__,
			},
			{
				"step": self.wait_for_machine_to_be_accessible.__doc__,
				"method": self.wait_for_machine_to_be_accessible.__name__,
				"wait_for_completion": True,
			},
			{
				"step": self.update_plan.__doc__,
				"method": self.update_plan.__name__,
			},
		]

	def update_partition_labels(self) -> StepStatus:
		"Update partition labels"
		# Ubuntu images have labels for root (cloudimg-rootfs) and efi (UEFI) partitions
		# Remove these labels from the old volume
		# So the new machine doesn't mount these as root or efi partitions
		# Important: Update fstab so we can still boot the old machine
		parsed_devices = json.loads(self.parsed_devices)
		for device in parsed_devices:
			old_label = device["label"]
			if not old_label:
				continue

			labeler = {"ext4": "e2label", "vfat": "fatlabel"}[device["fstype"]]
			new_label = {"cloudimg-rootfs": "old-rootfs", "UEFI": "OLD-UEFI"}[old_label]
			inventory = f"{self.virtual_machine},"
			commands = [
				# Reference: https://wiki.archlinux.org/title/Persistent_block_device_naming#by-label
				f"{labeler} /dev/{device['name']} {new_label}",
				f"sed -i 's/LABEL\\={old_label}/LABEL\\={new_label}/g' /etc/fstab",  # Ansible implementation quirk
			]
			if old_label == "UEFI":
				# efi mounts have dirty bit set. This resets it.
				commands.append(f"fsck -a /dev/{device['name']}")

			for command in commands:
				result = AnsibleAdHoc(sources=inventory).run(command, self.name)
				if result[0]["status"] != "Success":
					self.add_comment(text=f"Error updating partition labels: {result[0]}")
					return StepStatus.Failure
		return StepStatus.Success

	def stop_machine(self) -> StepStatus:
		"Stop machine"
		machine = self.machine
		machine.sync()
		if machine.status == "Stopped":
			return StepStatus.Success
		if machine.status == "Pending":
			return StepStatus.Pending
		machine.stop()
		return StepStatus.Success

	def wait_for_machine_to_stop(self) -> StepStatus:
		"Wait for machine to stop"
		# We need to make sure the machine is stopped before we proceed
		machine = self.machine
		machine.sync()
		if machine.status == "Stopped":
			return StepStatus.Success
		return StepStatus.Pending

	def disable_delete_on_termination_for_all_volumes(self) -> StepStatus:
		"Disable Delete-on-Termination for all volumes"
		# After this we can safely terminate the instance without losing any data
		copied_machine = self.copied_machine
		if copied_machine.volumes:
			copied_machine.disable_delete_on_termination_for_all_volumes()
		return StepStatus.Success

	def terminate_previous_machine(self) -> StepStatus:
		"Terminate previous machine"
		copied_machine = self.copied_machine
		if copied_machine.status == "Terminated":
			return StepStatus.Success
		if copied_machine.status == "Pending":
			return StepStatus.Pending

		copied_machine.disable_termination_protection()
		copied_machine.reload()
		copied_machine.terminate()
		return StepStatus.Success

	def wait_for_previous_machine_to_terminate(self) -> StepStatus:
		"Wait for previous machine to terminate"
		# Private ip address is released when the machine is terminated
		copied_machine = self.copied_machine
		copied_machine.sync()
		if copied_machine.status == "Terminated":
			return StepStatus.Success
		return StepStatus.Pending

	def reset_virtual_machine_attributes(self) -> StepStatus:
		"Reset virtual machine attributes"
		machine = self.machine
		machine.instance_id = None
		machine.public_ip_address = None
		machine.volumes = []

		# Set new machine image and machine type
		machine.virtual_machine_image = self.virtual_machine_image
		machine.machine_type = self.machine_type
		machine.save()
		return StepStatus.Success

	def provision_new_machine(self) -> StepStatus:
		"Provision new machine"
		# Create new machine in place. So we retain Name, IP etc.
		self.machine._provision_aws()
		return StepStatus.Success

	def wait_for_machine_to_start(self) -> StepStatus:
		"Wait for new machine to start"
		# We can't attach volumes to a machine that is not running
		machine = self.machine
		machine.sync()
		if machine.status == "Running":
			return StepStatus.Success
		return StepStatus.Pending

	def attach_volumes(self) -> StepStatus:
		"Attach volumes"
		machine = self.machine
		for volume in self.volumes:
			try:
				machine.client().attach_volume(
					InstanceId=machine.instance_id,
					Device=volume.device_name,
					VolumeId=volume.volume_id,
				)
				volume.status = "Attached"
			except Exception as e:
				self.add_comment(text=f"Error attaching volume {volume.volume_id}: {e}")
		machine.sync()
		return StepStatus.Success

	def wait_for_machine_to_be_accessible(self) -> StepStatus:
		"Wait for machine to be accessible"
		server = self.machine.get_server()
		server.ping_ansible()

		plays = frappe.get_all(
			"Ansible Play",
			{"server": server.name, "play": "Ping Server"},
			["status"],
			order_by="creation desc",
			limit=1,
		)
		if plays and plays[0].status == "Success":
			return StepStatus.Success
		return StepStatus.Pending

	def update_plan(self) -> StepStatus:
		"Update plan"
		if self.new_plan:
			server = self.machine.get_server()
			plan = frappe.get_doc("Server Plan", self.new_plan)
			server._change_plan(plan)
		return StepStatus.Success

	@frappe.whitelist()
	def execute(self):
		self.status = "Running"
		self.start = frappe.utils.now_datetime()
		self.save()
		self.next()

	def fail(self) -> None:
		self.status = "Failure"
		for step in self.steps:
			if step.status == "Pending":
				step.status = "Skipped"
		self.end = frappe.utils.now_datetime()
		self.duration = (self.end - self.start).total_seconds()
		self.save()

	def succeed(self) -> None:
		self.status = "Success"
		self.end = frappe.utils.now_datetime()
		self.duration = (self.end - self.start).total_seconds()
		self.save()

	@frappe.whitelist()
	def next(self, arguments=None) -> None:
		self.status = "Running"
		self.save()
		next_step = self.next_step

		if not next_step:
			# We've executed everything
			self.succeed()
			return

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"execute_step",
			step_name=next_step.name,
			enqueue_after_commit=True,
		)

	@frappe.whitelist()
	def force_continue(self) -> None:
		# Mark all failed and skipped steps as pending
		for step in self.steps:
			if step.status in ("Failure", "Skipped"):
				step.status = "Pending"
		self.next()

	@frappe.whitelist()
	def force_fail(self) -> None:
		# Mark all pending steps as failure
		for step in self.steps:
			if step.status == "Pending":
				step.status = "Failure"
		self.status = "Failure"

	@property
	def next_step(self) -> VirtualMachineMigrationStep | None:
		for step in self.steps:
			if step.status == "Pending":
				return step
		return None

	@frappe.whitelist()
	def execute_step(self, step_name):
		step = self.get_step(step_name)

		if not step.start:
			step.start = frappe.utils.now_datetime()
		step.status = "Running"
		try:
			result = getattr(self, step.method)()
			step.status = result.name
			if step.wait_for_completion:
				step.attempts = step.attempts + 1
				if result == StepStatus.Pending:
					# Wait some time before the next run
					time.sleep(1)
		except Exception:
			step.status = "Failure"
			step.traceback = frappe.get_traceback(with_context=True)

		step.end = frappe.utils.now_datetime()
		step.duration = (step.end - step.start).total_seconds()

		if step.status == "Failure":
			self.fail()
		else:
			self.next()

	def get_step(self, step_name) -> VirtualMachineMigrationStep | None:
		for step in self.steps:
			if step.name == step_name:
				return step
		return None
