#!/usr/bin/env python
# Copyright 2021 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This script reads environment variables and generates a shared Synapse worker,
# nginx and supervisord configs depending on the workers requested.
#
# The environment variables it reads are:
#   * SYNAPSE_SERVER_NAME: The desired server_name of the homeserver.
#   * SYNAPSE_REPORT_STATS: Whether to report stats.
#   * SYNAPSE_WORKER_TYPES: A comma separated list of worker names as specified in
#         WORKERS_CONFIG below. Leave empty for no workers. Add a ':' and a number at
#         the end to multiply that worker. Append multiple worker types with '+' to
#         merge the worker types into a single worker. Add a name and a '=' to the
#         front of a worker type to give this instance a name in logs and nginx.
#         Examples:
#         SYNAPSE_WORKER_TYPES='event_persister, federation_sender, client_reader'
#         SYNAPSE_WORKER_TYPES='event_persister:2, federation_sender:2, client_reader'
#         SYNAPSE_WORKER_TYPES='stream_writers=account_data+presence+typing'
#   * SYNAPSE_AS_REGISTRATION_DIR: If specified, a directory in which .yaml and .yml
#         files will be treated as Application Service registration files.
#   * SYNAPSE_TLS_CERT: Path to a TLS certificate in PEM format.
#   * SYNAPSE_TLS_KEY: Path to a TLS key. If this and SYNAPSE_TLS_CERT are specified,
#         Nginx will be configured to serve TLS on port 8448.
#   * SYNAPSE_USE_EXPERIMENTAL_FORKING_LAUNCHER: Whether to use the forking launcher,
#         only intended for usage in Complement at the moment.
#         No stability guarantees are provided.
#   * SYNAPSE_LOG_LEVEL: Set this to DEBUG, INFO, WARNING or ERROR to change the
#         log level. INFO is the default.
#   * SYNAPSE_LOG_SENSITIVE: If unset, SQL and SQL values won't be logged,
#         regardless of the SYNAPSE_LOG_LEVEL setting.
#
# NOTE: According to Complement's ENTRYPOINT expectations for a homeserver image (as
# defined in the project's README), this script may be run multiple times, and
# functionality should continue to work if so.

import codecs
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, NoReturn, Optional, Set

import yaml
from jinja2 import Environment, FileSystemLoader

DEBUG = True
if DEBUG is True:
    import json

MAIN_PROCESS_HTTP_LISTENER_PORT = 8008
MAIN_PROCESS_HTTP_FED_LISTENER_PORT = 8448
MAIN_PROCESS_NEW_CLIENT_PORT = 8080
MAIN_PROCESS_NEW_FEDERATION_PORT = 8081
MAIN_PROCESS_NEW_REPLICATION_PORT = 9093
MAIN_PROCESS_HTTP_METRICS_LISTENER_PORT = 8060
enable_compressor = False
enable_coturn = False
enable_prometheus = False
enable_redis_exporter = False
enable_postgres_exporter = False

# Workers with exposed endpoints needs either "client", "federation", or "media"
#   listener_resources
# Watching /_matrix/client needs a "client" listener
# Watching /_matrix/federation needs a "federation" listener
# Watching /_matrix/media and related needs a "media" listener
# Stream Writers require "client" and "replication" listeners because they
#   have to attach by instance_map to the master process and have client endpoints.
WORKERS_CONFIG: Dict[str, Dict[str, Any]] = {
    "pusher": {
        "app": "synapse.app.generic_worker",
        "listener_resources": [],
        "endpoint_patterns": [],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "user_dir": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["client"],
        "endpoint_patterns": [
            "^/_matrix/client/(api/v1|r0|v3|unstable)/user_directory/search$"
        ],
        "shared_extra_conf": {"update_user_directory_from_worker": "placeholder_name"},
        "worker_extra_conf": "",
    },
    "media_repository": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["media"],
        "endpoint_patterns": [
            "^/_matrix/media/",
            "^/_synapse/admin/v1/purge_media_cache$",
            "^/_synapse/admin/v1/room/.*/media.*$",
            "^/_synapse/admin/v1/user/.*/media.*$",
            "^/_synapse/admin/v1/media/.*$",
            "^/_synapse/admin/v1/room/.*/media/quarantine$",
        ],
        # The first configured media worker will run the media background jobs
        "shared_extra_conf": {
            "enable_media_repo": False,
            "media_instance_running_background_jobs": "placeholder_name",
        },
        "worker_extra_conf": "enable_media_repo: true",
    },
    "appservice": {
        "app": "synapse.app.generic_worker",
        "listener_resources": [],
        "endpoint_patterns": [],
        "shared_extra_conf": {"notify_appservices_from_worker": "placeholder_name"},
        "worker_extra_conf": "",
    },
    "federation_sender": {
        "app": "synapse.app.generic_worker",
        "listener_resources": [],
        "endpoint_patterns": [],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "synchrotron": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["client"],
        "endpoint_patterns": [
            "^/_matrix/client/(r0|v3|unstable)/sync$",
            "^/_matrix/client/(api/v1|r0|v3)/events$",
            "^/_matrix/client/(api/v1|r0|v3)/initialSync$",
            # "^/_matrix/client/(api/v1|r0|v3)/rooms/[^/]+/initialSync$",
        ],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "client_reader": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["client"],
        "endpoint_patterns": [
            "^/_matrix/client/(api/v1|r0|v3|unstable)/publicRooms$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/joined_members$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/context/.*$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/members$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/state$",
            "^/_matrix/client/v1/rooms/.*/hierarchy$",
            "^/_matrix/client/(v1|unstable)/rooms/.*/relations/",
            "^/_matrix/client/v1/rooms/.*/threads$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/login$",
            "^/_matrix/client/(r0|v3|unstable)/account/3pid$",
            "^/_matrix/client/(r0|v3|unstable)/account/whoami$",
            "^/_matrix/client/versions$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/voip/turnServer$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/register$",
            "^/_matrix/client/(r0|v3|unstable)/auth/.*/fallback/web$",
            # This one needs to be routed by the .* cuz that's the room name.
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/messages$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/event",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/joined_rooms",
            "^/_matrix/client/(r0|v3|unstable/.*)/rooms/.*/aliases",
            "^/_matrix/client/v1/rooms/.*/timestamp_to_event$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/search",
        ],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "federation_reader": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["federation"],
        "endpoint_patterns": [
            "^/_matrix/federation/(v1|v2)/event/",
            "^/_matrix/federation/(v1|v2)/state/",
            "^/_matrix/federation/(v1|v2)/state_ids/",
            "^/_matrix/federation/(v1|v2)/backfill/",
            "^/_matrix/federation/(v1|v2)/get_missing_events/",
            "^/_matrix/federation/(v1|v2)/publicRooms",
            "^/_matrix/federation/(v1|v2)/query/",
            "^/_matrix/federation/(v1|v2)/make_join/",
            "^/_matrix/federation/(v1|v2)/make_leave/",
            "^/_matrix/federation/(v1|v2)/send_join/",
            "^/_matrix/federation/(v1|v2)/send_leave/",
            "^/_matrix/federation/(v1|v2)/invite/",
            "^/_matrix/federation/(v1|v2)/query_auth/",
            "^/_matrix/federation/(v1|v2)/event_auth/",
            "^/_matrix/federation/v1/timestamp_to_event/",
            "^/_matrix/federation/(v1|v2)/exchange_third_party_invite/",
            "^/_matrix/federation/(v1|v2)/user/devices/",
            "^/_matrix/federation/(v1|v2)/get_groups_publicised$",
            "^/_matrix/key/v2/query",
        ],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "federation_inbound": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["federation"],
        "endpoint_patterns": ["^/_matrix/federation/(v1|v2)/send/"],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "event_persister": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["replication"],
        "endpoint_patterns": [],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "background_worker": {
        "app": "synapse.app.generic_worker",
        "listener_resources": [],
        "endpoint_patterns": [],
        # This worker cannot be sharded. Therefore, there should only ever be one
        # background worker. This is enforced for the safety of your database.
        "shared_extra_conf": {"run_background_tasks_on": "placeholder_name"},
        "worker_extra_conf": "",
    },
    "event_creator": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["client"],
        "endpoint_patterns": [
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/redact",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/send",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/"
            "(join|invite|leave|ban|unban|kick)$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/join/",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/profile/",
            "^/_matrix/client/(v1|unstable/org.matrix.msc2716)/rooms/.*/batch_send",
        ],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "frontend_proxy": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["client"],
        "endpoint_patterns": ["^/_matrix/client/(api/v1|r0|v3|unstable)/keys/upload"],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "account_data": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["client", "replication"],
        "endpoint_patterns": [
            "^/_matrix/client/(r0|v3|unstable)/.*/tags",
            "^/_matrix/client/(r0|v3|unstable)/.*/account_data",
        ],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "presence": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["client", "replication"],
        "endpoint_patterns": ["^/_matrix/client/(api/v1|r0|v3|unstable)/presence/"],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "receipts": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["client", "replication"],
        "endpoint_patterns": [
            "^/_matrix/client/(r0|v3|unstable)/rooms/.*/receipt",
            "^/_matrix/client/(r0|v3|unstable)/rooms/.*/read_markers",
        ],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "to_device": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["client", "replication"],
        "endpoint_patterns": ["^/_matrix/client/(r0|v3|unstable)/sendToDevice/"],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
    "typing": {
        "app": "synapse.app.generic_worker",
        "listener_resources": ["client", "replication"],
        "endpoint_patterns": [
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/typing"
        ],
        "shared_extra_conf": {},
        "worker_extra_conf": "",
    },
}

