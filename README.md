# kentik-device-renamer

Rename devices in the Kentik portal using the Device API (`v202504beta2`, REST).
It's a single Python file with no runtime dependencies (Python 3.9+). The project is managed
with [uv](https://docs.astral.sh/uv/).

For how it works and why, see the [design document](docs/DESIGN.md).

## Supported devices

The tool supports devices with **NMS**, **Flow**, or both enabled, using the
**"Agent-based SNMP for Full Monitoring"** collection method. It hasn't been tested with other
device types or collection methods. Try a dry run with `--dump` first and check the planned
request before applying.

## Setup

```sh
uv sync                 # creates .venv and installs the tool (+ pytest for development)
cp .env.example .env
# then edit .env:
#   KENTIK_EMAIL=you@example.com
#   KENTIK_API_TOKEN=...             # Kentik portal: Settings > Authentication
#   KENTIK_REGION=EU                 # US (default) or EU
```

The tool looks for `.env` in the current directory first, then next to the script. Use
`--env-file PATH` to point it at another file. Variables already set in your shell override
the file, and `--region` overrides `KENTIK_REGION`. Use `--base-url` for anything other than
the US or EU cluster.

`.env` holds your API token. Keep it out of version control; the included `.gitignore` does that.

## Usage

```sh
# Rename one device (shows the plan only; nothing changes yet)
uv run kentik-rename rename --name edge_router_01 --to nyc_edge_01
uv run kentik-rename rename --id 12345 --to nyc_edge_01 --apply

# Bulk: export all devices, fill in the new_name column, then apply
uv run kentik-rename list -o devices.csv
uv run kentik-rename rename --csv devices.csv            # check the plan
uv run kentik-rename rename --csv devices.csv --apply
```

### Saving snapshots (`--dump`)

Add `--dump` to keep a JSON record of each device. It goes in `dumps/<YYYYmmdd-HHMMSS>/`, or in a
folder you name with `--dump DIR`:

```
dumps/20261118-153000/
  142266_hzn-vmx-cr1/
    before.json     # device config before the rename (GET)
    request.json    # body sent to UpdateDevice (PUT)
    response.json   # what UpdateDevice returned
    after.json      # device config after the rename (GET)
    status.json     # renamed | failed | skipped | not_run | dry_run, plus a message
```

Every device in the CSV gets its own folder, including ones that weren't renamed:
- **skipped:** the device already has the new name (e.g. from an earlier run). Only `before.json`
  is saved.
- **failed:** whatever was collected before the error is kept, usually `before.json` and
  `request.json`.
- **not_run:** the run stopped at an earlier failure. Only `status.json` is written, because the
  device wasn't touched. Use `--keep-going` to attempt every row.

On a dry run (no `--apply`), only `before.json`, `request.json` and `status.json` are written, so
you can see exactly what would be sent. Compare a device's before and after with
`diff before.json after.json`.

The snapshots may contain SNMP/BGP credentials, so the folders are created readable by your user only.
Don't commit or share them.

Rows with an empty `new_name` column are skipped. You can match devices by `id` or by `old_name`.

Kentik's name rule: 4–60 characters; letters, digits, underscores, hyphens and dots. (The proto
documents only letters, digits and underscores, but Kentik accepts hyphens and dots too.)
Before changing anything, the tool also rejects:
- a device it can't find
- a new name that another device already uses
- the same new name given to two devices

Because renames run one at a time, a swap (A→B and B→A in one go) is rejected. Rename one of them to a temporary name first.

## How a rename works

`UpdateDevice` **replaces** the device's configuration with whatever the request contains. So for each device the tool:

1. Reads the full device (`GET /device/v202504beta2/device/{id}`).
2. Rebuilds the update payload from it, changing only `device_name`. It converts field formats where the read and update shapes differ, for example `plan.id` becomes `plan_id` and the sample rate goes from text to a number.
3. Sends the update (`PUT`).
4. Reads the device again and compares the settings it kept against the originals.

If anything other than the name changed, it reports `FAIL ... OTHER FIELDS CHANGED` and stops, unless you pass `--keep-going`.

The SNMP community, BGP password and SNMPv3 settings are never sent back. The API documents them as removed from requests, and what a read returns for them may be masked. Labels are managed through a separate endpoint and aren't affected.

## First run against a real account

Try `--apply` on **one** non-critical device first. Then check in the portal that its SNMP, BGP and flow settings are unchanged. The tests use a stand-in for the API, so how Kentik handles the secret fields that aren't sent back hasn't been checked against a real account.

## Tests

```sh
uv run pytest
```

The tests also run without uv: `python3 -m unittest discover -s tests`.

The tests run against a local stand-in for the API that copies its replace-the-whole-configuration behaviour. They show that a rename keeps the device's settings, and that the post-rename check catches a dropped field.

## License

Apache License 2.0. See [LICENSE](LICENSE).
