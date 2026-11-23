"""Everything the runtime does, this SDK does: any operation can be called from
Python as from Rust, C and C++.

pandora-core-rs offers its local runtime in Rust, as the binding this SDK
wraps (pandora_core_rs.runtime), in C and in C++; its
tools/check_api_parity.py holds those four to one list of operations. These
tests hold this SDK to the binding, both ways:

- every public name of runtime.Local has its name here, or a reason it has
  none (NOT_IN_SDK);
- every public name of pandora_sdk.Local is one of the runtime's operations.

A new pandora-core-rs with an operation this SDK lacks fails here: add it to
Local and to RUNTIME_TO_SDK. A new method on Local that no runtime operation
is behind fails too: add the operation to pandora-core-rs (in every language)
first, or name the operation it is built on.
"""
import inspect

import pandora_sdk
from pandora_core_rs import runtime

# Each public name of runtime.Local, and the names of pandora_sdk.Local
# built on it (properties and methods, Source's included).
RUNTIME_TO_SDK = {
    "bad_frames": ("bad_frames",),
    "calibration": ("calibration", "latest_calibration"),
    "close": ("close",),
    "devices": ("devices",),
    "dropped": ("dropped",),
    "incompatible": ("incompatible",),
    "is_running": ("ended", "wait_ended", "end_error"),
    "latest_hand": ("latest",),
    "next_event": ("read", "on", "frames", "raw", "debug", "on_status", "glove_profile_status"),
    "packets": ("packets",),
    "play_rom": ("play_rom",),
    "play_rtp": ("play_rtp",),
    "send_hall_config": ("send_hall_config",),
    "set_hall_range": ("set_hall_range",),
    "stop": ("stop_reading",),
    "stop_haptics": ("stop_haptics", "stop"),
    "subscribe": ("subscribe",),
    "unprofiled": ("unprofiled",),
    "uses_glove_profile": ("uses_glove_profile",),
    "want_lines": ("on_line",),
}

# Public names of runtime.Local this SDK does not offer, and why.
NOT_IN_SDK = {
    "record_pipeline": "a hook for this SDK's tests of pipeline restarts (test_local.py)",
    "pipeline_log": "the same hook's log",
    "take_link_error": "plumbing: Local gives a link's own exception as Disconnected's cause",
}

# The runtime module's other public names, and what this SDK offers for each.
RUNTIME_MODULE = {
    "Local": "Local",
    "open_port": "open_native_link",
    "SerialLink": "open_native_link",
    "Refused": "ServerError",
    "SILENT_AFTER_S": "Device",  # Device.connected: heard within this many seconds
}


def public(obj):
    return {name for name in dir(obj) if not name.startswith("_")}


def test_every_runtime_operation_has_an_sdk_name_or_a_reason():
    missing = public(runtime.Local) - set(RUNTIME_TO_SDK) - set(NOT_IN_SDK)
    assert not missing, (
        f"pandora_core_rs.runtime.Local has {sorted(missing)}, which this SDK does not offer: "
        f"add each to pandora_sdk.Local and to RUNTIME_TO_SDK in this file")


def test_every_runtime_name_here_is_the_runtimes():
    stale = (set(RUNTIME_TO_SDK) | set(NOT_IN_SDK)) - public(runtime.Local)
    assert not stale, f"runtime.Local has no {sorted(stale)}: take them out of this file"
    assert not set(RUNTIME_TO_SDK) & set(NOT_IN_SDK)


def test_every_sdk_name_here_exists():
    for operation, names in RUNTIME_TO_SDK.items():
        for name in names:
            assert hasattr(pandora_sdk.Local, name), f"{operation}: pandora_sdk.Local has no {name}"


def test_every_public_name_of_local_is_one_of_the_runtimes_operations():
    offered = {name for names in RUNTIME_TO_SDK.values() for name in names}
    extra = public(pandora_sdk.Local) - offered
    assert not extra, (
        f"pandora_sdk.Local has {sorted(extra)}, which no runtime operation is behind: add the "
        f"operation to pandora-core-rs's runtime in every language (its tools/check_api_parity.py) "
        f"first, or name the operation it is built on in RUNTIME_TO_SDK")


def test_the_runtime_modules_other_names_are_offered_too():
    names = {name for name in public(runtime) if not inspect.ismodule(getattr(runtime, name))}
    assert names == set(RUNTIME_MODULE), (
        f"pandora_core_rs.runtime has {sorted(names - set(RUNTIME_MODULE))} new, or lost "
        f"{sorted(set(RUNTIME_MODULE) - names)}: say in RUNTIME_MODULE what this SDK offers for each")
    for name, ours in RUNTIME_MODULE.items():
        assert hasattr(pandora_sdk, ours) or hasattr(pandora_sdk.local, ours), f"{name}: no {ours}"