HTTP_BASED_LISTENER_RESOURCES = [
    "health",
    "client",
    "federation",
    "media",
    "replication",
]

# Templates for sections that may be inserted multiple times in config files
NGINX_LOCATION_CONFIG_BLOCK = """
    location ~* {endpoint} {{
        proxy_pass {upstream};
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Host $host;

    }}
"""

NGINX_UPSTREAM_CONFIG_BLOCK = """
upstream {upstream_name} {{
    zone upstreams 64K;
{body}
    keepalive {keepalive_idle_connections};
}}
"""

PROMETHEUS_SCRAPE_CONFIG_BLOCK = """
    - targets: ["127.0.0.1:{metrics_port}"]
      labels:
        instance: "Synapse"
        job: "{name}"
        index: {index}
"""


class Worker:
    """
    This is a base class representing a requested worker.

    Attributes:
        base_name: The basic name serves as a group identifier
        name: The given name the worker should use. base_name + incrementing number
        index: The number this worker was given on the end of base_name to make the name
        app: 'synapse.app.generic_worker' for all now
        listener_resources: Set of types of listeners needed. 'client, federation,
            replication, media' etc.
        listener_port_map: Dict of 'listener':port_number so 'client':18900
        endpoint_patterns: Dict of listener resource containing url endpoints this
            worker accepts connections on. Because a worker can merge multiple roles
            with potentially different listeners, this is important. e.g.
            {'client':{'/url1','/url2'}}
        shared_extra_config: Dict of one-offs that enable special roles for specific
            workers. Ends up in shared.yaml
        worker_extra_conf: Only used by media_repository to enable that functionality.
            Ends up in worker.yaml
        types_list: the List of roles this worker fulfills.
    """

    base_name: str
    name: str
    index: int
    app: str
    listener_resources: Set[str]
    listener_port_map: Dict[str, int]
    endpoint_patterns: Dict[str, Set[str]]
    shared_extra_config: Dict[str, Any]
    worker_extra_conf: str
    types_list: List[str]

    def extract_jinja_worker_template(self) -> Dict[str, Any]:
        config: Dict[str, Any] = {}
        config.setdefault("app", self.app)
        config.setdefault("name", self.name)
        config.setdefault("base_name", self.base_name)
        config.setdefault("index", self.index)
        config.setdefault("listener_resources", self.listener_resources)
        config.setdefault("worker_extra_conf", self.worker_extra_conf)
        return config

    def __init__(self, name: str, worker_type_str: str) -> None:
        """
        Initialize the parameters of this new worker by parsing the piece of string
        stipulating the type of worker needed.

        Args:
            name: The name requested of the worker, will probably be updated at least
                    once.
            worker_type_str: The string representing what roles this worker will
                    fulfill.
        """
        self.listener_resources = set()
        self.endpoint_patterns = defaultdict(set[str])
        self.shared_extra_config = {}
        self.listener_port_map = defaultdict(int)
        self.types_list = []
        self.worker_extra_conf = ""
        self.base_name = ""
        self.name = ""
        self.index = 0
        self.app = ""

        # Assign the name provided
        self.base_name = name

        # Split the worker types from string into list. This will have already been
        # stripped of the potential name and multiplier. Check for duplicates in the
        # split worker type list. No advantage in having duplicated worker types on
        # the same worker. Two would consolidate into one. (e.g. "pusher + pusher"
        # would resolve to a single "pusher" which may not be what was intended.)
        self.types_list = split_and_strip_string(worker_type_str, "+")
        if len(self.types_list) != len(set(self.types_list)):
            error(f"Duplicate worker type found in '{worker_type_str}'! Please fix.")

        for role in self.types_list:
            worker_config = WORKERS_CONFIG.get(role)
            if worker_config:
                worker_config = worker_config.copy()
            else:
                error(
                    f"'{role}' is an unknown worker type! Was found in "
                    f"'{worker_type_str}'. Please fix!"
                )

            # Get the app(all should now be synapse.app.generic_worker).
            # TODO: factor this out
            self.app = str(worker_config.get("app"))

            # Get the listener_resources
            listener_resources = worker_config.get("listener_resources")
            if listener_resources:
                self.listener_resources.update(listener_resources)

            # Get the endpoint_patterns, add them to a set and assign to a dict key
            # of listener_resource. Since any given worker role has exactly one
            # external connection resource, figure out which one it is and use that
            # as a key to identify the endpoint pattern to be assigned to it. This
            # allows different resources to be split onto different ports and then
            # merged with similar resources when worker roles are merged together.
            lr: str = ""
            for this_resource in listener_resources:
                # Only look for these three, as endpoints shouldn't be assigned to
                # something like a 'health' or 'replication' listener.
                if this_resource in ["client", "federation", "media"]:
                    lr = this_resource
            endpoint_patterns = worker_config.get("endpoint_patterns")
            if endpoint_patterns:
                for endpoint in endpoint_patterns:
                    self.endpoint_patterns[lr].add(endpoint)

            # Get shared_extra_conf, if any
            shared_extra_config = worker_config.get("shared_extra_conf")
            if shared_extra_config:
                self.shared_extra_config.update(shared_extra_config)

            # Get worker_extra_conf, if any(there is only one at this time,
            # and that is the media_repository. This goes in the worker yaml, so it's
            # pretty safe, can't use 2 media_repo workers on the same worker.
            worker_extra_conf = worker_config.get("worker_extra_conf")
            if worker_extra_conf:
                # Copy it, in case it takes it as a reference.
                self.worker_extra_conf = worker_extra_conf


