#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial>=3.5"]
# ///
"""End-to-end check of plugin/host/buggr_mcp.py against a connected BuggrBox running a simulated-safety
build, speaking MCP over stdio exactly as a client would.

    python plugin/host/test_mcp.py
    BUGGR_MCP_CMD="uv run --script plugin/host/buggr_mcp.py" python plugin/host/test_mcp.py   as the plugin runs it

Steps: initialize (version, server name, instructions); tools/list is generated from the manifest
(dots become underscores, every tool has an object inputSchema, host-site usb_wait and debug_flash included);
adapter_status sees the box; a device refusal becomes a tool error with code and hint; a rail is
set, enabled, shorted with sim_set and the trip shows in events_read; the bench is left quiet; an
unknown tool and a usb_wait timeout are tool errors; closing stdin ends the server. Then the
manifest cache, with the box's COM port held so the adapter cannot reach it: a fresh adapter lists
the cached tools, refuses calls with E_TRANSPORT, reports the cache in adapter_status and goes live
once the port is free; with an empty cache it lists adapter_status alone and announces
tools/list_changed when the box answers. The test uses its own cache directory. Non-zero exit
status on any failure.
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  {detail}"))
    if not ok:
        fails += 1


CACHE = tempfile.mkdtemp(prefix="buggr_mcp_cache_")


class Client:
    def __init__(self, cache=CACHE):
        env = dict(os.environ, BUGGR_CACHE=cache)
        cmd = os.environ.get("BUGGR_MCP_CMD")
        argv = shlex.split(cmd, posix=os.name != "nt") if cmd else [sys.executable, os.path.join(HERE, "buggr_mcp.py")]
        self.p = subprocess.Popen(argv, env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr)
        self.n = 0
        self.notifications = []

    def send(self, obj):
        self.p.stdin.write((json.dumps(obj) + "\n").encode())
        self.p.stdin.flush()

    def request(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError(f"server closed stdout during {method}")
            reply = json.loads(line)
            if reply.get("id") == self.n:
                return reply
            self.notifications.append(reply)

    def tool(self, name, args=None):
        r = self.request("tools/call", {"name": name, "arguments": args or {}})
        res = r.get("result") or {}
        text = (res.get("content") or [{}])[0].get("text", "null")
        return res.get("isError"), json.loads(text)


def main():
    c = Client()
    r = c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "test_mcp", "version": "0"}})
    res = r.get("result", {})
    check("initialize: version echoed, server buggrbox, instructions present",
          res.get("protocolVersion") == "2025-06-18" and res.get("serverInfo", {}).get("name") == "buggrbox"
          and "device_info" in res.get("instructions", ""), json.dumps(r))
    c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    tools = c.request("tools/list").get("result", {}).get("tools", [])
    names = {t["name"] for t in tools}
    check("tools/list generated from the manifest",
          {"adapter_status", "device_info", "device_status", "power_set", "power_enable", "sim_set", "events_read", "usb_wait",
           "debug_probe", "debug_connect", "debug_read", "debug_flash"} <= names,
          str(sorted(names)))
    check("tool names have no dots and every inputSchema is an object",
          all("." not in t["name"] and t["inputSchema"].get("type") == "object" for t in tools))
    ps = next((t for t in tools if t["name"] == "power_set"), {})
    check("power_set description carries the lease note", "E_LEASE" in ps.get("description", ""), ps.get("description"))

    err, st = c.tool("adapter_status")
    check("adapter_status: connected, simulated safety", not err and st.get("connected") and st.get("safety_backend") == "simulated", json.dumps(st))

    err, e = c.tool("power_set", {"dut": 1, "volts": 4.3})
    check("device refusal -> tool error with code and hint", err is True and e.get("code") == "E_LIMIT" and "VIN range" in e.get("hint", ""), json.dumps(e))

    c.tool("sim_set", {"dut": 1, "load_ohm": None})
    err, rail = c.tool("power_set", {"dut": 1, "volts": 3.3, "limit_a": 0.5})
    check("power_set 3.3 V / 0.5 A", not err and rail.get("volts") == 3.3, json.dumps(rail))
    err, rail = c.tool("power_enable", {"dut": 1, "on": True})
    check("power_enable", not err and rail.get("enabled") is True, json.dumps(rail))
    err, ev0 = c.tool("events_read", {})
    err, sim = c.tool("sim_set", {"dut": 1, "load_ohm": 0})
    check("sim_set dead short trips rail 1", not err and sim["rails"][0]["tripped"] is True, json.dumps(sim))
    err, ev = c.tool("events_read", {"since_seq": ev0.get("next_seq", 0)})
    check("events_read shows the trip", not err and any(x["type"] == "trip" and x["dut"] == 1 for x in ev.get("events", [])), json.dumps(ev))

    c.tool("sim_set", {"dut": 1, "load_ohm": None})
    c.tool("sys_ack")
    c.tool("power_rearm", {"dut": 1})
    err, rail = c.tool("power_enable", {"dut": 1, "on": False})
    check("bench left quiet", not err and rail.get("enabled") is False and rail.get("tripped") is False, json.dumps(rail))

    why = ""
    try:
        import serial
        import serial.tools.list_ports as lp
        dev = next(p.device for p in lp.comports() if (p.vid, p.pid) == (0x303A, 0x83CC))
        with serial.Serial(dev, 115200, timeout=0.5):
            released = True
    except Exception as exc:                       # busy port, or the box went away
        released, why = False, str(exc)
    check("adapter releases the COM port between calls (a bench script can open it)", released, why)

    err, e = c.tool("no_such_tool")
    check("unknown tool -> E_UNSUPPORTED tool error", err is True and e.get("code") == "E_UNSUPPORTED", json.dumps(e))
    t0 = time.time()
    err, e = c.tool("usb_wait", {"dut": 1, "timeout_ms": 300})
    check("usb_wait with nothing new -> E_NOT_PRESENT after its timeout",
          err is True and e.get("code") == "E_NOT_PRESENT" and time.time() - t0 >= 0.3, json.dumps(e))

    err, e = c.tool("debug_flash", {"file": "no_such_image.elf", "chip": "STM32F411CEUx"})
    check("debug_flash with a missing file (or no probe-rs) is refused before touching the box",
          err is True and e.get("code") in ("E_INVALID_PARAMS", "E_UNSUPPORTED"), json.dumps(e))

    c.p.stdin.close()
    try:
        rc = c.p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        c.p.kill()
        rc = None
    check("closing stdin ends the server", rc == 0, f"rc={rc}")
    test_cache(len(tools))

    print(f"\n{fails} failure(s)")
    return 1 if fails else 0


def start(cache):
    c = Client(cache)
    c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "test_mcp", "version": "0"}})
    c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return c


def stop(c):
    c.p.stdin.close()
    try:
        c.p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        c.p.kill()


def test_cache(n_live):
    import serial
    import serial.tools.list_ports as lp
    dev = next(p.device for p in lp.comports() if (p.vid, p.pid) == (0x303A, 0x83CC))
    files = [f for f in os.listdir(CACHE) if f.startswith("manifest-")]
    check("the live manifest was cached, one file for this box", len(files) == 1, str(files))

    hold = serial.Serial(dev, 115200)                # the box is "not answering" for a fresh adapter
    c = start(CACHE)
    tools = c.request("tools/list").get("result", {}).get("tools", [])
    check("box unreachable: tools/list comes from the cache, same tools as live", len(tools) == n_live, f"{len(tools)} vs {n_live}")
    err, e = c.tool("device_info")
    check("a call before the box answers -> E_TRANSPORT, not a stale result", err is True and e.get("code") == "E_TRANSPORT", json.dumps(e))
    err, st = c.tool("adapter_status")
    check("adapter_status reports the cached manifest", st.get("connected") is False and st.get("manifest") == "cached", json.dumps(st))
    hold.close()
    err, info = c.tool("device_info")
    check("port free: the same session goes live", not err and "methods" in info, json.dumps(info)[:200])
    check("no tools/list_changed when the live set matches the cache",
          not any(m.get("method") == "notifications/tools/list_changed" for m in c.notifications), json.dumps(c.notifications))
    stop(c)

    empty = tempfile.mkdtemp(prefix="buggr_mcp_empty_")
    hold = serial.Serial(dev, 115200)
    c = start(empty)
    tools = c.request("tools/list").get("result", {}).get("tools", [])
    check("no cache and no box: adapter_status alone", [t["name"] for t in tools] == ["adapter_status"], str([t["name"] for t in tools]))
    hold.close()
    err, st = c.tool("adapter_status")
    check("the box answers: tools/list_changed announced",
          not err and st.get("manifest") == "live"
          and any(m.get("method") == "notifications/tools/list_changed" for m in c.notifications), json.dumps(st)[:200])
    stop(c)


if __name__ == "__main__":
    sys.exit(main())
