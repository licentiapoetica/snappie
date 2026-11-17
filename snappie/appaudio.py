"""Pick the apps (and the microphone) a recording hears, also while it runs (PipeWire).

Desktop audio as a sink's monitor mixes in everything playing there, voice chat included.
Instead each recording gets a private null sink, and the chosen apps' playback streams are
linked into it *in addition* to where they already play (they stay audible).  The default
microphone is linked in the same way, so muting it is removing two links: no pipeline
change, and the mic is only open while it's on.  The recorder records the sink's monitor
like any other device.  PipeWire mixes whatever is linked into an input port, and the sink
keeps producing silence while nothing is linked, so the audio track never stalls the muxer.

Why not a pipewiresrc that nothing auto-connects, with the apps linked straight into it:
its state change blocks until the stream has a format, which it only gets once something
is linked.  With no chosen app playing yet, the recording would never start.

The sink and the links are created by a `pw-cli` child and are not lingering objects, so
they disappear with it, also if Snappie crashes (PR_SET_PDEATHSIG: pw-cli never exits by
itself, not even on stdin EOF).  New streams (a browser tab, a game reopening its audio)
are linked as they appear: `pw-link -m` reports new ports, then a `pw-dump` scan relinks.

The planning functions work on parsed `pw-dump` output and need no running PipeWire.
"""

import ctypes
import json
import os
import select
import signal
import subprocess
import threading
import time

from .app_util import log

TOOLS = ("pw-cli", "pw-dump", "pw-link")
MODES = ("all", "only", "except")
LEFT = {"FL", "SL", "RL", "FLC", "FLW", "TFL", "TRL", "TSL", "RLC"}
RIGHT = {"FR", "SR", "RR", "FRC", "FRW", "TFR", "TRR", "TSR", "RRC"}
SKIP = {"LFE", "LFE2"}


# ---------------------------------------------------------------------- planning (pure)

def _by_type(dump, kind):
    return [o for o in dump if o.get("type") == "PipeWire:Interface:" + kind and o.get("info")]


def app_name(props):
    """What people call the app: "Firefox", "vesktop", "TeamSpeak"."""
    return (props.get("application.name") or props.get("node.name")
            or props.get("application.process.binary") or "?")


def stream_keys(props):
    """Lowercase names a chosen app may be written as: app name, node name, binary, Flatpak id."""
    keys = set()
    for k in ("application.name", "node.name", "application.process.binary", "pipewire.access.portal.app_id",
              "application.id"):
        v = props.get(k)
        if v:
            keys.add(str(v).strip().lower())
    return keys


def is_app_stream(props, own_pid=None):
    """An app's playback stream.  Filters and loopbacks (EasyEffects, virtual sinks) also have
    output streams, but those carry other apps' sound: linking one would bring back exactly
    what was left out.  They are recognisable by their link group."""
    if props.get("media.class") != "Stream/Output/Audio" or props.get("node.link-group"):
        return False
    if str(props.get("node.name", "")).startswith("snappie"):
        return False
    pid = props.get("application.process.id")
    return own_pid is None or str(pid) != str(own_pid)


def channel_targets(channel):
    """Our sink's channels (stereo) that an app port feeds."""
    ch = (channel or "").upper()
    if ch in SKIP:
        return ()
    if ch in LEFT:
        return ("FL",)
    if ch in RIGHT:
        return ("FR",)
    return ("FL", "FR")   # MONO, FC, RC, AUX…: both sides


class Selection:
    """Which apps go into the recording.  mode: all | only | except (names: app names, any case).
    Ticks (sound chooser, recording bar) override the rule per app, by its display name;
    apps that start playing later follow the rule.  mic: the default microphone is linked."""

    def __init__(self, mode, names, mic=False):
        self.mode = mode
        self.names = set() if mode == "all" else {n.strip().lower() for n in names or () if n.strip()}
        self.on, self.off = set(), set()
        self.mic = mic

    def wants(self, props):
        name = app_name(props).lower()
        if name in self.on:
            return True
        if name in self.off:
            return False
        hit = bool(stream_keys(props) & self.names)
        return hit if self.mode == "only" else not hit

    def set(self, name, on):
        name = name.lower()
        (self.on if on else self.off).add(name)
        (self.off if on else self.on).discard(name)


def streams(dump, own_pid=None):
    """[(node id, props, {channel: port id})] for apps playing right now."""
    nodes = {o["id"]: o["info"].get("props", {}) for o in _by_type(dump, "Node")}
    out = {nid: (nid, p, {}) for nid, p in nodes.items() if is_app_stream(p, own_pid)}
    for port in _by_type(dump, "Port"):
        p = port["info"].get("props", {})
        s = out.get(p.get("node.id"))
        if s and p.get("port.direction") == "out" and not p.get("port.monitor"):
            s[2][p.get("audio.channel") or p.get("port.name", "")] = port["id"]
    return list(out.values())


