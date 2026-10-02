---
name: buggrbox-setup
description: First-run setup, verification and troubleshooting for a BuggrBox bench instrument (a USB box that gives Claude power, measurement, GPIO, UART, I2C and SWD access to embedded prototypes through the buggrbox MCP server). Use it whenever the user says "set up my BuggrBox", has just installed the buggrbox plugin or plugged the box in, asks whether the box is working, only sees an adapter_status tool, gets E_TRANSPORT or "BuggrBox not found", hits a serial-port permission error on Linux, wants to put the box on WiFi, or asks what the box can do before powering a DUT, even if they do not say "setup".
---

# BuggrBox setup

The box ships with its firmware installed. Setup never flashes it: the job is to get the `buggrbox` MCP
server (the adapter, `${CLAUDE_PLUGIN_ROOT}/host/buggr_mcp.py`, which the plugin runs with `uv run`) talking to the box, confirm what
the box reports about itself, and leave the user knowing what is safe to do next.

Work through the steps in order and stop at the first one that fails: each later step depends on the
earlier ones, and the fix is almost always at the step that failed.

## Facts the steps rely on

- The box enumerates as **USB VID 303A, PID 83CC**, product "BuggrBox", with its MAC address as the USB
  serial number (e.g. `AA:BB:CC:11:22:33`). It appears as a USB CDC serial port plus a CMSIS-DAP debug
  interface.
- **303A:4001 is not the box.** It is ESP-IDF's default ID, used by many ESP32-S3 boards the user may be
  testing. 303A:1001 is any ESP32-S3 in its ROM bootloader. Never treat either as the BuggrBox.
- The adapter opens the serial port for each call and releases it. Only one program can hold a serial
  port at a time, so a terminal, esptool or another Claude session using the box will make calls fail.
- The adapter caches each box's manifest. A session that started before the box answered still lists
  the tools, but every call returns `E_TRANSPORT` until the box itself answers. That is deliberate:
  nothing runs against a manifest the box has not just confirmed.
- `device_info` is the source of truth for ranges, limits and what is implemented. Read numbers from it;
  do not quote them from memory or from this skill.

## Step 1: the adapter can run

Call `adapter_status`. If the tool does not exist at all, the plugin's MCP server did not start. The
usual cause is that `uv` is missing: check with `uv --version`.

If `uv` is missing, ask before installing it (it changes the user's system), then use Astral's
official installer:

- Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`

The MCP server starts with the session, so after installing `uv` the user has to restart Claude Code
(or start a new session). Say so plainly and stop here; nothing else can be checked until then.

## Step 2: the operating system sees the box

If `adapter_status` says the box was not found, look for 303A:83CC on USB before blaming software:

- Windows: `Get-PnpDevice -PresentOnly | Where-Object InstanceId -match 'VID_303A&PID_83CC'`
- Linux: `lsusb -d 303a:83cc`
- macOS: `system_profiler SPUSBDataType | grep -i -A6 buggrbox`

Nothing listed means a physical problem: a charge-only cable (the most common one), a loose connector,
an unpowered hub. Ask the user to try another cable and port. If something shows as 303A:4001 or
303A:1001 instead, that is a different ESP32 board, not the box.

## Step 3: the user may open it (Linux only)

Windows 10 and 11 need no driver for either interface. macOS needs none either (not yet verified on
real hardware: say so if the box misbehaves there). On Linux a normal user cannot open the serial port
or the debug interface without a udev rule, and ModemManager may grab the port and send it modem
commands. The fix needs root, so show the user the commands and let them run them:

```bash
sudo tee /etc/udev/rules.d/60-buggrbox.rules >/dev/null <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="303a", ATTR{idProduct}=="83cc", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="83cc", MODE="0660", TAG+="uaccess", ENV{ID_MM_DEVICE_IGNORE}="1"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then replug the box. `uaccess` covers a desktop login; on a headless machine or over SSH also add the
user to `dialout` (`sudo usermod -aG dialout $USER`) and have them log out and back in.

## Step 4: the box answers

Call `adapter_status` again. Healthy looks like `connected: true`, a `firmware` block, `manifest: live`
and a `methods` count.

- `connected: false` with `manifest: cached`: the box was seen before but is not answering now. Most
  often another program holds the port; ask the user to close serial monitors, esptool or other Claude
  sessions using the box, then retry.
- "several BuggrBoxes on USB": the user has more than one. They pick one by setting the plugin's `serial`
  option to that box's MAC address, then restarting the session.
- Only `adapter_status` is listed as a tool, yet it now reports `connected: true`: this session started
  before the box ever answered and the client has not refreshed its tool list. The manifest is now
  cached, so a new session will have every tool. Tell the user to start one.

## Step 5: explain what the box reports

Call `device_info` and give the user a short summary, not the raw JSON:

- firmware version and hardware revision, and how many DUT channels are populated;
- the safety backend (`safety.backend`) and what it means (below);
- what is implemented (`implemented: true/false` sections) and what reads `null` (not calibrated yet);
- which limits are hardware bounds and which are policy, as `device_info` states them.

**If the safety backend is `simulated`**, the power tools run the real driver logic against a software
model and power nothing; `sim_set` injects faults (shorts, undervoltage) to exercise trips. Nothing can
be damaged, which makes it the right place to learn the tools.

**If it is real hardware**, slow down before the first `power_enable`. Confirm with the user what the
DUT is and its IO voltage, check that the front-panel VCCIO class switch matches it, start with the
lowest current limit that makes sense, and take the bench lease (`lease_acquire`) so another client
cannot change the rail underneath you. The hardware bounds hold regardless of what any tool asks; the
point of these checks is to not need them.

## Optional: WiFi

The box works fully over USB. WiFi adds the LAN serial bridge (RFC 2217, for esptool and terminals over
the network) and a web page. The WiFi password must never be typed into the chat, where it would stay
in the transcript. Give the user this command, with the path and their network name filled in, to run
in their **own terminal**; it asks for the password there and sends it over USB to the box only:

```
uv run --script "${CLAUDE_PLUGIN_ROOT}/host/buggr_mcp.py" wifi join <SSID>
```

The network name is case-sensitive. If it reports `last_reason=201`, the SSID was not found (check the
exact spelling and case); 15, 202 or 204 usually mean a wrong password. The same command with `wifi status`
shows the result and the box's IP. Do not run the join yourself: it refuses to run without a terminal, precisely so the
password cannot pass through you.

## Finishing

Tell the user in a few lines: the box is connected (port, firmware version), whether safety is simulated
or real, and one concrete first thing to try, for example "power DUT 1 at 3.3 V with a 300 mA limit,
then use sim_set to short it and watch the trip in events_read" on a simulated box.

Firmware updates are not part of setup; the box ships with working firmware and this skill never flashes
it.
