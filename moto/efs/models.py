from __future__ import unicode_literals

import json
import string
import time
import random
from copy import deepcopy

from boto3 import Session
from moto.core import BaseBackend, CloudFormationModel, ACCOUNT_ID
from moto.core.utils import (
    underscores_to_camelcase,
    camelcase_to_underscores,
    get_random_hex,
)
from moto.ec2 import ec2_backends
from moto.efs.exceptions import (
    FileSystemAlreadyExists,
    BadRequest,
    FileSystemNotFound,
    MountTargetConflict,
)


class FileSystem(CloudFormationModel):
    def __init__(
        self,
        creation_token,
        file_system_id,
        performance_mode="generalPurpose",
        encrypted=False,
        kms_key_id=None,
        throughput_mode="bursting",
        provisioned_throughput_in_mibps=None,
        availability_zone_name=None,
        backup=False,
        lifecycle_policies=None,
        file_system_policy=None,
        tags=None,
    ):
        if availability_zone_name:
            backup = True
        if kms_key_id and not encrypted:
            raise BadRequest('If kms_key_id given, "encrypted" must be True.')

        # Save given parameters
        self.creation_token = creation_token
        self.performance_mode = performance_mode
        self.encrypted = encrypted
        self.kms_key_id = kms_key_id
        self.throughput_mode = throughput_mode
        self.provisioned_throughput_in_mibps = provisioned_throughput_in_mibps
        self.availability_zone_name = availability_zone_name
        self.backup = backup
        self.lifecycle_policies = lifecycle_policies
        self.file_system_policy = file_system_policy

        # Validate tag structure.
        if tags is None:
            self.tags = []
        else:
            if (
                not isinstance(tags, list)
                or not all(isinstance(tag, dict) for tag in tags)
                or not all(set(tag.keys()) == {"Key", "Value"} for tag in tags)
            ):
                raise ValueError("Invalid tags: {}".format(tags))
            else:
                self.tags = tags

        # Generate AWS-assigned parameters
        self.file_system_id = file_system_id
        self.file_system_arn = "arn:aws:elasticfilesystem:{region}:{user_id}:file-system/{file_system_id}".format(
            region=None, user_id=None, file_system_id=self.file_system_id
        )
        self.creation_time = time.time()
        self.owner_id = ACCOUNT_ID

        # Initialize some state parameters
        self.life_cycle_state = "available"
        self._mount_targets = {}
        self._size_value = 0

    @property
    def size_in_bytes(self):
        return {
            "Value": self._size_value,
            "ValueInIA": 0,
            "ValueInStandard": self._size_value,
            "Timestamp": time.time(),
        }

    @property
    def physical_resource_id(self):
        return self.file_system_id

    @property
    def number_of_mount_targets(self):
        return len(self._mount_targets)

    def info_json(self):
        ret = {
            underscores_to_camelcase(k.capitalize()): v
            for k, v in self.__dict__.items()
            if not k.startswith("_")
        }
        ret["SizeInBytes"] = self.size_in_bytes
        ret["NumberOfMountTargets"] = self.number_of_mount_targets
        return ret

    def add_mount_target(self, subnet, mount_target):
        self._mount_targets[subnet.availability_zone] = mount_target

    def has_mount_target(self, subnet):
        return subnet.availability_zone in self._mount_targets

    def iter_mount_targets(self):
        for mt in self._mount_targets.values():
            yield mt

    @staticmethod
    def cloudformation_name_type():
        return

    @staticmethod
    def cloudformation_type():
        return "AWS::EFS::FileSystem"

    @classmethod
    def create_from_cloudformation_json(
        cls, resource_name, cloudformation_json, region_name
    ):
        # https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-efs-filesystem.html
        props = deepcopy(cloudformation_json["Properties"])
        props = {camelcase_to_underscores(k): v for k, v in props.items()}
        if "file_system_tags" in props:
            props["tags"] = props.pop("file_system_tags")
        if "backup_policy" in props:
            if "status" not in props["backup_policy"]:
                raise ValueError("BackupPolicy must be of type BackupPolicy.")
            status = props.pop("backup_policy")["status"]
            if status not in ["ENABLED", "DISABLED"]:
                raise ValueError('Invalid status: "{}".'.format(status))
            props["backup"] = status == "ENABLED"
        if "bypass_policy_lockout_safety_check" in props:
            raise ValueError(
                "BypassPolicyLockoutSafetyCheck not currently "
                "supported by AWS Cloudformation."
            )

        return efs_backends[region_name].create_file_system(resource_name, **props)

    @classmethod
    def update_from_cloudformation_json(
        cls, original_resource, new_resource_name, cloudformation_json, region_name
    ):
        return

    @classmethod
    def delete_from_cloudformation_json(
        cls, resource_name, cloudformation_json, region_name
    ):
        return