class Workers:
    """
    This is a grouping of Worker, containing all the workers requested.

    Attributes:
        total_count: int of how many workers there are total.
        worker: the collection of workers to access data about.
        worker_type_counter: The broad counter that is used to increment the worker name
        worker_type_to_name_map: Used to check for cross naming violations.
        worker_type_fine_grain_counter: A finer grained counter for total number of a
            specific type of worker.
        current_port_counter: A simple integer counter to track the next port number to
            assign.
        map_of_worker_to_upstream: dict of 'worker_name':'upstream_host'.
            e.g. 'event_creator1':'event_creator.client'
    """

    total_count: int
    worker: Dict[str, Worker]
    worker_type_counter: Dict[str, int]
    worker_type_to_name_map: Dict[str, str]
    worker_type_fine_grain_counter: Dict[str, int]
    current_port_counter: int
    map_of_worker_to_upstream: Dict[str, str]
    SHARDING_NOT_ALLOWED_LIST: List[str] = [
        "background_worker",
        "account_data",
        "presence",
        "receipts",
        "typing",
        "to_device",
    ]

    def __init__(self, port_starting_num: int) -> None:
        self.map_of_worker_to_upstream = {}
        self.total_count = 0
        self.worker = {}
        self.worker_type_counter = defaultdict(int)
        self.worker_type_to_name_map = {}
        self.worker_type_fine_grain_counter = defaultdict(int)
        self.current_port_counter = port_starting_num

    def add_worker(self, requested_worker_type: str) -> str:
        """
        Make a worker, check its name is sane, and collect its configuration bits for
                later.

        Args:
            requested_worker_type: the string combination of roles this worker needs to
                fulfill(and possibly a requested name).
        Returns: string of the new worker's full name
        """
        # Peel off any name designated before a '=' to use later.
        worker_base_name = ""
        if "=" in requested_worker_type:
            # Split on "=", remove extra whitespace from ends then make list
            worker_type_split = split_and_strip_string(requested_worker_type, "=")
            if len(worker_type_split) > 2:
                error(
                    "To many worker names requested for a single worker, or to many "
                    f"'='. Please fix: '{requested_worker_type}'"
                )

            # Assign the name
            worker_base_name = worker_type_split[0]

            if not re.match(r"^[a-zA-Z0-9_+-]*[a-zA-Z_+-]$", worker_base_name):
                # Apply a fairly narrow regex to the worker names. Some characters
                # aren't safe for use in file paths or nginx configurations.
                # Don't allow to end with a number because we'll add a number
                # ourselves in a moment.
                error(
                    "Invalid worker name; please choose a name consisting of "
                    "alphanumeric letters, _ + -, but not ending with a digit: "
                    f"{worker_base_name!r}"
                )

            # Reassign the worker_type string with no name on it.
            requested_worker_type = worker_type_split[1]

        # If there was a requested name, it's been removed now
        worker_type_set: Set[str] = set(split_and_strip_string(
            requested_worker_type, "+"
        ))

        if worker_base_name:
            # It'll be useful to have this in the log in case it's a complex of many
            # workers merged together. Note for Complement: it would only be seen in the
            # logs for blueprint construction(which are not collected).
            log(
                f"Worker name request found: '{worker_base_name}', for: "
                f"'{requested_worker_type}'"
            )
        else:
            # The worker name will be the worker_type as a sorted and joined string
            worker_base_name = "+".join(sorted(worker_type_set))

        # Check worker base name isn't in use by something else already. Will error and
        # stop if it is.
        name_to_check = self.worker_type_to_name_map.get(worker_base_name)
        if (name_to_check is None) or (name_to_check == requested_worker_type):
            new_worker = Worker(worker_base_name, requested_worker_type)

            # Check if there is to many of a worker there can only be one of.
            # Will error and stop if it is a problem, e.g. 'background_worker'.
            for role in new_worker.types_list:
                if role in self.worker_type_fine_grain_counter:
                    # It's in the counter, even once. Check for sharding.
                    if role in self.SHARDING_NOT_ALLOWED_LIST:
                        error(
                            f"There can be only a single worker with '{role}' type. "
                            "Please check and remove."
                        )
                # Either it's not in counter or it is but is not a shard hazard,
                # therefore it's safe to add. Don't need the return value here.
                self.worker_type_fine_grain_counter[role] += 1

            # Add to the name:type map, if it already exists this will no-op and that's
            # what we want.
            self.worker_type_to_name_map.setdefault(
                worker_base_name, requested_worker_type
            )

            # Now add or increment it on the global counter
            self.worker_type_counter[requested_worker_type] += 1

            # Save the count as an index
            new_worker.index = self.worker_type_counter[requested_worker_type]
            # Name workers by their type or requested name concatenated with an
            # incrementing number. e.g. event_creator+event_persister1,
            # federation_reader1 or bob1
            new_worker.name = new_worker.base_name + str(new_worker.index)
            # Save it to the kettle for later cooking
            self.worker.setdefault(new_worker.name, new_worker)
            # Give back the new worker name that's been settled on as a copy of the
            # string, more work to do.
            return str(new_worker.name)
        else:
            error(
                f"Can not use '{worker_base_name}' with requested worker_type: "
                f"'{requested_worker_type}', it is in use by: "
                f"'{name_to_check}'"
            )

    def update_local_shared_config(self, worker_name: str) -> None:
        """Insert a given worker name into the worker's configuration dict.

        Args:
            worker_name: The name of the worker to insert.
        """
        dict_to_edit = self.worker[worker_name].shared_extra_config.copy()
        for k, v in dict_to_edit.items():
            # Only proceed if it's a string as some values are boolean
            if isinstance(dict_to_edit[k], str):
                # This will be ignored if the text isn't the placeholder.
                dict_to_edit[k] = v.replace("placeholder_name", worker_name)
            if isinstance(dict_to_edit[k], list):
                # I think this is where we can add stream writers and other special list
                pass
        self.worker[worker_name].shared_extra_config = dict_to_edit

    def set_listener_port_by_resource(
        self, worker_name: str, resource_name: str
    ) -> None:
        """
        Simple helper to add to the listener_port_map and increment the counter of port
        numbers.

        Args:
            worker_name: Name of worker
            resource_name: The listener resource this is for. e.g. 'client' or 'media'

        """
        self.worker[worker_name].listener_port_map[
            resource_name
        ] = self.current_port_counter
        # Increment the counter
        self.current_port_counter += 1


