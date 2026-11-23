"""Hands straight from a receiver, with no app running.

    import pandora_sdk

    with pandora_sdk.open_receiver("profiles/") as gloves:
        gloves.subscribe(hands=["right"], rate_hz=50, bones=False)
        for hand in gloves.frames():
            print(hand.angles_deg)

The same reading as connect() gives (subscribe, frames, raw, debug, read,
latest, on, devices), the same Frame, Raw and Debug, and the same commands for a
glove (play_rom, play_rtp, stop, set_hall_range) and calibration bounds, so a
program can change between the two by changing one line. Here the
receiver is read in this process by pandora-core-rs's runtime
(pandora_core_rs.runtime, in Rust): its thread reads the receiver's port and
pushes each glove's packets through that glove's own pipeline, and commands
are written to the same port. The same runtime is what the C and C++ SDK
read through; this module only hands its messages to Source.

A glove's pipeline runs only while a frames or debug subscription includes
it, or a raw one with cancelled (the counts after earth-field cancellation
exist only once the pipeline has had the packet); with plain raw alone, its
packets make the raw message and nothing else, and no pipeline is built at
all until calibration() asks for one, which loads the glove's profile with
its newest packet. The pipeline's default files (pandora-core-rs's own, or
defaults=) are parsed only then. A
pipeline that missed packets starts again as a new one at the next packet it
is wanted for, so its smoothing begins there, as on a glove's first packet.

A glove's frames need a profile: its hand and the calibration it carries.
`pandora calibrate` (pandora_sdk/calibrate.py) writes one, into
default_profiles_dir() unless told otherwise, which is also where profiles
are read from when none are named. A glove with no profile given this way
has its own read from its flash: the hand and calibration the 01 Glove app
stores on it (read_glove_profiles, on by default), so a glove calibrated in
the app gives frames here too. Its packets do not wait for the read, which
takes a moment; on_status() and glove_profile_status() say how it went. A
profile given always wins over the glove's. A glove with none is read raw only: its
raw messages are passed on as the default hand's (default_hand, "right"
unless told otherwise; the packet does not say), it takes commands, and it
never gets a pipeline, so no frames, debug values or cancelled counts; its id
is kept in `unprofiled`. With default_hand=None its packets are left out
altogether. Nothing is sent to a glove unless asked (set_hall_range, the
haptics), so a profile's calibration must have been taken at the range the
glove powers up in, which is the range `pandora calibrate` puts it on, or the
program puts the glove on the profile's range itself.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Union

from pandora_core_rs import protocol, receiver, runtime
from pandora_core_rs.glove import Profile
from pandora_core_rs.packet_decoder import GlovePacket, PacketDecoder
from pandora_core_rs.packet_layout import (HALL_DEFAULT_FILTER, HALL_DEFAULT_ODR, HALL_DEFAULT_OSR,
                                           HALL_DEFAULT_RANGE, HALL_TARGET_ALL)
from pandora_core_rs.protocol import Calibration, Device, Subscription
from pandora_core_rs.receiver import ReceiverPort
from pandora_core_rs.serial_framing import decode_frame, encode_frame, is_binary_frame
from pandora_core_rs.tuning import Tuning

from pandora_sdk.source import CallbackFailed, ConnectionFailed, ServerError, Source

# A glove with no packet for this long is listed as not connected, as the
# protocol's device list says, and takes no commands.
SILENT_AFTER_S = 2.0

# A message of each stream as the runtime hands it over (the app's msgpack),
# decoded as connect()'s client decodes the app's.
_DECODE: Dict[str, Callable[[bytes], Any]] = {
    "frames": protocol.decode_frame, "raw": protocol.decode_raw, "debug": protocol.decode_debug}

Profiles = Union[str, Path, Mapping[str, Union[Profile, Mapping[str, Any]]]]

_log = logging.getLogger("pandora_sdk")


def default_profiles_dir() -> Path:
    """Where `pandora calibrate` writes profiles and open_receiver() reads
    them when no folder is named: `.pandora/profiles` in the home folder."""
    return Path.home() / ".pandora" / "profiles"


def load_profiles(folder: Union[str, Path]) -> Dict[str, Profile]:
    """The profiles in *folder*: one `<device id>.json` per glove, each the
    JSON form of a pandora_core_rs Profile (hand, sections, range, ...). None
    when there is no such folder."""
    return {path.stem: Profile.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(Path(folder).glob("*.json"))}


def save_profile(folder: Union[str, Path], device_id: str, profile: Profile) -> Path:
    """Write *profile* as *folder*'s `<device id>.json`, whole or not at all;
    the file's path."""
    path = Path(folder) / f"{device_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    unfinished = path.with_name(path.name + ".tmp")
    unfinished.write_text(json.dumps(profile.to_dict(), indent=1) + "\n", encoding="utf-8")
    unfinished.replace(path)
    return path


