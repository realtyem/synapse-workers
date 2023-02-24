#!/usr/bin/env python
# Copyright 2022 The Matrix.org Foundation C.I.C.
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

# Copied heavily from calculate_jobs.py to set various complement jobs.

import json
import os


def set_output(key: str, value: str):
    # See https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions#setting-an-output-parameter
    with open(os.environ["GITHUB_OUTPUT"], "at") as f:
        print(f"{key}={value}", file=f)


# Calculate the various types of workers.
#
# For each type of test we only run on Py3.10
# Always use postgres and workers options, so those are excluded here.

complement_single_worker_tests = [
    {
        "test_name": "Singles",
        "worker_types": workers,
    }
    for workers in (
        "account_data",
        "appservice",
        "background_worker",
        "client_reader",
        "event_creator",
        "event_persister",
        "federation_inbound",
        "federation_reader",
        "federation_sender",
        "frontend_proxy",
        "media_repository",
        "presence",
        "pusher",
        "receipts",
        "synchrotron",
        "to_device",
        "typing",
        "user_dir",
    )
]

complement_federation_worker_tests = [
    {
        "test_name": "Federation Workers",
        "worker_types": "federation_inbound, federation_reader, federation_sender",
    }
]

complement_sharding_worker_tests = [
    {"test_name": "Sharding", "worker_types": "federation_sender, federation_sender"},
    {"test_name": "Sharding", "worker_types": "pusher, pusher"},
]

complement_stream_writers_worker_tests = [
    {
        "test_name": "Stream Writers",
        "worker_types": "account_data, event_persister, presence, receipts, "
        "to_device, typing",
    },
    {
        "test_name": "Async Stream Writers",
        "worker_types": "stream_writers=account_data+presence+receipts+to_device"
        "+typing, event_persister:2",
        "reactor": "1"
        # Using 1 as a parameter so it passes directly in
    },
]

complement_nuclear_worker_tests = [
    {
        "worker_types": "account_data, account_data, background_worker, event_creator, event_creator, event_persister, event_persister, federation_inbound, federation_reader, federation_reader, federation_sender, federation_sender, frontend_proxy, media_repository, media_repository, pusher, pusher, synchrotron, synchrotron, synchrotron, synchrotron, synchrotron, synchrotron, synchrotron, synchrotron, synchrotron, synchrotron, to_device, to_device, user_dir, user_dir"
    },
    {"worker_types": "BLOW_IT_UP"},
]

print("::group::Calculated Complement jobs")
print(
    json.dumps(
        complement_single_worker_tests
        + complement_sharding_worker_tests
        + complement_stream_writers_worker_tests
        + complement_nuclear_worker_tests,
        indent=4,
    )
)
print("::endgroup::")

test_matrix = json.dumps(complement_single_worker_tests)
set_output("complement_singles_test_matrix", test_matrix)
test_matrix = json.dumps(complement_sharding_worker_tests)
set_output("complement_sharding_test_matrix", test_matrix)
test_matrix = json.dumps(complement_stream_writers_worker_tests)
set_output("complement_stream_writers_test_matrix", test_matrix)
test_matrix = json.dumps(complement_nuclear_worker_tests)
set_output("complement_nuclear_test_matrix", test_matrix)