class NginxConfig:
    """
    This represents collected data to plug into the various nginx configuration points.

    Attributes:
        locations: A dict of locations and the name of the upstream_host responsible for
            it. This will be used in the final nginx file. e.g.
            '/_matrix/federation/.*/send':'bob-federation_inbound.federation'
        upstreams_to_ports: A map of upstream host to a Set of ports. This is used in
            the final nginx file.
        upstreams_roles: Set of worker_type roles. Used to calculate load-balancing.

    """

    locations: Dict[str, str]
    upstreams_to_ports: Dict[str, Set[int]]
    upstreams_roles: Dict[str, Set[str]]

    def __init__(self) -> None:
        self.locations = {}
        self.upstreams_to_ports = {}
        self.upstreams_roles = {}

    def add_upstreams(
        self,
        host: str,
        worker_roles: Set[str],
        ports: Set[int],
    ) -> None:
        """
        Add a new upstream host to NginxConfig.upstreams_to_ports and
            NginxConfig.upstreams_roles when given the worker roles as a set and a
            set of ports to assign to it.

        Args:
            host: The name to use for the load-balancing upstream nginx block.
            worker_roles: A Set of roles this upstream block can be responsible for.
            ports: A Set containing a single port or multiple ports to assign.
        Returns: None
        """
        debug(
            f"Making new upstream: {host}\n - roles: {worker_roles}\n - ports: {ports}"
        )
        # Initialize this (possible new) host entries
        self.upstreams_to_ports.setdefault(host, set()).update(ports)
        self.upstreams_roles.setdefault(host, set()).update(worker_roles)

    def create_upstreams_and_locations(self, workers: Workers):
        """
        Use to create upstream data and endpoint locations for the written out Nginx
            config file. Check class attributes for locations and upstreams_to_ports
        """
        # Primarily used to create the necessary data for an upstream block to be
        # written into the nginx config file. For such, the upstream needs a name and
        # port(s). Additionally, some endpoints are more efficiently used if hashed
        # to consistently point to the same worker(or port, in this context). Finally,
        # will create the nginx.locations map used for the final nginx config file that
        # associates the correct upstream name to a given endpoint.

        # The upstream name created will be in the format of "name.listener_type" and
        # will not have "http://" added to it.

        # Note that this does create upstreams even with a single port. Nginx says
        # that doing so, and adding in correct 'zone' and 'keepalive' declarations,
        # allows for better performance because of shared_mem usage for state
        # information on upstreams and by recycling open connections instead of
        # constantly creating and closing them. The connection pool is naturally
        # quick large, but a given closed connection holds the file descriptors for 2
        # minutes before adding it back to the pool.

        # Uses data from
        #   locations_to_port_set to find out what ports can serve a given endpoint
        #   port_to_upstream_name to get the hostname used by this port, typically a
        #       worker's base name is used for convenient grouping. Multiple
        #       non-matching hostnames will be concatenated with a '-' when creating
        #       this upstream name.
        #   port_to_listener_type to get the information on what kind of listener
        #       type(like 'client' or 'federation') this port is associated with.
        #       Functionally, this allows a given upstream to separate client requests
        #       from federation requests(and etc) since each listener type has it's own
        #       port. Also has a visual use for a person inspecting the config file.
        #   upstreams_roles to add a worker's roles for use in the specializing
        #       load-balancing by client IP or other hashing method.

        # locations_to_port_set: A dict of locations and a set of collected ports.
        #    e.g. {'/endpoint1': {1234, 1235, ...}}
        locations_to_port_set: Dict[str, Set[int]] = {}

        # port_to_upstream_name: A map of port to the worker's base name related to it.
        #    e.g. {1234: "worker_base_name"}
        port_to_upstream_name: Dict[int, str] = {}

        # port_to_listener_type: A map of port to worker's listener_type. Functionally,
        #    not important, but used to append to the upstream name so visually can
        #    identify what type of listener it's pointing to.
        #    e.g. { 1234: "client" }
        port_to_listener_type: Dict[int, str] = {}

        # Add nginx location blocks for this worker's endpoints (if any are defined)
        # There are now the capability of having multiple types of listeners.
        # Inappropriate types of listeners were already filtered out.
        for worker in workers.worker.values():
            for listener_type, patterns in worker.endpoint_patterns.items():
                for pattern in patterns:
                    # Collect port numbers for this endpoint pattern
                    locations_to_port_set.setdefault(pattern, set()).add(
                        worker.listener_port_map[listener_type]
                    )
                # Set lookup maps to be used for combining upstreams in a moment.
                # Need the worker's base name
                port_to_upstream_name.setdefault(
                    worker.listener_port_map[listener_type], worker.base_name
                )
                # The listener type
                port_to_listener_type.setdefault(
                    worker.listener_port_map[listener_type], listener_type
                )
                # And the list of roles this(possibly combination) worker can fill
                self.upstreams_roles.setdefault(worker.base_name, set()).update(
                    worker.types_list
                )

        for endpoint_pattern, port_set in locations_to_port_set.items():
            # Reset these for each run
            new_nginx_upstream_set: Set[str] = set()
            new_nginx_upstream_listener_set: Set[str] = set()
            new_nginx_upstream_roles: Set[str] = set()

            # Check each port number. For a single-port upstream this only iterates once
            for each_port in port_set:
                # Get the worker.base_name for the upstream name
                new_nginx_upstream_set.add(port_to_upstream_name[each_port])
                # Get the listener_type, as it's appended to the upstream name
                new_nginx_upstream_listener_set.add(port_to_listener_type[each_port])
                # Get the workers roles for specialized load-balancing
                new_nginx_upstream_roles.update(
                    self.upstreams_roles[port_to_upstream_name[each_port]]
                )

            # This will be the name of the upstream
            new_nginx_upstream = f"{'-'.join(sorted(new_nginx_upstream_set))}"
            new_nginx_upstream += (
                f".{'-'.join(sorted(new_nginx_upstream_listener_set))}"
            )

            # Check this upstream exists, if not then make it
            if new_nginx_upstream not in self.upstreams_to_ports:
                self.add_upstreams(
                    new_nginx_upstream, new_nginx_upstream_roles, port_set
                )

            # Finally, update nginx.locations with new upstream
            self.locations[endpoint_pattern] = new_nginx_upstream


# Utility functions
def log(txt: str) -> None:
    print(txt, flush=True)


def error(txt: str) -> NoReturn:
    print(txt, file=sys.stderr, flush=True)
    sys.exit(2)


def debug(txt: str) -> None:
    if DEBUG:
        print(txt, flush=True)


def flush_buffers() -> None:
    sys.stdout.flush()
    sys.stderr.flush()


def convert(src: str, dst: str, **template_vars: object) -> None:
    """Generate a file from a template

    Args:
        src: Path to the input file.
        dst: Path to write to.
        template_vars: The arguments to replace placeholder variables in the template
            with.
    """
    # Read the template file
    # We disable autoescape to prevent template variables from being escaped,
    # as we're not using HTML.
    env = Environment(loader=FileSystemLoader(os.path.dirname(src)), autoescape=False)
    template = env.get_template(os.path.basename(src))

    # Generate a string from the template.
    rendered = template.render(**template_vars)

    # Write the generated contents to a file
    #
    # We use append mode in case the files have already been written to by something
    # else (for instance, as part of the instructions in a dockerfile).
    with open(dst, "a") as outfile:
        # In case the existing file doesn't end with a newline
        outfile.write("\n")

        outfile.write(rendered)


def getenv_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in ("yes", "y", "true", "1", "t", "on")


def add_worker_roles_to_shared_config(
    shared_config: dict,
    worker_type_list: list,
    worker_name: str,
    worker_ports: Dict[str, int],
) -> None:
    """Given a dictionary representing a config file shared across all workers,
    append appropriate worker information to it for the current worker_type instance.

    Args:
        shared_config: The config dict that all worker instances share (after being
            converted to YAML)
        worker_type_list: The type of worker (one of those defined in WORKERS_CONFIG).
            This list can be a single worker type or multiple.
        worker_name: The name of the worker instance.
        worker_ports: The dict of ports to find the HTTP replication port that the
            worker instance is listening on.
    """
    # The instance_map config field marks the workers that write to various replication
    # streams
    instance_map = shared_config.setdefault("instance_map", {})

    # This is a list of the stream_writers that there can be only one of. Events can be
    # sharded, and therefore doesn't belong here.
    singular_stream_writers = [
        "account_data",
        "presence",
        "receipts",
        "to_device",
        "typing",
    ]

    # Worker-type specific sharding config. Now a single worker can fulfill multiple
    # roles, check each.
    if "pusher" in worker_type_list:
        shared_config.setdefault("pusher_instances", []).append(worker_name)

    if "federation_sender" in worker_type_list:
        shared_config.setdefault("federation_sender_instances", []).append(worker_name)

    if "event_persister" in worker_type_list:
        # Event persisters write to the events stream, so we need to update
        # the list of event stream writers
        shared_config.setdefault("stream_writers", {}).setdefault("events", []).append(
            worker_name
        )

    # This 'for' loop serves two purposes:
    # 1. Update the list of stream writers. It's convenient that the name of the worker
    # type is the same as the stream to write. Iterate over the whole list in case there
    # is more than one.
    # 2. Any worker type that has a 'replication' listener gets add to the
    # 'instance_map'.
    for worker in worker_type_list:
        if worker in singular_stream_writers:
            shared_config.setdefault("stream_writers", {}).setdefault(
                worker, []
            ).append(worker_name)

        if "replication" in worker_ports.keys():
            # Map of worker instance names to host/ports combos. If a worker type in
            # WORKERS_CONFIG needs to be added here in the future, just add a
            # 'replication' entry to the list in listener_resources for that worker.
            instance_map[worker_name] = {
                "host": "localhost",
                "port": worker_ports["replication"],
            }


def combine_shared_config_fragments(
    shared_config: Dict[str, Any], entries_to_add: Dict[str, Any]
) -> Dict[str, Any]:
    # This takes the new dict and copies the old one over top of it, so that it
    # overwrites any duplicate values with the pre-existing ones.
    new_shared_config = entries_to_add.copy()
    new_shared_config.update(shared_config)
    return new_shared_config


def split_and_strip_string(given_string: str, split_char: str) -> List:
    # Removes whitespace from ends of result strings before adding to list.
    return [x.strip() for x in given_string.split(split_char)]


def extract_port_number_from_original_listeners(
        original_listeners_list: List[Any],
        listener_to_find: str
) -> int:
    """
    Iterate and return the port number for a given listener from the original
        configuration files.
    Args:
        original_listeners_list: The list after extraction from the existing config
        listener_to_find: The name of the listener to track down. e.g. 'client'
    Returns: int of the port number found, 0 if not found
    """
    # The original_listeners_list is a list with each entry being a dict containing, at
    # least, a 'type' and a 'port' and another list(resources) with potentially more
    # than one dict again containing, at least, one or more 'names' as a list.
    port_to_return = 0
    for list_entry in original_listeners_list:
        if "http" in list_entry["type"]:
            for resource in list_entry["resources"]:
                if listener_to_find in resource["names"]:
                    debug(f"{listener_to_find} port found: {list_entry['port']}")
                    port_to_return = list_entry["port"]
        if listener_to_find in list_entry["type"]:
            debug(f"{listener_to_find} port found: {list_entry['port']}")
            port_to_return = list_entry["port"]

    return port_to_return