def apps(dump, own_pid=None):
    """Display names of the apps playing right now, sorted."""
    names = {app_name(p) for _, p, _ in streams(dump, own_pid)}
    return sorted(names, key=str.lower)


def choices(dump, selection, own_pid=None):
    """[(app name, recorded?, its stream keys)] for the apps playing now; an app counts as
    recorded if any of its streams is."""
    items = {}
    for _, props, _ in streams(dump, own_pid):
        name = app_name(props)
        on, keys = items.get(name, (False, set()))
        items[name] = (on or selection.wants(props), keys | stream_keys(props))
    return [(n, on, keys) for n, (on, keys) in sorted(items.items(), key=lambda kv: kv[0].lower())]


def menu_items(dump, selection, own_pid=None):
    """[(app name, recorded?)]."""
    return [(n, on) for n, on, _ in choices(dump, selection, own_pid)]


def default_source(dump):
    """(node id, {channel: port id}) of the default microphone, or None."""
    name = None
    for o in dump:
        if o.get("type") == "PipeWire:Interface:Metadata" and \
                (o.get("props") or {}).get("metadata.name") == "default":
            entries = {m.get("key"): m.get("value") for m in o.get("metadata") or []}
            for key in ("default.audio.source", "default.configured.audio.source"):
                v = entries.get(key)
                if isinstance(v, dict) and v.get("name"):
                    name = v["name"]
                    break
    if not name:
        return None
    nid = next((o["id"] for o in _by_type(dump, "Node") if o["info"].get("props", {}).get("node.name") == name
                and str(o["info"]["props"].get("media.class", "")).startswith("Audio/")), None)
    if nid is None:
        return None
    ports = {}
    for port in _by_type(dump, "Port"):
        p = port["info"].get("props", {})
        if p.get("node.id") == nid and p.get("port.direction") == "out" and not p.get("port.monitor"):
            ports[p.get("audio.channel") or p.get("port.name", "")] = port["id"]
    return nid, ports


def remembered(mode, names, ticks):
    """The rule to store after the sound chooser, so it opens the same way next time.
    ticks: {app name: (on, its stream keys)} -> (mode, names)."""
    kept = list(names or [])
    for app, (on, keys) in ticks.items():
        keys = set(keys) | {app.lower()}
        listed = mode == "only"     # does being on the list mean "recorded"?
        kept = [n for n in kept if n.strip().lower() not in keys]
        if on == listed:
            kept.append(app)
    if mode == "only":
        return "only", kept
    return ("except" if kept else "all"), kept


def plan(dump, sink_name, selection, own_pid=None):
    """(links to create [(out node, out port, in node, in port)], link ids to remove) so that
    exactly the chosen apps (and the mic, if on) feed the sink.  None if the sink isn't there (yet)."""
    sink = next((o["id"] for o in _by_type(dump, "Node")
                 if o["info"].get("props", {}).get("node.name") == sink_name), None)
    if sink is None:
        return None
    inputs = {}
    for port in _by_type(dump, "Port"):
        p = port["info"].get("props", {})
        if p.get("node.id") == sink and p.get("port.direction") == "in":
            inputs[p.get("audio.channel")] = port["id"]
    want = set()
    for nid, props, ports in streams(dump, own_pid):
        if not selection.wants(props):
            continue
        for ch, port in ports.items():
            for target in channel_targets(ch):
                if target in inputs:
                    want.add((nid, port, sink, inputs[target]))
    mic = selection.mic and default_source(dump)
    if mic:
        for ch, port in mic[1].items():
            for target in channel_targets(ch):
                if target in inputs:
                    want.add((mic[0], port, sink, inputs[target]))
    have = {}
    for link in _by_type(dump, "Link"):
        i = link["info"]
        if i.get("input-node-id") == sink:
            have[(i.get("output-node-id"), i.get("output-port-id"), sink, i.get("input-port-id"))] = link["id"]
    create = sorted(want - set(have))
    remove = sorted(lid for key, lid in have.items() if key not in want)
    return create, remove


# ---------------------------------------------------------------------- running

def available():
    import shutil
    return all(shutil.which(t) for t in TOOLS)


def dump():
    r = subprocess.run(["pw-dump", "--no-colors"], capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "pw-dump failed")
    return json.loads(r.stdout or "[]")


def playing_apps():
    """Apps playing sound right now (for the settings list); [] when PipeWire can't be asked."""
    try:
        return apps(dump(), os.getpid())
    except Exception as e:
        log("can't list apps playing sound:", e)
        return []


_prctl = None


def _die_with_parent():
    # runs in the child between fork and exec: only the prepared libc call
    if _prctl:
        _prctl(1, signal.SIGKILL)   # PR_SET_PDEATHSIG


def _spawn(argv, **kw):
    """A child that is killed when we die.  Call from the GUI thread: the kernel sends the
    signal when the *thread* that forked exits, and a worker thread would end too soon."""
    global _prctl
    if _prctl is None:
        try:
            _prctl = ctypes.CDLL(None, use_errno=True).prctl
        except (OSError, AttributeError):
            _prctl = False
    return subprocess.Popen(argv, preexec_fn=_die_with_parent, **kw)


