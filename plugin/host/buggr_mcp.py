#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial>=3.5"]
# ///
"""BuggrBox MCP server (stdio). A generated adapter: every tool comes from the device manifest
(device.info), so a method added to the firmware's method table appears here after a reflash with
no change to this file.

    python plugin/host/buggr_mcp.py                                 USB CDC (VID 303A PID 83CC)
    uv run --script plugin/host/buggr_mcp.py                        the same with no Python setup (what the plugin runs)
    uvx --no-cache --from ./plugin/host buggrbox-mcp                the same, as the packaged command
    BUGGR_HTTP=buggrbox.local BUGGR_TOKEN=<token> python plugin/host/buggr_mcp.py   over the LAN

Registered for this repository in .mcp.json, and shipped in the Claude Code plugin (plugin/.mcp.json). Over USB the port is opened for each call and released
after it, so otaflash and the bench scripts can run between tool calls (not during one).

Tool names are the method names with dots as underscores (power.set -> power_set). Device errors
come back as tool errors whose text carries data.code and data.hint. Methods the manifest marks
site=host (usb.wait, debug.flash) are evaluated here. adapter_status reports the connection and reloads the
manifest (call it after a reflash).

Every manifest read from a box is cached per user, one file per box (USB serial, or LAN host). A session
that starts while the box is unplugged or busy lists its tools from that cache, so it still has them when
the box turns up; until the box answers, every call is refused with E_TRANSPORT rather than run against a
stale manifest, and the first live manifest replaces the cache (tools/list_changed if the set differs).

Environment:
    BUGGR_HTTP        LAN host instead of USB; BUGGR_TOKEN is the bearer token from net.token.get
    BUGGR_OWNER       lease owner name over HTTP (default "mcp")
    BUGGR_SERIAL      USB serial number (the box's MAC, e.g. AA:BB:CC:11:22:33) when several boxes are plugged in
    BUGGR_CACHE       manifest cache directory (default %LOCALAPPDATA%\buggrbox, else $XDG_CACHE_HOME or ~/.cache/buggrbox)
    BUGGR_BENCH_MAP   JSON file; {"duts": {"1": {"vid": "10C4", "pid": "EA60"}}} lets usb.wait
                      match a DUT by VID/PID instead of "the first new serial port"

Standard library plus pyserial. Logs go to stderr; stdout carries only the protocol.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:          # HTTP-only use does not need it
    serial = None
    list_ports = None

# The box's own PID (brief §8 item 48), never 303A:4001: that is ESP-IDF's TinyUSB default, which an
# ESP32-S3 DUT may enumerate as. Provisional until espressif/usb-pids lists it; s3/tools/boxusb.py too.
BOX_VID, BOX_PID = 0x303A, 0x83CC
__version__ = "0.1.0"                 # the package version too (pyproject.toml reads it)
PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
INSTRUCTIONS = (
    "BuggrBox is a bench instrument that gives you physical access to embedded prototypes (DUTs): "
    "power, current measurement, GPIO, USB sequencing, recovery. Call device_info first and trust it: "
    "it states every range and enforced limit, which limits are hardware and which are policy, and "
    "whether the safety hardware is simulated. Errors carry data.code and data.hint; act on the hint. "
    "Writes are refused with E_LEASE while another client holds the bench lease (lease_acquire)."
)


def norm_serial(sn):
    return "".join(c for c in (sn or "").upper() if c in "0123456789ABCDEF")


def log(*args):
    print("buggr-mcp:", *args, file=sys.stderr, flush=True)


def cache_dir():
    d = os.environ.get("BUGGR_CACHE")
    if not d:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        d = os.path.join(base, "buggrbox")
    return d


# ---------------------------------------------------------------------------------- transports

class UsbDevice:
    name = "usb-cdc"

    def __init__(self):
        self.s = None
        self.port = None
        self.serial = None
        self.n = 0

    def identity(self):
        """The box this transport reaches, for the manifest cache: its USB serial (base MAC)."""
        return norm_serial(self.serial) or None

    def wanted(self):
        return norm_serial(os.environ.get("BUGGR_SERIAL")) or None

    def _open(self, wait_s):
        if serial is None:
            raise ConnectionError("pyserial is not installed (pip install pyserial)")
        want = norm_serial(os.environ.get("BUGGR_SERIAL"))
        deadline = time.time() + wait_s
        while True:
            boxes = [p for p in list_ports.comports() if p.vid == BOX_VID and p.pid == BOX_PID
                     and (not want or norm_serial(p.serial_number) == want)]
            if len(boxes) > 1:
                raise ConnectionError("several BuggrBoxes on USB (serials " + ", ".join(str(p.serial_number) for p in boxes)
                                      + "): set BUGGR_SERIAL to the one to use")
            for p in boxes:
                try:
                    self.s = serial.Serial(p.device, 115200, timeout=0.5)
                except serial.SerialException as exc:
                    raise ConnectionError(f"{p.device} is in use by another program (a bench script, esptool, a terminal?); "
                                          f"call again when it is free ({exc})")
                if self.port != p.device:
                    log("box on", p.device)
                self.port = p.device
                self.serial = p.serial_number
                time.sleep(0.2)
                self.s.reset_input_buffer()
                return
            if time.time() >= deadline:
                raise ConnectionError(f"BuggrBox not found on USB (VID 303A PID 83CC{', serial ' + want if want else ''}): plugged in, running its app?")
            time.sleep(0.5)

    def close(self):
        if self.s is not None:
            try:
                self.s.close()
            except Exception:
                pass
        self.s = None

    def call(self, method, params=None, timeout=10.0, wait_s=5.0):
        # The port is held only for the duration of one call: Windows COM ports are exclusive, and a
        # session that kept it open would lock out otaflash and the bench scripts between tool calls.
        self._open(wait_s=wait_s)           # waits a little: the box may be mid-reboot
        self.n += 1
        req = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params:
            req["params"] = params
        try:
            self.s.write((json.dumps(req, separators=(",", ":")) + "\n").encode())
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = self.s.readline()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == self.n:
                    return msg
            raise TimeoutError(f"no reply to {method} within {timeout:.0f} s")
        except TimeoutError:
            raise
        except (OSError, serial.SerialException) as exc:
            # Never resend: the write may already have been applied.
            raise ConnectionError(f"USB link lost during {method} ({exc}); the box may be rebooting; call again")
        finally:
            self.close()


class HttpDevice:
    name = "http"

    def __init__(self, host, token, owner):
        self.host = host
        self.url = f"http://{host}/rpc"
        self.port = self.url
        self.token = token
        self.owner = owner
        self.n = 0

    def identity(self):
        return self.host

    def wanted(self):
        return self.host

    def close(self):
        pass

    def call(self, method, params=None, timeout=10.0, wait_s=None):
        self.n += 1
        body = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params:
            body["params"] = params
        headers = {"Content-Type": "application/json", "X-Buggr-Owner": self.owner}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read())
            except ValueError:
                raise ConnectionError(f"HTTP {exc.code} from {self.url}")
        except (urllib.error.URLError, OSError) as exc:
            raise ConnectionError(f"cannot reach {self.url}: {exc}")


# ---------------------------------------------------------------------------------- host-site methods

def _bench_map():
    path = os.environ.get("BUGGR_BENCH_MAP")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        log("bench map unreadable:", exc)
        return {}


def host_usb_wait(args, dev):
    if list_ports is None:
        return tool_error("E_UNSUPPORTED", "pyserial is not installed on the host")
    dut = args.get("dut")
    timeout_s = min(max(int(args.get("timeout_ms", 10000)), 0), 120000) / 1000.0
    want = (_bench_map().get("duts") or {}).get(str(dut)) or {}
    vid = int(str(want["vid"]), 16) if want.get("vid") else None
    pid = int(str(want["pid"]), 16) if want.get("pid") else None
    before = {p.device for p in list_ports.comports()}
    deadline = time.time() + timeout_s
    while True:
        for p in list_ports.comports():
            if p.vid is None or (p.vid == BOX_VID and p.pid == BOX_PID):
                continue
            hit = (p.vid == vid and (pid is None or p.pid == pid)) if vid is not None else p.device not in before
            if hit:
                return tool_ok({"port": p.device, "vid": f"{p.vid:04X}", "pid": f"{p.pid:04X}",
                                "description": p.description,
                                "matched": "bench map VID/PID" if vid is not None else "first new serial port (no bench map entry for this DUT)"})
        if time.time() >= deadline:
            what = f"VID {want.get('vid')} PID {want.get('pid')}" if vid is not None else "a new serial port"
            return tool_error("E_NOT_PRESENT", f"no {what} enumerated within {int(timeout_s * 1000)} ms; check the DUT cable and usb/power state")
        time.sleep(0.2)


def host_debug_flash(args, dev):
    """Flash the SWD target with probe-rs through the box's CMSIS-DAP v2 interface (same USB device)."""
    if dev.name != UsbDevice.name:
        return tool_error("E_UNSUPPORTED", "debug.flash needs the USB transport: the CMSIS-DAP interface is on the box's USB. "
                                           "Over the LAN, debug.read/debug.write work; flashing over the LAN is not available yet")
    exe = shutil.which("probe-rs")
    if not exe:
        return tool_error("E_UNSUPPORTED", "probe-rs is not installed on this host (https://probe.rs); install it and call again")
    path, chip = args.get("file"), args.get("chip")
    if not path or not os.path.isfile(path):
        return tool_error("E_INVALID_PARAMS", f"file {path!r} does not exist on the host running this adapter")
    ext = os.path.splitext(path)[1].lower()
    base = args.get("base_address")
    if ext == ".bin" and base is None:
        return tool_error("E_INVALID_PARAMS", "a .bin image needs base_address (e.g. 0x08000000)")

    try:
        st = dev.call("debug.status")
        if not (st.get("result") or {}).get("enabled"):
            p = {"enable": True}
            for k in ("swclk", "khz"):
                if args.get(k) is not None:
                    p[k] = args[k]
            r = dev.call("debug.probe", p)
            if "error" in r:
                d = r["error"].get("data") or {}
                return tool_error(d.get("code", "E_INTERNAL"), d.get("hint", ""), r["error"].get("message"))
    except (ConnectionError, TimeoutError) as exc:
        return tool_error("E_TRANSPORT", str(exc))

    probe = f"{BOX_VID:04x}:{BOX_PID:04x}"
    cmd = [exe, "download", "--probe", probe, "--chip", chip]
    if args.get("khz"):
        cmd += ["--speed", str(int(args["khz"]))]
    if ext == ".bin":
        cmd += ["--binary-format", "bin", "--base-address", str(base)]
    elif ext in (".hex", ".ihex"):
        cmd += ["--binary-format", "hex"]
    if args.get("verify", True):
        cmd.append("--verify")
    cmd.append(path)
    t0 = time.time()
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        out = (run.stdout or "") + (run.stderr or "")
        ok = run.returncode == 0
        if ok and args.get("reset", True):
            rr = subprocess.run([exe, "reset", "--probe", probe, "--chip", chip], capture_output=True, text=True, timeout=60)
            out += (rr.stdout or "") + (rr.stderr or "")
            ok = rr.returncode == 0
    except subprocess.TimeoutExpired:
        return tool_error("E_BUSY", "probe-rs did not finish within 300 s; lower khz, or check the target with debug.connect")
    result = {"ok": ok, "seconds": round(time.time() - t0, 1), "output": out[-2000:]}
    if not ok:
        return tool_error("E_HW_FAULT", "probe-rs failed; its output follows. debug.connect shows whether the target answers",
                          json.dumps(result))
    return tool_ok(result)