def generate_base_homeserver_config() -> None:
    """Starts Synapse and generates a basic homeserver config, which will later be
    modified for worker support.

    Raises: CalledProcessError if calling start.py returned a non-zero exit code.
    """
    # start.py already does this for us, so just call that.
    # note that this script is copied in from the official, monolith dockerfile
    if "SYNAPSE_HTTP_PORT" not in os.environ:
        os.environ["SYNAPSE_HTTP_PORT"] = str(MAIN_PROCESS_HTTP_LISTENER_PORT)
    if "SYNAPSE_METRICS_HTTP_PORT" not in os.environ:
        os.environ["SYNAPSE_METRICS_HTTP_PORT"] = str(
            MAIN_PROCESS_HTTP_METRICS_LISTENER_PORT
        )
    subprocess.run(["/usr/local/bin/python", "/start.py", "migrate_config"], check=True)


def generate_worker_files(
    environ: Mapping[str, str], config_path: str, data_dir: str
) -> None:
    """Read the desired list of workers from environment variables and generate
    shared homeserver, nginx and supervisord configs.

    Args:
        environ: os.environ instance.
        config_path: The location of the generated Synapse main worker config file.
        data_dir: The location of the synapse data directory. Where log and
            user-facing config files live.
    """
    # Note that yaml cares about indentation, so care should be taken to insert lines
    # into files at the correct indentation below.

    # shared_config is the contents of a Synapse config file that will be shared amongst
    # the main Synapse process as well as all workers.
    # It is intended mainly for disabling functionality when certain workers are spun
    # up, and adding a replication listener.

    # pass through global variables for the add-ons
    # the auto compressor is taken care of in main
    global enable_prometheus
    global enable_redis_exporter
    enable_manhole_master = getenv_bool("SYNAPSE_MANHOLE_MASTER", False)
    enable_manhole_workers = getenv_bool("SYNAPSE_MANHOLE_WORKERS", False)
    enable_metrics = getenv_bool("SYNAPSE_METRICS", False)
    enable_internal_redis = False

    # First read the original config file and extract the listeners block and a few
    # other things. Then we'll add another listener for replication. Later we'll
    # write out the result to the shared config file.

    # These get set as this for the default, in case there isn't actually a main
    # homeserver.yaml to grab original values from.
    original_client_listener_port = MAIN_PROCESS_HTTP_LISTENER_PORT
    original_federation_listener_port = MAIN_PROCESS_HTTP_LISTENER_PORT
    original_replication_listener_port = 0

    with open(config_path) as file_stream:
        original_config = yaml.safe_load(file_stream)
        original_listeners = original_config.get("listeners")
        if original_listeners:
            debug("original_listeners: " + json.dumps(original_listeners, indent=4))
            debug(f"original_listeners length: {len(original_listeners)}")
            original_client_listener_port = extract_port_number_from_original_listeners(original_listeners, "client")
            original_federation_listener_port = extract_port_number_from_original_listeners(original_listeners, "federation")
            original_replication_listener_port = extract_port_number_from_original_listeners(original_listeners, "replication")

        # Any of these listeners could have been declared in the original
        # homeserver.yaml file. Make a new listeners block with appropriate
        # characteristics. The appropriate port(or ports) that were extracted(if
        # found) will be placed into the nginx config for synapse as 'listen'
        # statements. At this time, these port numbers should be the same, as that is
        # how most guides have them written.

        # Do the replication listener first. It gets made regardless, if it was found
        # in the original homeserver.yaml file then let them know that it is being
        # ignored since an external, unrestricted, replication endpoint is a security
        # issue. This type of listener is only used between workers and the main
        # process and serves no other purpose.
        if original_replication_listener_port > 0:
            log(
                "The original 'replication' listener port "
                f"'{original_replication_listener_port}' is being ignored. At this "
                "time, that is a security issue. 'Replication' listeners are only used "
                "by the main process when workers are present.")

        listeners = [
            {
                "port": MAIN_PROCESS_NEW_REPLICATION_PORT,
                "bind_address": "127.0.0.1",
                "type": "http",
                "resources": [{"names": ["replication"]}],
            }
        ]

        # So, at this point we have the original 'client' and 'federation' ports.
        # Ideally, they will be the same port as that is what most guides say to do for
        # the docker image. Experimentally, allow a different port which at this time
        # won't work correctly, but which has the side effect of disabling the receiving
        # of federation traffic.
        # Note: This is not recommended, and is not a performance enhancement.
        if original_client_listener_port == original_federation_listener_port:
            new_main_listeners = [
                {
                    "port": MAIN_PROCESS_NEW_CLIENT_PORT,
                    "bind_addresses": ["0.0.0.0"],
                    "type": "http",
                    "resources": [
                        {
                            "names": ["client", "federation"],
                            "compress": True
                        }
                    ],
                    "tls": False,
                    "x_forwarded": True,
                }
            ]
        else:
            # This was put in as an experimental option to have separated listeners,
            # instead of the default single listener with all the externally important
            # resources attached. It's not completely wired up yet. When finished,
            # declaring SYNAPSE_HTTP_FED_PORT will make it work.
            new_main_listeners = [
                {
                    "port": MAIN_PROCESS_NEW_CLIENT_PORT,
                    "bind_addresses": ["0.0.0.0"],
                    "type": "http",
                    "resources": [
                        {
                            "names": ["client"],
                            "compress": True
                        }
                    ],
                    "tls": False,
                    "x_forwarded": True,
                },
                {
                    "port": MAIN_PROCESS_NEW_FEDERATION_PORT,
                    "bind_addresses": ["0.0.0.0"],
                    "type": "http",
                    "resources": [
                        {
                            "names": ["federation"],
                            "compress": True
                        }
                    ],
                    "tls": False,
                    "x_forwarded": True,
                },
            ]

        listeners += new_main_listeners

        # One of the things to check for is a redis installation declared in the main
        # config file. Since we enable redis support for workers only, if they want to
        # use their own external installation of it then accommodate.
        original_redis = original_config.get("redis")
        if original_redis:
            # Although we can be extremely picky about this, probably just easier to
            # use it as-is. As such, we do nothing here but log it.
            log("External Redis installation found by declaration in homeserver.yaml")

    # If metrics is enabled, and the listener block got overwritten, will need to inject
    # that back in.
    if enable_metrics:
        metric_listener = [
            {
                "port": MAIN_PROCESS_HTTP_METRICS_LISTENER_PORT,
                "bind_address": "0.0.0.0",
                "type": "metrics",
                "resources": [{"compress": True}],
            }
        ]
        listeners += metric_listener

    # Only activate the manhole if the environment says to do so. SYNAPSE_MANHOLE_MASTER
    if enable_manhole_master:
        # The manhole listener is basically the same as other listeners. Needs a type
        # "manhole". In practice, we don't need to bind address because we are in docker
        # and are not going to expose this outside.
        manhole_listener = [
            {
                "type": "manhole",
                "port": 18008,
            }
        ]
        listeners += manhole_listener

    debug("new listener block: " + json.dumps(listeners, indent=4))
    # Start worker ports from this arbitrary port
    worker_port = 18009

    # A keepalive multiplier, this gets multiplied by the number of server lines in a
    # given upstream. This value is a maximum number of idle connections to keepalive.
    # 1 is fine for testing, 32 is recommended for production.
    keepalive_multiplier = 1

    # The main object where our workers configuration will live.
    workers = Workers(worker_port)

    # Create the Object that will contain our nginx data for the reverse proxy
    nginx = NginxConfig()

    # The shared homeserver config. The contents of which will be inserted into the
    # base shared worker jinja2 template.
    #
    # This config file will be passed to all workers, included Synapse's main process.
    shared_config: Dict[str, Any] = {"listeners": listeners}

    # List of dicts that describe workers.
    # We pass this to the Supervisor template later to generate the appropriate
    # program blocks.
    worker_descriptors: List[Dict[str, Any]] = []

    # TODO: Move this data to appropriate place
    # Upstreams for load-balancing purposes. This dict takes the form of a worker type
    # to the ports of each worker. For example:
    # {
    #   worker_type: {1234, 1235, ...}}
    # }
    # and will be used to construct 'upstream' nginx directives.
    #  nginx_upstreams: Dict[str, Set[int]] = {}

    # A map of: {"endpoint": "upstream"}, where "upstream" is a str representing what
    # will be placed after the proxy_pass directive. The main benefit to representing
    # this data as a dict over a str is that we can easily deduplicate endpoints across
    # multiple instances of the same worker.
    #
    # An nginx site config that will be amended to depending on the workers that are
    # spun up. To be placed in /etc/nginx/conf.d.
    # nginx_locations = {}

    # Read the desired worker configuration from the environment
    worker_types_env = environ.get("SYNAPSE_WORKER_TYPES", "").strip()
    # Some shortcuts.
    if worker_types_env == "full":
        worker_types_env = (
            "account_data,background_worker,event_creator,"
            "event_persister,federation_inbound,federation_reader,"
            "federation_sender,frontend_proxy,media_repository,"
            "presence,pusher,receipts,to_device,typing,synchrotron,"
            "user_dir"
        )

    if not worker_types_env:
        # No workers, just the main process
        worker_types = []
    else:
        # Split type names by comma, ignoring whitespace.
        worker_types = split_and_strip_string(worker_types_env, ",")

    # Create the worker configuration directory if it doesn't already exist
    os.makedirs("/conf/workers", exist_ok=True)

    # TODO: move this data to an appropriate place
    # Special endpoint patterns which can share an upstream. For example, take the
    # SYNAPSE_WORKER_TYPES declared as 'federation_inbound:2, federation_inbound +
    # synchrotron'. In this case, there are actually 3 federation_inbound potential
    # workers. Use this to merge these cases together into a special nginx proxy
    # upstream for load-balancing. Going with the above example, this would look like:
    #
    # {
    #   "^/_matrix/federation/(v1|v2)/send/":
    #       [
    #           "federation_inbound",
    #           "federation_inbound+synchrotron"
    #       ]
    #  },
    # {
    #   "^/_matrix/client/(r0|v3|unstable)/sync$":
    #       ["federation_inbound+synchrotron"]
    # },
    # {
    #   "^/_matrix/client/(api/v1|r0|v3)/events$":
    #       ["federation_inbound+synchrotron"]
    # },
    # {
    #   "^/_matrix/client/(api/v1|r0|v3)/initialSync$":
    #       ["federation_inbound+synchrotron"]
    # }
    #
    # Thereby allowing a deeper merging of endpoints. I'm not going lie, this can get
    # complicated really quick.
    # worker_endpoints_dict: Dict[str, list[str]] = {}

    # A list of internal endpoints to healthcheck, starting with the main process
    # which exists even if no workers do.
    healthcheck_urls = ["http://localhost:8080/health"]

    # Expand worker_type multiples if requested in shorthand(e.g. worker:2). Checking
    # for not an actual defined type of worker is done later.
    # Checking performed:
    # 1. if worker:2 or more is declared, it will create additional workers up to number
    # 2. if worker:1, it will create a single copy of this worker as if no number was
    #   given
    # 3. if worker:0 is declared, this worker will be ignored. This is to allow for
    #   scripting and automated expansion and is intended behaviour.
    # 4. if worker:NaN or is a negative number, it will error and log it.
    new_worker_types = []
    for worker_type in worker_types:
        if ":" in worker_type:
            worker_type_components = split_and_strip_string(worker_type, ":")
            count = 0
            # Should only be 2 components, a type of worker(s) and an integer as a
            # string. Cast the number as an int then it can be used as a counter.
            if (
                len(worker_type_components) == 2
                and worker_type_components[-1].isdigit()
            ):
                count = int(worker_type_components[1])
            else:
                error(
                    "Multiplier signal(:) for worker found, but incorrect components: "
                    f"'{worker_type}'. Please fix."
                )
            # As long as there are more than 0, we add one to the list to make below.
            while count > 0:
                new_worker_types.append(worker_type_components[0])
                count -= 1
        else:
            # If it's not a real worker, it will error out below
            new_worker_types.append(worker_type)

    # worker_types is now an expanded list of worker types.
    worker_types = new_worker_types

    # For each worker type specified by the user, create config values
    for worker_type in worker_types:
        # worker_type is a string that can be:
        # 1. a single worker type
        # 2. a combination of worker types, concatenated with a '+'
        # 3. possibly prepended with a name and a '='
        # Make the worker from that string.
        new_worker_name = workers.add_worker(worker_type)

        # Take a reference to update things without a ridiculous amount of extra lines.
        worker = workers.worker[new_worker_name]

        # Replace placeholder names in the config template with the actual worker name.
        workers.update_local_shared_config(new_worker_name)

        # If metrics is enabled, add a listener_resource for that
        if enable_metrics:
            worker.listener_resources.add("metrics")

        # Same for manholes
        if enable_manhole_workers:
            worker.listener_resources.add("manhole")

        # All workers get a health listener
        worker.listener_resources.add("health")

        # Add in ports for each listener entry(e.g. 'client', 'federation', 'media',
        # 'replication')
        for listener_entry in worker.listener_resources:
            workers.set_listener_port_by_resource(new_worker_name, listener_entry)

        # Every worker gets a separate port to handle it's 'health' resource. Append it
        # to the list so docker can check it.
        healthcheck_urls.append(
            f"http://localhost:{worker.listener_port_map['health']}/health"
        )

        # Prepare the bits that will be used in the worker.yaml file
        worker_config = worker.extract_jinja_worker_template()

        # Append the global shared config with any worker-type specific options.
        # Specifically, this keeps existing worker options in the shared.yaml without
        # overwriting them as would normally happen with an update().
        shared_config = combine_shared_config_fragments(
            shared_config, worker.shared_extra_config
        )

        # Update the shared config with sharding-related options if any are found in the
        # global shared_config.
        add_worker_roles_to_shared_config(
            shared_config,
            worker.types_list,
            new_worker_name,
            worker.listener_port_map,
        )

        # Enable the worker in supervisord. This is a list with the bastardized dict
        # appended to it.
        worker_descriptors.append(worker_config)

        # Write out the worker's logging config file
        log_config_filepath = generate_worker_log_config(environ, worker.name, data_dir)

        # Build the worker_listener block for the worker.yaml
        worker_listeners: Dict[str, Any] = {}
        for listener in worker.listener_resources:
            this_listener: Dict[str, Any] = {}
            if listener in HTTP_BASED_LISTENER_RESOURCES:
                this_listener = {
                    "type": "http",
                    "port": worker.listener_port_map[listener],
                    # "resources": [{"names": [listener]}, {"compress": True}],
                    "resources": [{"names": [listener]}],
                }
            # The 'metrics' and 'manhole' listeners don't use 'http' as their type.
            elif listener in ["metrics", "manhole"]:
                this_listener = {
                    "type": listener,
                    "port": worker.listener_port_map[listener],
                }
            worker_listeners.setdefault("worker_listeners", []).append(this_listener)

        # That's everything needed to construct the worker config file.
        convert(
            "/conf/worker.yaml.j2",
            "/conf/workers/{name}.yaml".format(name=worker.name),
            **worker_config,
            worker_listeners=yaml.dump(worker_listeners),
            worker_log_config_filepath=log_config_filepath,
        )

    # Should have all the data needed to create nginx configuration now
    nginx.create_upstreams_and_locations(workers)

    # There are now two dicts to pull data from to construct the nginx config files.
    # nginx.locations has all the endpoints and the upstream they point at
    # nginx.upstreams_to_ports contains the upstream name and the ports it will need

    # Build the nginx location config blocks now that the upstreams are settled. Now is
    # when we pre-pend the 'http://'
    nginx_location_config = ""
    for endpoint_url, upstream_to_use in nginx.locations.items():
        nginx_location_config += NGINX_LOCATION_CONFIG_BLOCK.format(
            endpoint=endpoint_url,
            upstream=f"http://{upstream_to_use}",
        )

    # Determine the load-balancing upstreams to configure
    nginx_upstream_config = ""

    # lb stands for load-balancing. These can be added to if other worker roles are
    # appropriate. Based on the Docs, this is it.
    roles_lb_header_list = ["synchrotron"]
    roles_lb_ip_list = ["federation_inbound"]

    for upstream_name, upstream_worker_ports in nginx.upstreams_to_ports.items():
        body = ""
        # upstream_worker_ports = nginx.upstreams_to_ports.get(upstream_name)
        roles_list = nginx.upstreams_roles[upstream_name]

        # This presents a dilemma. Some endpoints are better load-balanced by
        # Authorization header, and some by remote IP. What do you do if a combo
        # worker was requested that has endpoints for both? As it is likely but
        # not impossible that a user will be on the same IP if they have multiple
        # devices(like at home on Wi-Fi), I believe that balancing by IP would be
        # the broader reaching choice. This is probably only slightly better than
        # round-robin. As such, leave balancing by remote IP as the first of the
        # conditionals below, so if both would apply the first is used.

        # Three additional notes:
        #   1. Federation endpoints shouldn't (necessarily) have Authorization
        #       headers, so using them on these endpoints would be a moot point.
        #   2. For Complement, this situation is reversed as there is only ever a
        #       single IP used during tests, 127.0.0.1.
        #   3. IIRC, it may be possible to hash by both at once, or at least have
        #       both hashes on the same line. If I understand that correctly, the
        #       one that doesn't exist is effectively ignored. However, that
        #       requires increasing the hashmap size in the nginx master config
        #       file, which would take more jinja templating(or at least a 'sed'),
        #       and may not be accepted upstream. Based on previous experiments,
        #       increasing this value was required for hashing by room id, so may
        #       end up being a path forward anyway.

        # Some endpoints should be load-balanced by client IP. This way,
        # if it comes from the same IP, it goes to the same worker and should be
        # a smarter way to cache data. This works well for federation.
        if any(x in roles_lb_ip_list for x in roles_list):
            body += "    hash $proxy_add_x_forwarded_for;\n"

        # Some endpoints should be load-balanced by Authorization header. This
        # means that even with a different IP, a user should get the same data
        # from the same upstream source, like a synchrotron worker, with smarter
        # caching of data.
        elif any(x in roles_lb_header_list for x in roles_list):
            body += "    hash $http_authorization consistent;\n"

        # Add specific "hosts" by port number to the upstream block.
        for port in upstream_worker_ports:
            body += f"    server localhost:{port};\n"

        # Need this to determine keepalive argument, need multiple of 2. Double the
        # number, as each connection to an upstream actually has two sockets. Then apply
        # our multiplier.
        keepalive_idle_connections = (
            len(upstream_worker_ports) * 2 * keepalive_multiplier
        )

        # Everything else, just use the default basic round-robin scheme.
        nginx_upstream_config += NGINX_UPSTREAM_CONFIG_BLOCK.format(
            upstream_name=upstream_name,
            body=body,
            keepalive_idle_connections=keepalive_idle_connections,
        )

    # Finally, we'll write out the config files.

    # log config for the master process
    master_log_config = generate_worker_log_config(environ, "master", data_dir)
    shared_config["log_config"] = master_log_config

    # Find application service registrations
    appservice_registrations = None
    appservice_registration_dir = os.environ.get("SYNAPSE_AS_REGISTRATION_DIR")
    if appservice_registration_dir:
        # Scan for all YAML files that should be application service registrations.
        appservice_registrations = [
            str(reg_path.resolve())
            for reg_path in Path(appservice_registration_dir).iterdir()
            if reg_path.suffix.lower() in (".yaml", ".yml")
        ]

    debug("nginx.locations: " + json.dumps(nginx.locations, indent=4))
    debug("nginx.upstreams_to_ports: " + str(nginx.upstreams_to_ports))
    debug("nginx_upstream_config: " + str(nginx_upstream_config))
    debug("global shared_config: " + json.dumps(shared_config, indent=4))
    # One last thing to fixup for the shared.yaml file, redis.
    workers_in_use = len(worker_types) > 0
    # If using workers, we need redis
    if workers_in_use:
        # Redis wasn't found in the original config
        if not original_redis:
            # This is sufficient, because an existing statement of redis will be used.
            enable_internal_redis = True

    # Shared homeserver config
    convert(
        "/conf/shared.yaml.j2",
        "/conf/workers/shared.yaml",
        shared_worker_config=yaml.dump(shared_config),
        appservice_registrations=appservice_registrations,
        enable_internal_redis=enable_internal_redis,
    )

    # Nginx config
    convert(
        "/conf/nginx.conf.j2",
        "/etc/nginx/conf.d/matrix-synapse.conf",
        worker_locations=nginx_location_config,
        upstream_directives=nginx_upstream_config,
        tls_cert_path=os.environ.get("SYNAPSE_TLS_CERT"),
        tls_key_path=os.environ.get("SYNAPSE_TLS_KEY"),
        original_federation_listener_port=original_federation_listener_port,
        original_client_listener_port=original_client_listener_port,
        main_proxy_pass_cli_port=MAIN_PROCESS_NEW_CLIENT_PORT,
        # Part of the SYNAPSE_HTTP_FED_PORT experiment. Empty is ok here.
        main_proxy_pass_fed_port=MAIN_PROCESS_NEW_FEDERATION_PORT,
    )

    # Prometheus config, if enabled
    # Set up the metric end point locations, names and indexes
    if enable_prometheus:
        prom_endpoint_config = ""
        for _, worker in workers.worker.items():
            prom_endpoint_config += PROMETHEUS_SCRAPE_CONFIG_BLOCK.format(
                name=worker.base_name,
                metrics_port=str(worker.listener_port_map["metrics"]),
                index=str(worker.index),
            )
        convert(
            "/conf/prometheus.yml.j2",
            "/etc/prometheus/prometheus.yml",
            metric_endpoint_locations=prom_endpoint_config,
        )

    # Supervisord config
    os.makedirs("/etc/supervisor", exist_ok=True)
    convert(
        "/conf/supervisord.conf.j2",
        "/etc/supervisor/supervisord.conf",
        main_config_path=config_path,
        enable_redis=enable_internal_redis,
        enable_redis_exporter=enable_redis_exporter,
        enable_postgres_exporter=enable_postgres_exporter,
        enable_prometheus=enable_prometheus,
        enable_compressor=enable_compressor,
        enable_coturn=enable_coturn,
    )

    convert(
        "/conf/synapse.supervisord.conf.j2",
        "/etc/supervisor/conf.d/synapse.conf",
        workers=worker_descriptors,
        main_config_path=config_path,
        use_forking_launcher=environ.get("SYNAPSE_USE_EXPERIMENTAL_FORKING_LAUNCHER"),
    )

    # healthcheck config
    convert(
        "/conf/healthcheck.sh.j2",
        "/healthcheck.sh",
        healthcheck_urls=healthcheck_urls,
    )

    # Ensure the logging directory exists
    log_dir = data_dir + "/logs"
    if not os.path.exists(log_dir):
        os.mkdir(log_dir)