class MountTarget(CloudFormationModel):
    def __init__(self, file_system, subnet, ip_address, security_groups):
        # Set the simple given parameters.
        self.file_system_id = file_system.file_system_id
        self._file_system = file_system
        self.subnet_id = subnet.id
        self._subnet = subnet
        self.vpc_id = subnet.vpc_id
        self.security_groups = security_groups

        # Get an IP address if needed, otherwise validate the one we're given.
        if ip_address is None:
            ip_address = subnet.get_available_subnet_ip(self)
        else:
            subnet.request_ip(ip_address, self)
        self.ip_address = ip_address

        # Init non-user-assigned values.
        self.owner_id = ACCOUNT_ID
        self.mounted_target_id = "fsmt-{}".format(get_random_hex())
        self.life_cycle_state = "available"
        self.network_interface_id = None
        self.availability_zone_id = subnet.availability_zone_id
        self.availability_zone_name = subnet.availability_zone

    def set_network_interface(self, network_interface):
        self.network_interface_id = network_interface.id

    def info_json(self):
        ret = {
            underscores_to_camelcase(k.capitalize()): v
            for k, v in self.__dict__.items()
            if not k.startswith("_")
        }
        return ret

    @property
    def physical_resource_id(self):
        return self.mounted_target_id

    @staticmethod
    def cloudformation_name_type():
        pass

    @staticmethod
    def cloudformation_type():
        return "AWS::EFS::MountTarget"

    @classmethod
    def create_from_cloudformation_json(
        cls, resource_name, cloudformation_json, region_name
    ):
        # https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-efs-mounttarget.html
        props = deepcopy(cloudformation_json["Properties"])
        props = {camelcase_to_underscores(k): v for k, v in props.items()}
        return efs_backends[region_name].create_mount_target(**props)

    @classmethod
    def update_from_cloudformation_json(
        cls, original_resource, new_resource_name, cloudformation_json, region_name
    ):
        pass

    @classmethod
    def delete_from_cloudformation_json(
        cls, resource_name, cloudformation_json, region_name
    ):
        pass