def list_receivers() -> List[ReceiverPort]:
    """The receivers plugged into this computer, by port name in number
    order (COM9 before COM10): each one's port (.device), USB serial number
    and product name."""
    return sorted(receiver.find_receivers(), key=lambda found: _port_order(found.device))


def _port_order(name: str) -> List[Union[int, str]]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def receiver_port(port: Optional[str] = None) -> str:
    """*port*, or the serial port of the one receiver plugged in."""
    if port is not None:
        return port
    found = list_receivers()
    if not found:
        raise ConnectionFailed("no receiver is plugged into this computer", "no_receiver")
    if len(found) > 1:
        raise ConnectionFailed(
            f"more than one receiver ({', '.join(f.device for f in found)}); "
            "say which one by its port", "several_receivers")
    return found[0].device


def open_link(port: Optional[str] = None, *, timeout: float = 0.2,
              write_timeout: float = 2.0) -> Any:
    """The receiver's serial port (a pyserial Serial), opened as the link
    needs: 4 Mbaud, RTS/CTS flow control, held by this program alone, and on
    Linux the CH343's flow control switched on. *port*, or the one receiver
    plugged in (list_receivers() for choosing among several). *timeout* is
    how long a read waits for its first byte, in seconds; *write_timeout*
    how long a write may wait on the receiver (a hung one stops taking
    bytes), after which it raises. ConnectionFailed when it cannot be
    opened; its code says why."""
    port = receiver_port(port)
    try:
        link = receiver.open_port(port, timeout=timeout, write_timeout=write_timeout)
    except OSError as failed:
        raise open_refusal(port, failed, "read through it with pandora_sdk.connect(), "
                                         "or close it.") from failed
    receiver.prepare_link(port, link, _log.info)
    return link


def open_native_link(port: Optional[str] = None, *, timeout: float = 0.2,
                     write_timeout: float = 2.0) -> Any:
    """The receiver's serial port opened by pandora-core-rs's runtime, for
    Local: as open_link() opens it (4 Mbaud, RTS/CTS, held by this program
    alone, the CH343's flow control on Linux), but read from Rust with no
    Python in the loop. ConnectionFailed when it cannot be opened."""
    port = receiver_port(port)
    try:
        link = runtime.open_port(port, timeout=timeout, write_timeout=write_timeout)
    except OSError as failed:
        raise open_refusal(port, failed, "read through it with pandora_sdk.connect(), "
                                         "or close it.") from failed
    for note in getattr(link, "notes", ()):
        _log.info(note)
    return link


def open_refusal(port: str, failed: OSError, while_the_app_runs: str) -> ConnectionFailed:
    """ConnectionFailed for *port* refusing to open with *failed*: in_use,
    no_access or open_failed. *while_the_app_runs* ends the in_use message.
    Windows says "Access is denied" for a port another program holds; POSIX
    says "Permission denied" only for a port this user may not open, and
    "could not exclusively lock" for a held one."""
    words = str(failed).lower()
    if "permission denied" in words and "exclusively lock" not in words:
        return ConnectionFailed(
            f"this user may not open {port}. On Linux, add yourself to the group that owns "
            "it (dialout on Debian and Ubuntu, uucp on Arch) and log in again.", "no_access")
    if receiver.is_port_locked(failed):
        return ConnectionFailed(
            f"{port} is in use. If the 01 Glove app is running it holds the receiver: "
            f"{while_the_app_runs}", "in_use")
    return ConnectionFailed(f"could not open {port}: {failed}", "open_failed")