class AppAudio:
    """The private sink for one recording, kept linked to the chosen apps until `stop`.
    ticks: {app name: on} from the sound chooser, on top of the rule."""

    def __init__(self, mode, names, mic=False, ticks=None):
        self.selection = Selection(mode, names, mic)
        for name, on in (ticks or {}).items():
            self.selection.set(name, on)
        self.sink = f"snappie_apps_{os.getpid()}"
        self.device = self.sink + ".monitor"   # what pulsesrc records
        self._pid = os.getpid()
        self._cli = self._mon = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = False
        self._linked = None        # app names currently linked (for the log)
        self.last_dump = []

    def start(self):
        """Create the sink and wait (briefly) until it exists.  Raises RuntimeError."""
        if not available():
            from .deps import install_hint
            raise RuntimeError(f"needs the PipeWire tools pw-cli, pw-dump and pw-link ({install_hint(['pw-tools'])})")
        self._cli = _spawn(["pw-cli"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, text=True)
        # node.virtual: Plasma's volume applet lists it only with "Show virtual devices";
        # priority 0: never chosen as the default output
        self._send("create-node adapter { factory.name=support.null-audio-sink"
                   f" node.name={self.sink} node.description=\"Snappie recording\""
                   " media.class=Audio/Sink audio.position=[ FL FR ] object.linger=false"
                   " node.virtual=true priority.session=0 priority.driver=0 }")
        deadline = time.monotonic() + 3
        while True:
            d = dump()
            if plan(d, self.sink, self.selection, self._pid) is not None and self._has_inputs(d):
                break
            if time.monotonic() > deadline or self._cli.poll() is not None:
                self.stop()
                raise RuntimeError("PipeWire didn't create the recording sink")
            time.sleep(0.03)
        self._apply(d)
        try:
            self._mon = _spawn(["pw-link", "-m", "-o"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            log("app audio: no port monitor, checking every few seconds:", e)
        threading.Thread(target=self._run, name="app-audio", daemon=True).start()

    def _has_inputs(self, d):
        sink = next(o["id"] for o in _by_type(d, "Node") if o["info"].get("props", {}).get("node.name") == self.sink)
        return sum(1 for o in _by_type(d, "Port") if o["info"].get("props", {}).get("node.id") == sink
                   and o["info"].get("props", {}).get("port.direction") == "in") >= 2

    def set_app(self, name, on):
        """Tick / untick an app during the recording (GUI thread)."""
        with self._lock:
            self.selection.set(name, on)
        log(f"app audio: {name} {'on' if on else 'off'}")
        self._wake.set()

    def set_mic(self, on):
        with self._lock:
            self.selection.mic = on
        log(f"microphone {'on' if on else 'off'}")
        self._wake.set()

    @property
    def mic(self):
        return self.selection.mic

    def menu(self):
        """[(app name, recorded?)] for the apps playing now (GUI thread, runs pw-dump)."""
        try:
            d = dump()
        except Exception as e:
            log("app audio: can't list apps:", e)
            d = self.last_dump
        with self._lock:
            return menu_items(d, self.selection, self._pid)

    def stop(self):
        self._stopped = True
        self._wake.set()
        for p in (self._mon, self._cli):
            if p and p.poll() is None:
                p.kill()
                p.wait()
        self._mon = self._cli = None

    def _send(self, line):
        cli = self._cli
        if cli and cli.poll() is None:
            cli.stdin.write(line + "\n")
            cli.stdin.flush()

    def _apply(self, d):
        self.last_dump = d
        with self._lock:
            p = plan(d, self.sink, self.selection, self._pid)
            names = {app_name(props) for _, props, _ in streams(d, self._pid) if self.selection.wants(props)}
        if p is None:
            return
        create, remove = p
        for out_node, out_port, in_node, in_port in create:
            self._send(f"create-link {out_node} {out_port} {in_node} {in_port}")
        for lid in remove:
            self._send(f"destroy {lid}")
        if names != self._linked:
            log("app audio: recording", ", ".join(sorted(names, key=str.lower)) or "no app")
            self._linked = names

    def _run(self):
        if self._mon:
            threading.Thread(target=self._watch_ports, name="app-audio-ports", daemon=True).start()
        while not self._stopped:
            self._wake.wait(5)   # also rescan now and then, in case an event was missed
            if self._stopped:
                break
            time.sleep(0.05)     # a new stream adds its ports one by one
            self._wake.clear()
            try:
                self._apply(dump())
            except Exception as e:
                log("app audio: relinking failed:", e)

    def _watch_ports(self):
        mon = self._mon
        try:
            while not self._stopped and mon.poll() is None:
                ready, _, _ = select.select([mon.stdout], [], [], 1)
                if ready:
                    if not os.read(mon.stdout.fileno(), 65536):
                        break
                    self._wake.set()
        except (OSError, ValueError):
            pass