HOST_METHODS = {"usb.wait": host_usb_wait, "debug.flash": host_debug_flash}


# ---------------------------------------------------------------------------------- MCP server

def tool_ok(result):
    return {"content": [{"type": "text", "text": json.dumps(result, separators=(",", ":"))}], "isError": False}


def tool_error(code, hint, message=None):
    err = {"code": code, "hint": hint}
    if message:
        err["message"] = message
    return {"content": [{"type": "text", "text": json.dumps(err, separators=(",", ":"))}], "isError": True}


ADAPTER_STATUS = {
    "name": "adapter_status",
    "description": "This MCP adapter's own state: transport, whether the BuggrBox answers, the firmware and "
                   "number of methods loaded, the safety backend. Reloads the manifest: call it after the box "
                   "was reflashed or reconnected.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}


class Server:
    def __init__(self, dev):
        self.dev = dev
        self.manifest = None
        self.tools = {}      # tool name -> manifest method entry
        self.live = False    # manifest read from the box this session (else from the cache, or none)
        self.cached = None   # {"identity", "saved_at", "path"} while tools come from the cache
        self.last_error = None

    def _set_manifest(self, manifest):
        self.manifest = manifest
        self.tools = {m["name"].replace(".", "_"): m for m in manifest.get("methods", [])}

    def load_manifest(self, wait_s=5.0):
        r = self.dev.call("device.info", wait_s=wait_s)
        if "result" not in r:
            raise ConnectionError(f"device.info failed: {r.get('error')}")
        self._set_manifest(r["result"])
        self.live, self.cached, self.last_error = True, None, None
        log(f"manifest loaded: {len(self.tools)} methods")
        self.save_cache()

    def save_cache(self):
        ident = self.dev.identity()
        if not ident:
            return
        d = cache_dir()
        path = os.path.join(d, "manifest-" + "".join(c if c.isalnum() else "_" for c in ident) + ".json")
        try:
            os.makedirs(d, exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"transport": self.dev.name, "identity": ident, "saved_at": int(time.time()),
                           "manifest": self.manifest}, f)
            os.replace(path + ".tmp", path)
        except OSError as exc:                        # a read-only home must not cost the session
            log("manifest cache not written:", exc)

    def load_cache(self):
        """The cached manifest of the box this transport is pointed at (BUGGR_SERIAL / BUGGR_HTTP),
        else of the box this transport reached most recently."""
        want, best = self.dev.wanted(), None
        try:
            names = os.listdir(cache_dir())
        except OSError:
            return False
        for n in names:
            if not (n.startswith("manifest-") and n.endswith(".json")):
                continue
            path = os.path.join(cache_dir(), n)
            try:
                with open(path, encoding="utf-8") as f:
                    c = json.load(f)
            except (OSError, ValueError):
                continue
            if c.get("transport") != self.dev.name or not isinstance(c.get("manifest"), dict):
                continue
            if want and c.get("identity") != want:
                continue
            if best is None or c.get("saved_at", 0) > best[0].get("saved_at", 0):
                best = (c, path)
        if best is None:
            return False
        c, path = best
        self._set_manifest(c["manifest"])
        self.cached = {"identity": c.get("identity"), "saved_at": c.get("saved_at"), "path": path}
        log(f"box not answering; {len(self.tools)} tools listed from the manifest cached for {c.get('identity')}")
        return True

    def ensure_manifest(self, wait_s=5.0):
        if self.live:
            return
        try:
            self.load_manifest(wait_s)
        except (ConnectionError, TimeoutError) as exc:
            self.last_error = str(exc)
            log("manifest unavailable:", exc)
            if self.manifest is None:
                self.load_cache()

    def tool_list(self):
        self.ensure_manifest(wait_s=0.5)              # session start: do not stall on an absent box
        tools = [ADAPTER_STATUS]
        for name, m in self.tools.items():
            if m.get("site") == "host" and m["name"] not in HOST_METHODS:
                continue
            notes = []
            if m.get("needs_lease"):
                notes.append("write: E_LEASE while another client holds the bench lease")
            if m.get("transport") == "usb":
                notes.append("USB transport only")
            if m.get("site") == "host":
                notes.append("evaluated by this adapter on the host")
            desc = m.get("summary", "") + (f" ({'; '.join(notes)})" if notes else "")
            schema = dict(m.get("params") or {})
            schema.setdefault("type", "object")
            tools.append({"name": name, "description": desc, "inputSchema": schema})
        return tools

    def adapter_status(self):
        st = {"transport": self.dev.name, "endpoint": self.dev.port}
        try:
            self.load_manifest()
            fw = self.manifest.get("firmware") or {}
            st.update(connected=True, firmware=fw, methods=len(self.tools), manifest="live",
                      safety_backend=(self.manifest.get("safety") or {}).get("backend"))
        except (ConnectionError, TimeoutError) as exc:
            self.last_error = str(exc)
            st.update(connected=False, error=str(exc))
            if self.manifest is None:
                self.load_cache()
            if self.cached:
                st.update(manifest="cached", cached_for=self.cached["identity"],
                          cached_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.cached["saved_at"] or 0)),
                          methods=len(self.tools))
        return tool_ok(st)

    def call_tool(self, name, args):
        if name == "adapter_status":
            return self.adapter_status()
        self.ensure_manifest()
        if not self.live:
            # Tools listed from the cache run only once the box itself has answered: a call is never
            # built from a manifest the box has not just confirmed.
            return tool_error("E_TRANSPORT", f"the BuggrBox is not answering ({self.last_error or 'not found'}); "
                                             "plug it in (or free its port) and call again, or call adapter_status")
        m = self.tools.get(name)
        if m is None:
            return tool_error("E_UNSUPPORTED", f"no tool '{name}'; the loaded manifest has {len(self.tools)} methods "
                                               "(reflashed? call adapter_status to reload)")
        if m.get("site") == "host":
            impl = HOST_METHODS.get(m["name"])
            return impl(args or {}, self.dev) if impl else tool_error("E_UNSUPPORTED", f"{m['name']} is host-evaluated and not implemented in this adapter")
        try:
            # a device-side wait (uart.wait) answers only after its timeout_ms
            wait_s = min(max(float((args or {}).get("timeout_ms") or 0), 0.0), 600000.0) / 1000.0
            r = self.dev.call(m["name"], args or None, timeout=10.0 + wait_s)
        except (ConnectionError, TimeoutError) as exc:
            return tool_error("E_TRANSPORT", str(exc))
        if "error" in r:
            e = r["error"]
            d = e.get("data") or {}
            return tool_error(d.get("code", "E_INTERNAL"), d.get("hint", ""), e.get("message"))
        return tool_ok(r.get("result"))