class EFSBackend(BaseBackend):
    def __init__(self, region_name=None):
        super(EFSBackend, self).__init__()
        self.region_name = region_name
        self.creation_tokens = set()
        self.file_systems_by_id = {}
        self.mount_targets_by_id = {}
        self.next_markers = {}

    def reset(self):
        # preserve region
        region_name = self.region_name
        self.__dict__ = {}
        self.__init__(region_name)

    def _mark_description(self, corpus, max_items):
        if max_items < len(corpus):
            new_corpus = corpus[max_items:]
            next_marker = str(hash(json.dumps(new_corpus)))
            self.next_markers[next_marker] = new_corpus
        else:
            next_marker = None
        return next_marker

    @property
    def ec2_backend(self):
        return ec2_backends[self.region_name]

    def create_file_system(self, creation_token, **params):
        if not creation_token:
            raise ValueError("No creation token given.")
        if creation_token in self.creation_tokens:
            raise FileSystemAlreadyExists(creation_token)

        # Create a new file system ID:
        def make_id():
            return "fs-{}".format(get_random_hex())

        fsid = make_id()
        while fsid in self.file_systems_by_id:
            fsid = make_id()
        self.file_systems_by_id[fsid] = FileSystem(creation_token, fsid, **params)
        self.creation_tokens.add(creation_token)
        return self.file_systems_by_id[fsid]

    def describe_file_systems(self, marker, max_items, creation_token, file_system_id):
        # Restrict the possible corpus of resules based on inputs.
        if creation_token and file_system_id:
            raise BadRequest(
                "Request cannot contain both a file system ID and a creation token."
            )
        elif creation_token:
            # Handle the creation token case.
            corpus = []
            for fs in self.file_systems_by_id.values():
                if fs.creation_token == creation_token:
                    corpus.append(fs.info_json())
        elif file_system_id:
            # Handle the case that a file_system_id is given.
            if file_system_id not in self.file_systems_by_id:
                raise FileSystemNotFound(file_system_id)
            corpus = [self.file_systems_by_id[file_system_id]]
        elif marker is not None:
            # Handle the case that a marker is given.
            if marker not in self.next_markers:
                raise BadRequest("Invalid Marker")
            corpus = self.next_markers[marker]
        else:
            # Handle the vanilla case.
            corpus = [fs.info_json() for fs in self.file_systems_by_id.values()]

        # Handle the max_items parameter.
        file_systems = corpus[:max_items]
        next_marker = self._mark_description(file_systems, max_items)
        return next_marker, file_systems

    def create_mount_target(
        self, file_system_id, subnet_id, ip_address=None, security_groups=None
    ):
        # Get the relevant existing resources
        subnet = self.ec2_backend.get_subnet(subnet_id)
        file_system = self.file_systems_by_id[file_system_id]

        # Check that the mount target doesn't violate constraints.
        if file_system.has_mount_target(subnet):
            raise MountTargetConflict("Mount Target already exists in AZ")

        # Create the new mount target
        mount_target = MountTarget(file_system, subnet, ip_address, security_groups)

        # Establish the network interface.
        network_interface = self.ec2_backend.create_network_interface(
            subnet, [mount_target.ip_address], group_ids=security_groups
        )
        mount_target.set_network_interface(network_interface)

        # Record the new mount target
        file_system.add_mount_target(subnet, mount_target)
        self.mount_targets_by_id[mount_target.mounted_target_id] = mount_target
        return mount_target

    def describe_mount_targets(
        self, max_items, file_system_id, mount_target_id, access_point_id, marker
    ):
        # Restrict the possible corpus of results based on inputs.
        if not (bool(file_system_id) ^ bool(mount_target_id) ^ bool(access_point_id)):
            raise BadRequest("Must specify exactly one mutually exclusive parameter.")
        elif file_system_id:
            # Handle the case that a file_system_id is given.
            if file_system_id not in self.file_systems_by_id:
                raise FileSystemNotFound(file_system_id)
            corpus = [
                mt.info_json()
                for mt in self.file_systems_by_id[file_system_id].iter_mount_targets()
            ]
        elif mount_target_id:
            # Handle mount target specification case.
            corpus = [self.mount_targets_by_id[mount_target_id].info_json()]
        else:
            # We don't handle access_point_id's yet.
            assert False, "Moto does not yet support EFS access points."

        # Handle the case that a marker is given. Note that the handling is quite
        # different from that in describe_file_systems.
        if marker is not None:
            if marker not in self.next_markers:
                raise BadRequest("Invalid Marker")
            corpus_mtids = {m["MountTargetId"] for m in corpus}
            marked_mtids = {m["MountTargetId"] for m in self.next_markers[marker]}
            mt_ids = corpus_mtids & marked_mtids
            corpus = [
                mt_json
                for mt_json in (self.next_markers[marker] + corpus)
                if mt_json["MountTargetId"] in mt_ids
            ]

        # Handle the max_items parameter.
        mount_targets = corpus[:max_items]
        next_marker = self._mark_description(mount_targets, max_items)
        return next_marker, mount_targets

    # add methods from here


efs_backends = {}
for region in Session().get_available_regions("efs"):
    efs_backends[region] = EFSBackend(region)
for region in Session().get_available_regions("efs", partition_name="aws-us-gov"):
    efs_backends[region] = EFSBackend(region)
for region in Session().get_available_regions("efs", partition_name="aws-cn"):
    efs_backends[region] = EFSBackend(region)
