# buggrbox-test

Test copy of the BuggrBox Claude Code plugin, for trying the setup on new computers (Windows, macOS,
Linux). It contains the plugin and nothing else; a BuggrBox must be plugged in to do anything useful.

In Claude Code:

```
/plugin marketplace add BuggrBox/buggrbox-test
```
```
/plugin install buggrbox@buggrbox
```

Then start a new session with the box plugged in and type `set up my BuggrBox`. The full steps,
including setup by hand, are in [plugin/QUICKSTART.md](plugin/QUICKSTART.md).

To check the connector on its own (box on USB), from a checkout of this repository:

```
uv run --script plugin/host/test_mcp.py
```