CLI_USAGE = """buggrbox-mcp                   the MCP server (stdio); what an MCP client runs
buggrbox-mcp wifi status       the box's WiFi state, over USB
buggrbox-mcp wifi join <ssid>  asks for the WiFi password here (or $BUGGR_PSK), waits for an IP
buggrbox-mcp wifi forget       erase the credentials and stop the radio
buggrbox-mcp --version

Run the wifi commands in your own terminal: the password goes USB -> the box and nowhere else,
never through a chat."""


def cli(argv):
    if argv[0] in ("--version", "-V"):
        print(__version__)
        return 0
    if argv[:1] != ["wifi"] or len(argv) < 2 or argv[1] not in ("status", "join", "forget"):
        print(CLI_USAGE)
        return 2
    dev = UsbDevice()

    def call(method, params=None):
        try:
            r = dev.call(method, params)
        except (ConnectionError, TimeoutError) as exc:
            sys.exit(str(exc))
        if "error" in r:
            e = r["error"]
            sys.exit(f"{method}: {(e.get('data') or {}).get('code', e.get('message'))}: {(e.get('data') or {}).get('hint', '')}")
        return r["result"]

    if argv[1] == "status":
        print(json.dumps(call("net.wifi.status"), indent=2))
        return 0
    if argv[1] == "forget":
        print(json.dumps(call("net.wifi.forget"), indent=2))
        return 0
    if len(argv) < 3:
        sys.exit("wifi join needs the network name (SSID); it is case-sensitive")
    ssid, psk = argv[2], os.environ.get("BUGGR_PSK")
    if psk is None:
        if not sys.stdin.isatty():
            sys.exit("run this in your own terminal: it asks for the WiFi password, which must not pass through a chat")
        import getpass
        psk = getpass.getpass(f"WiFi password for '{ssid}' (empty = open network): ")
    call("net.wifi.set", {"ssid": ssid, "psk": psk})
    print("stored on the box; waiting for an IP")
    t0, st = time.time(), {}
    while time.time() - t0 < 30:
        st = call("net.wifi.status")
        if st.get("state") == "connected":
            break
        time.sleep(1)
    if st.get("state") != "connected":
        print(f"not connected after 30 s (last_reason={st.get('last_reason')}; 201 = network not found: "
              f"check the SSID's exact spelling and case; 15, 202 or 204 usually = wrong password)")
        return 1
    print(f"connected: ip={st.get('ip')} hostname={st.get('hostname')}.local rssi={st.get('rssi')} dBm")
    return 0