def send_command(link: Any, payload: bytes) -> None:
    """Write one command for a glove (pandora_core_rs.commands' bytes) to the
    receiver on *link*, framed. ServerError "send_failed" when the port will
    not take it."""
    try:
        link.write(encode_frame(payload))
    except OSError as failed:  # pyserial's errors, its write timeout among them, are OSErrors
        raise ServerError("send_failed",
                          f"could not send to the receiver ({failed}). If it stays like "
                          "this, unplug the receiver and plug it back in.") from None


def link_packets(link: Any, decoder: Optional[PacketDecoder] = None,
                 other: Optional[Callable[[bytes], None]] = None
                 ) -> Iterator[Optional[GlovePacket]]:
    """Each tracking packet as *link* delivers it, from any glove, and None
    for a read that held none, so the reader can stop or lose patience.
    Every line that held no packet (the core's packet_from_line says which)
    goes to *other*, in order with the packets. Never ends; a failing port
    raises its OSError (link_failure words it)."""
    decoder = decoder or PacketDecoder()
    splitter = receiver.LineSplitter()
    while True:
        found = False
        for line in splitter.feed(receiver.read_available(link)):
            packet = receiver.packet_from_line(line, decoder)
            if packet is not None:
                found = True
                yield packet
            elif other is not None:
                other(line)
        if not found:
            yield None


def is_damaged_frame(line: bytes) -> bool:
    """Whether a line that held no packet is a binary frame that failed its
    COBS or CRC check (pandora_core_rs.serial_framing's decode_frame)."""
    return is_binary_frame(line) and decode_frame(line) is None


def text_of(line: bytes) -> Optional[str]:
    """A receiver status line (BL:, PONG:, DFU: ...) without its line end,
    for a line that held no packet; None for a binary frame, an empty line,
    or bytes no status line has (the tail of a frame cut off when the port
    was opened, which lost the byte that marks a frame)."""
    if is_binary_frame(line) or _NOT_TEXT.search(line):
        return None
    return line.decode("ascii").strip() or None


# Bytes no receiver status line holds: control characters other than tab and
# the line end, and anything past ASCII.
_NOT_TEXT = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\xff]")


def link_failure(failed: OSError) -> str:
    """Why reading the receiver ended, for the person at the keyboard."""
    return ("the receiver was unplugged" if receiver.is_disconnect_error(failed)
            else f"reading the receiver failed ({failed})")


def link_failure_code(failed: OSError) -> str:
    """Why reading the receiver ended, as Disconnected's code."""
    return "unplugged" if receiver.is_disconnect_error(failed) else "read_failed"


def open_receiver(profiles: Optional[Profiles] = None, *, port: Optional[str] = None,
                  tuning: Union[Tuning, Mapping[str, Any], None] = None,
                  defaults: Optional[Mapping[str, Any]] = None, buffer: int = 256,
                  default_hand: Optional[str] = "right",
                  read_glove_profiles: bool = True) -> "Local":
    """Open the receiver on this computer and run the gloves' pipelines here.

    *profiles* is a folder of `<device id>.json` files, or a dict of device id
    to profile; left out, it is default_profiles_dir(), where `pandora
    calibrate` writes. *port* names the receiver's serial port when there is
    more than one. *tuning* is the pipeline's settings (pandora_core_rs Tuning),
    *defaults* its shipped default files as parsed JSON by file name; left
    out, pandora-core-rs's own are used, read when the first pipeline is built.
    *default_hand* is the hand a glove with no profile is read as, raw only;
    None leaves such a glove out. *read_glove_profiles*: a glove with no
    profile in *profiles* is read with the one it stores itself, once read
    (the 01 Glove app's calibration); False reads it as a glove with no
    profile.
    """
    return Local(profiles, port=port, tuning=tuning, defaults=defaults, buffer=buffer,
                 default_hand=default_hand, read_glove_profiles=read_glove_profiles)


