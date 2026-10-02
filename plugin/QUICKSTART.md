# BuggrBox quickstart

BuggrBox connects your computer to the prototype on your bench so Claude can power it, measure it,
talk to it over UART, I2C and SWD, and recover it when it hangs. The box arrives with its firmware
installed, so there is nothing to flash, and Claude does most of the setup for you.

## What you need

- the BuggrBox and a USB cable that carries data (some charging cables do not)
- a computer running Windows 10 or 11, macOS, or Linux
- Claude Code, either the desktop app or the terminal version

## Setup

1. Install the BuggrBox plugin in Claude Code:

   ```
   /plugin marketplace add BuggrBox/buggrbox-test
   ```
   ```
   /plugin install buggrbox@buggrbox
   ```

2. Plug the box into your computer.

3. Start a new Claude Code session and type:

   ```
   set up my BuggrBox
   ```

Claude checks the connection and asks before it changes anything on your computer. At the end it tells
you which firmware the box runs, whether its power stage is real or simulated, and something to try
first.

## If you'd rather do it by hand

1. Install `uv`, which runs the BuggrBox connector without any other Python setup: see
   https://docs.astral.sh/uv/getting-started/installation/.
2. Register the connector with Claude Code:

   ```
   claude mcp add buggrbox -- uvx --from git+https://github.com/BuggrBox/buggrbox-test#subdirectory=plugin/host buggrbox-mcp
   ```

3. On Linux only, let your user account open the box. Run these, then unplug the box and plug it back in:

   ```bash
   sudo tee /etc/udev/rules.d/60-buggrbox.rules >/dev/null <<'EOF'
   SUBSYSTEM=="usb", ATTR{idVendor}=="303a", ATTR{idProduct}=="83cc", MODE="0660", TAG+="uaccess"
   SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="83cc", MODE="0660", TAG+="uaccess", ENV{ID_MM_DEVICE_IGNORE}="1"
   EOF
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ```

4. Start a new session and ask Claude to call `device_info`.

## Putting the box on WiFi (optional)

Everything works over USB. WiFi adds a network serial port (esptool and terminal programs can use it
from anywhere on your network) and a status page in your browser. Ask Claude to put the box on WiFi and
it gives you a command to run in your own terminal, not in the chat, so your WiFi password stays out of
the conversation. Without the plugin, the command is:

```
uvx --from git+https://github.com/BuggrBox/buggrbox-test#subdirectory=plugin/host buggrbox-mcp wifi join <your network name>
```

Network names are case-sensitive.

## When something is off

| What you see | What to do |
|---|---|
| Claude only has an `adapter_status` tool | Start a new session with the box plugged in. |
| "BuggrBox not found" | Try another USB cable and port. Charge-only cables are the usual cause. |
| The box is found but calls fail | Close any serial monitor, esptool, or other Claude session using the box. |
| Permission denied on Linux | Do step 3 of "by hand", then replug the box. |

## Before you power a real prototype

Ask Claude to explain what `device_info` reports. Set the VCCIO switch on the front panel to match your
prototype's IO voltage, and start with a low current limit. The box's hardware limits hold whatever the
software asks for; a careful first power-up means you never have to find out how.