def main():
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    host = os.environ.get("BUGGR_HTTP")
    dev = HttpDevice(host, os.environ.get("BUGGR_TOKEN", ""), os.environ.get("BUGGR_OWNER", "mcp")) if host else UsbDevice()
    srv = Server(dev)
    out = sys.stdout.buffer

    def send(obj):
        out.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")
        out.flush()

    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            break
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        if not isinstance(msg, dict) or "method" not in msg:
            continue                                  # responses to requests we never send
        mid, method = msg.get("id"), msg["method"]
        if mid is None:
            continue                                  # notifications (initialized, cancelled)
        try:
            if method == "initialize":
                asked = (msg.get("params") or {}).get("protocolVersion")
                result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                          "capabilities": {"tools": {"listChanged": True}},
                          "serverInfo": {"name": "buggrbox", "version": __version__},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": srv.tool_list()}
            elif method == "tools/call":
                p = msg.get("params") or {}
                before = set(srv.tools)
                result = srv.call_tool(p.get("name"), p.get("arguments") or {})
                # also when the first manifest arrives: a session started with the box absent lists
                # only adapter_status until the client is told to fetch the list again
                if set(srv.tools) != before:
                    send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as exc:                      # never let one bad call kill the session
            log("internal error:", repr(exc))
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}})
    dev.close()


if __name__ == "__main__":
    main()