class Local(Source):
    """A receiver read in this process. Use open_receiver(); *link* is an
    already open port (or anything with its read, in_waiting, write and
    close), *clock* the seconds frames are stamped and paced with, and
    *wall_clock* the seconds since the Unix epoch their wall_time_us is.

    The reading itself is pandora-core-rs's runtime (pandora_core_rs.runtime):
    its thread reads the link and runs the pipelines. A thread of this
    object's ("pandora-sdk-receiver") takes what it made and hands it to
    Source, so callbacks given to on() and on_line() run there."""

    def __init__(self, profiles: Optional[Profiles] = None, *, port: Optional[str] = None,
                 tuning: Union[Tuning, Mapping[str, Any], None] = None,
                 defaults: Optional[Mapping[str, Any]] = None, buffer: int = 256,
                 link: Any = None, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 default_hand: Optional[str] = "right",
                 read_glove_profiles: bool = True) -> None:
        super().__init__(buffer)
        if default_hand is not None and default_hand not in protocol.HANDS:
            raise ValueError(f"default_hand is {default_hand!r}; it is 'left', 'right' or None")
        if profiles is None or isinstance(profiles, (str, Path)):
            loaded = load_profiles(default_profiles_dir() if profiles is None else profiles)
        else:
            loaded = {device: profile if isinstance(profile, Profile)
                      else Profile.from_dict(profile)
                      for device, profile in profiles.items()}
        if isinstance(tuning, Tuning):
            tuning = tuning.to_dict()
        # Tracking packets read so far, from any glove: counted as their
        # messages are handed over, so every message of a packet counted is
        # already in latest() and the queues.
        self.packets = 0
        # The function given each receiver text line (on_line()).
        self._on_line: Optional[Callable[[str], object]] = None
        # The function given each glove status (on_status()), and how each
        # glove's profile read ended (its last profile_* status kind).
        self._on_status: Optional[Callable[[str, str, str], object]] = None
        self._read_glove_profiles = read_glove_profiles
        self._profile_status: Dict[str, str] = {}
        # A link given here is closed here; one opened here is the runtime's.
        self._link = link
        native_link = open_native_link(port) if link is None else link
        self._native = runtime.Local(
            native_link, {device: profile.to_dict() for device, profile in loaded.items()},
            tuning, defaults, default_hand,
            # The system's clocks are read in Rust; a clock given is called from there.
            None if clock is time.monotonic else clock,
            None if wall_clock is time.time else wall_clock,
            time.monotonic() if clock is time.monotonic else None,
            read_glove_profiles=read_glove_profiles)
        self._stopping = False
        self._reader = threading.Thread(target=self._take_messages, name="pandora-sdk-receiver",
                                        daemon=True)
        self._reader.start()

    @property
    def unprofiled(self) -> Set[str]:
        """Gloves heard that have no profile: read raw only, as the default
        hand, or left out when there is none."""
        return set(self._native.unprofiled)

    @property
    def bad_frames(self) -> int:
        """Binary frames read so far that failed their COBS or CRC check."""
        return self._native.bad_frames

    @property
    def incompatible(self) -> Dict[str, str]:
        """What gloves send that this SDK cannot read, in words, by device id
        ("receiver" for a packet naming none): the glove's (or receiver's)
        firmware and this SDK's pandora-core-rs do not match, so its packets
        are left out until whichever is older is updated."""
        return dict(self._native.incompatible)

    def uses_glove_profile(self, device: str) -> bool:
        """Whether *device* runs on the profile it stores itself (read from
        the glove), rather than one given here or none."""
        return self._native.uses_glove_profile(device)

    def glove_profile_status(self, device: str) -> Optional[str]:
        """How reading *device*'s own profile ended, as its status kind:
        "profile_loaded", "profile_unsupported" (firmware without a profile
        store), "profile_failed" or "profile_no_hand". None while it is being
        read, for a glove given a profile, or with read_glove_profiles off."""
        with self._changed:
            return self._profile_status.get(device)

    def on_status(self, callback: Optional[Callable[[str, str, str], object]]) -> None:
        """Call *callback*(device, kind, message) with what there is to know
        about a glove as it happens: its own profile read (kind
        "profile_loaded", "profile_unsupported", "profile_failed",
        "profile_no_hand") and packets this SDK cannot read
        ("incompatible_firmware"; device "receiver" when the packet names
        none). Every status is also logged on the "pandora_sdk" logger. As
        with on(), it runs on the thread that hands messages over, and if it
        raises the source ends (Disconnected, "callback_failed"). None stops."""
        with self._changed:
            self._on_status = callback

    # ── the same requests as the app answers ─────────────────────────────

    def devices(self, timeout: Optional[float] = None) -> List[Device]:
        """The gloves heard so far, sending or not: those with a profile,
        and those read raw only as the default hand (unprofiled)."""
        return [Device(*known) for known in self._native.devices()]

    def subscribe(self, stream: str = "frames", *, devices: Optional[Sequence[str]] = None,
                  hands: Sequence[str] = protocol.HANDS, rate_hz: float = 0.0,
                  bones: bool = True, keys: Sequence[str] = ("*",), cancelled: bool = False,
                  timeout: Optional[float] = None) -> Subscription:
        """Ask for a stream, as Client.subscribe does; nothing is delivered
        before. Judged by the same code as the app's server. Raw with
        *cancelled* keeps each glove it includes going through its pipeline,
        as frames do, and each of its messages is sent once the pipeline has
        had the packet; hall is still the glove's own counts."""
        asked = Subscription(stream, None if devices is None else tuple(devices), tuple(hands),
                             rate_hz, bones, tuple(keys), cancelled)
        return Subscription.from_message(_answered(self._native.subscribe, asked.to_message()))

    # ── commands for a glove (what each does is on Source) ───────────────

    def play_rom(self, device: str, effects: Sequence[int],
                 timeout: Optional[float] = None) -> None:
        """As Client.play_rom. The app's haptic settings (which motors may
        run, the wearer's strength) are the app's: here every command goes
        as given."""
        _answered(self._native.play_rom, device, list(effects))

    def play_rtp(self, device: str, amplitudes: Sequence[int], durations_ms: Sequence[int],
                 timeout: Optional[float] = None) -> None:
        """As Client.play_rtp, with every command going as given (play_rom)."""
        _answered(self._native.play_rtp, device, list(amplitudes), list(durations_ms))

    def set_hall_range(self, device: str, hall_range: int,
                       timeout: Optional[float] = None, *, resend: bool = True) -> str:
        """As Client.set_hall_range. Every sensor gets the range with the
        other settings it powers up with, as the app sends them before the
        user has changed them; a sensor the profile has on a range of its own
        is on this one too from now on. The glove's pipeline takes the new
        range from its next packet. With *resend*, the glove is put on it
        again when it comes back after going silent (it may have been
        switched off and on); without, it is not, and a range remembered
        before is forgotten. The profile file is not changed. Returns "":
        the words the app gives are the app's; calibration(device).extrapolated
        says whether the bounds are a rescale."""
        _answered(self._native.set_hall_range, device, hall_range, resend)
        return ""

    def send_hall_config(self, device: str, *, reset: bool = False,
                         hall_range: int = HALL_DEFAULT_RANGE, osr: int = HALL_DEFAULT_OSR,
                         odr: int = HALL_DEFAULT_ODR, filter: int = HALL_DEFAULT_FILTER,
                         target: Union[int, str] = HALL_TARGET_ALL,
                         timeout: Optional[float] = None) -> None:
        """One Hall config command, exactly as given (what each setting is
        is on Source). Nothing is sent again by itself afterwards, and a
        range set_hall_range remembered for the glove is forgotten: a program
        sending its own Hall settings sends them again itself, after the
        glove went silent (a glove switched off and on is on its firmware's
        defaults). The range the command puts the sensors on (the default
        range after a reset) is what the glove's raw messages say and its
        pipeline is rescaled to, from its next packet: the whole glove's for
        every sensor, one sensor's range (raw sensor_ranges) for one."""
        _answered(self._native.send_hall_config, device, reset, hall_range, osr, odr, filter,
                  target)

    # ── calibration bounds ───────────────────────────────────────────────

    def latest_calibration(self, device: str) -> Optional[Calibration]:
        """The bounds *device*'s pipeline normalises with now; None for a
        glove not heard. Here they are read when asked, so they are never
        behind."""
        try:
            return self._calibration_of(device, None)
        except ServerError:
            return None

    def _calibration_of(self, device: str, timeout: Optional[float]) -> Calibration:
        """From the glove's own stores, as the app's server reads them. A
        glove with no pipeline yet gets one, loaded with its newest packet."""
        return protocol.parse_calibration(_answered(self._native.calibration, device))

    # ── the receiver ─────────────────────────────────────────────────────

    def on_line(self, callback: Optional[Callable[[str], object]]) -> None:
        """Call *callback* with each text line the receiver sends among the
        frames (its status: "BL:CONN ...", "PONG", "DFU:..." and the like),
        without its line end, in order with the packets; None stops. As
        with on(), it runs on the thread that hands messages over, so keep it
        short, and if it raises the source ends (Disconnected, code
        "callback_failed"). Without it, text lines are dropped. Only without
        the app: the app keeps the receiver's lines to itself."""
        with self._changed:
            self._on_line = callback
        self._native.want_lines(callback is not None)

    def _take_messages(self) -> None:
        """This object's thread: the runtime's messages to Source until it
        ends or this object is closed."""
        native = self._native
        why, code, cause = "", "closed", None
        try:
            while True:
                event = native.next_event(0.1)
                if event is None:
                    if self._stopping:
                        break
                    continue
                kind = event[0]
                if kind == "message":
                    _, stream, _device, data = event
                    self._deliver(stream, _DECODE[stream](data))
                elif kind == "packet":
                    with self._changed:
                        self.packets = event[1]
                        self._changed.notify_all()
                elif kind == "line":
                    self._line(event[1])
                elif kind == "status":
                    self._status(event[1], event[2], event[3])
                elif kind == "ended":
                    why, code = event[1], event[2]
                    cause = native.take_link_error()
                    if cause is None and code in ("unplugged", "read_failed"):
                        cause = OSError(why)
                    break
        except CallbackFailed as failed:
            native.stop()
            why, code, cause = str(failed), "callback_failed", failed.__cause__
        except Exception as failed:  # a message that would not decode must not pass for silence
            native.stop()
            why, code, cause = f"a glove's pipeline failed ({failed!r})", "pipeline_failed", failed
        self._end(why, code, cause)

    def _line(self, text: str) -> None:
        with self._changed:
            callback = self._on_line
        if callback is None:
            return
        try:
            callback(text)
        except Exception as failed:
            _log.exception("the callback for the receiver's lines raised")
            raise CallbackFailed(f"the callback for the receiver's lines raised "
                                 f"{failed!r}") from failed

    def _status(self, device: str, kind: str, message: str) -> None:
        trouble = kind in ("incompatible_firmware", "profile_failed", "profile_no_hand")
        (_log.warning if trouble else _log.info)("%s: %s", device, message)
        with self._changed:
            if kind.startswith("profile_"):
                self._profile_status[device] = kind
            callback = self._on_status
        if callback is None:
            return
        try:
            callback(device, kind, message)
        except Exception as failed:
            _log.exception("the callback for glove statuses raised")
            raise CallbackFailed(f"the callback for glove statuses raised {failed!r}") from failed

    def close(self) -> None:
        """Stop reading and close the port. Unread messages can still be read."""
        with self._changed:
            self._closed = True
            self._changed.notify_all()
        self._stopping = True
        self._native.close()
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=2.0)
        if self._link is not None:
            self._link.close()


def _answered(request: Callable[..., Any], *args: Any) -> Any:
    """*request* to the runtime; what it refuses is a ServerError with the
    protocol's code, as the app's server refuses a request."""
    try:
        return request(*args)
    except runtime.Refused as refused:
        code, message = refused.args
        raise ServerError(code, message) from None