def generate_worker_log_config(
    environ: Mapping[str, str], worker_name: str, data_dir: str
) -> str:
    """Generate a log.config file for the given worker.

    Returns: the path to the generated file
    """
    # Check whether we should write worker logs to disk, in addition to the console
    extra_log_template_args: Dict[str, Optional[str]] = {}
    if environ.get("SYNAPSE_WORKERS_WRITE_LOGS_TO_DISK"):
        extra_log_template_args["LOG_FILE_PATH"] = f"{data_dir}/logs/{worker_name}.log"

    extra_log_template_args["SYNAPSE_LOG_LEVEL"] = environ.get("SYNAPSE_LOG_LEVEL")
    extra_log_template_args["SYNAPSE_LOG_SENSITIVE"] = environ.get(
        "SYNAPSE_LOG_SENSITIVE"
    )

    # Render and write the file
    log_config_filepath = f"/conf/workers/{worker_name}.log.config"
    convert(
        "/conf/log.config",
        log_config_filepath,
        worker_name=worker_name,
        **extra_log_template_args,
        include_worker_name_in_log_line=environ.get(
            "SYNAPSE_USE_EXPERIMENTAL_FORKING_LAUNCHER"
        ),
    )
    return log_config_filepath


def main(args: List[str], environ: MutableMapping[str, str]) -> None:
    config_dir = environ.get("SYNAPSE_CONFIG_DIR", "/data")
    config_path = environ.get("SYNAPSE_CONFIG_PATH", config_dir + "/homeserver.yaml")
    data_dir = environ.get("SYNAPSE_DATA_DIR", "/data")
    # Enable add-ons from environment string
    global enable_compressor
    global enable_coturn
    global enable_prometheus
    global enable_redis_exporter
    global enable_postgres_exporter
    enable_compressor = (
        getenv_bool("SYNAPSE_ENABLE_COMPRESSOR", False)
        and "POSTGRES_PASSWORD" in environ
    )
    enable_coturn = getenv_bool("SYNAPSE_ENABLE_BUILTIN_COTURN", False)
    enable_prometheus = getenv_bool("SYNAPSE_METRICS", False)
    enable_redis_exporter = (
        getenv_bool("SYNAPSE_ENABLE_REDIS_METRIC_EXPORT", False)
        and enable_prometheus is True
    )
    enable_postgres_exporter = (
        getenv_bool("SYNAPSE_ENABLE_POSTGRES_METRIC_EXPORT", False)
        and "POSTGRES_PASSWORD" in environ
    )

    # override SYNAPSE_NO_TLS, we don't support TLS in worker mode,
    # this needs to be handled by a frontend proxy
    environ["SYNAPSE_NO_TLS"] = "yes"

    # Sanitize the environment to keep a bunch of junk out of the jinja templates
    environ["SYNAPSE_METRICS"] = str(getenv_bool("SYNAPSE_METRICS", False))
    environ["SYNAPSE_ENABLE_REGISTRATION"] = str(
        getenv_bool("SYNAPSE_ENABLE_REGISTRATION", False)
    )
    environ["SYNAPSE_ALLOW_GUEST"] = str(getenv_bool("SYNAPSE_ALLOW_GUEST", False))
    environ["SYNAPSE_URL_PREVIEW_ENABLED"] = str(
        getenv_bool("SYNAPSE_URL_PREVIEW_ENABLED", False)
    )
    environ["SYNAPSE_SERVE_SERVER_WELLKNOWN"] = str(
        getenv_bool("SYNAPSE_SERVE_SERVER_WELLKNOWN", False)
    )
    environ["SYNAPSE_EMAIL"] = str(getenv_bool("SYNAPSE_EMAIL", False))
    if enable_coturn is True:
        if "SYNAPSE_TURN_SECRET" not in environ:
            log("Generating a random secret for SYNAPSE_TURN_SECRET")
            value = codecs.encode(os.urandom(32), "hex").decode()
            environ["SYNAPSE_TURN_SECRET"] = value

        if "SYNAPSE_TURN_URIS" not in environ:
            log("Make sure you setup port forwarding for port 3478")
            value = "turn:%s:3478?transport=udp,turn:%s:3478?transport=tcp" % (
                environ["SYNAPSE_SERVER_NAME"],
                environ["SYNAPSE_SERVER_NAME"],
            )
            environ["SYNAPSE_TURN_URIS"] = value

        if "COTURN_EXTERNAL_IP" not in environ:
            value = urllib.request.urlopen("https://v4.ident.me").read().decode("utf8")
            environ["COTURN_EXTERNAL_IP"] = value

        if "COTURN_INTERNAL_IP" not in environ:
            value = str(socket.gethostbyname(socket.gethostname()))
            environ["COTURN_INTERNAL_IP"] = value

        if "COTURN_MIN_PORT" not in environ:
            value = "49153"
            environ["COTURN_MIN_PORT"] = value

        if "COTURN_MAX_PORT" not in environ:
            value = "49173"
            environ["COTURN_MAX_PORT"] = value

        environ["COTURN_METRICS"] = str(getenv_bool("COTURN_METRICS", False))

    # Generate the base homeserver config if one does not yet exist
    if not os.path.exists(config_path):
        log("Generating base homeserver config")
        generate_base_homeserver_config()

    # This script may be run multiple times (mostly by Complement, see note at top of
    # file). Don't re-configure workers in this instance.
    mark_filepath = "/conf/workers_have_been_configured"
    if not os.path.exists(mark_filepath):
        # This gets added here instead of above so it only runs one time.
        # Add cron service and crontab file if enabled in environment.
        if enable_compressor is True:
            shutil.copy("/conf/synapse_auto_compressor.job", "/etc/cron.d/")
            convert(
                "/conf/run_compressor.sh.j2",
                "/conf/run_compressor.sh",
                postgres_user=os.environ.get("POSTGRES_USER"),
                postgres_password=os.environ.get("POSTGRES_PASSWORD"),
                postgres_db=os.environ.get("POSTGRES_DB"),
                postgres_host=os.environ.get("POSTGRES_HOST"),
                postgres_port=os.environ.get("POSTGRES_PORT"),
            )
            # Make the custom script we just made executable, as it's run by cron.
            subprocess.run(
                ["chmod", "0755", "/conf/run_compressor.sh"], stdout=subprocess.PIPE
            ).stdout.decode("utf-8")
            # Actually add it to cron explicitly.
            subprocess.run(
                ["crontab", "/etc/cron.d/synapse_auto_compressor.job"],
                stdout=subprocess.PIPE,
            ).stdout.decode("utf-8")

        # Make postgres_exporter custom script if enabled in environment.
        if enable_postgres_exporter is True:
            convert(
                "/conf/run_pg_exporter.sh.j2",
                "/conf/run_pg_exporter.sh",
                postgres_user=os.environ.get("POSTGRES_USER"),
                postgres_password=os.environ.get("POSTGRES_PASSWORD"),
                postgres_db=os.environ.get("POSTGRES_DB"),
                postgres_host=os.environ.get("POSTGRES_HOST"),
                postgres_port=os.environ.get("POSTGRES_PORT"),
            )
            # Make the custom script we just made executable, as it's run by cron.
            subprocess.run(
                ["chmod", "0755", "/conf/run_pg_exporter.sh"], stdout=subprocess.PIPE
            ).stdout.decode("utf-8")

        if enable_coturn is True:
            convert(
                "/conf/turnserver.conf.j2",
                "/conf/turnserver.conf",
                server_name=environ["SYNAPSE_SERVER_NAME"],
                coturn_secret=environ["SYNAPSE_TURN_SECRET"],
                min_port=environ["COTURN_MIN_PORT"],
                max_port=environ["COTURN_MAX_PORT"],
                internal_ip=environ["COTURN_INTERNAL_IP"],
                external_ip=environ["COTURN_EXTERNAL_IP"],
                enable_coturn_metrics=environ["COTURN_METRICS"],
            )
        # Always regenerate all other config files
        generate_worker_files(environ, config_path, data_dir)

        # Mark workers as being configured
        with open(mark_filepath, "w") as f:
            f.write("")

    # Lifted right out of start.py
    jemallocpath = "/usr/lib/%s-linux-gnu/libjemalloc.so.2" % (platform.machine(),)

    if os.path.isfile(jemallocpath):
        environ["LD_PRELOAD"] = jemallocpath
    else:
        log("Could not find %s, will not use" % (jemallocpath,))

    # Start supervisord, which will start Synapse, all of the configured worker
    # processes, redis, nginx etc. according to the config we created above.
    log("Starting supervisord")
    log("Custom entrypoint loaded")
    flush_buffers()
    os.execle(
        "/usr/local/bin/supervisord",
        "supervisord",
        "-c",
        "/etc/supervisor/supervisord.conf",
        environ,
    )


if __name__ == "__main__":
    main(sys.argv, os.environ)
