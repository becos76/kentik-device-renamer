# Kentik Device Renamer: design

kentik-device-renamer 0.1.0 · design as of 18 November 2026

A command-line tool that renames devices in the Kentik portal, one at a time or in bulk from a CSV, without disturbing any other part of their configuration.

- Device API `v202504beta2` (beta), REST with `X-CH-Auth-*` headers
- Python ≥ 3.9, standard library only
- 24 tests passing
- Apache-2.0

## Contents

1. [Overview](#overview)
2. [Scope](#scope)
3. [How a rename works](#how-a-rename-works)
4. [Design decisions](#design-decisions)
5. [Field mapping](#field-mapping)
6. [Safety controls](#safety-controls)
7. [Snapshots (`--dump`)](#snapshots---dump)
8. [Configuration](#configuration)
9. [Command reference](#command-reference)
10. [Testing](#testing)
11. [Risks and open questions](#risks-and-open-questions)
12. [Possible next steps](#possible-next-steps)

## Overview

> **Note:** this is an unofficial tool, not affiliated with, endorsed by, or supported by Kentik. Use it at your own risk; see the notice in the [README](../README.md).

Renaming a device sounds like a one-field change, but the Device API has no rename call. The only way to change `device_name` is `UpdateDevice`, and its documentation says it *replaces* the device's configuration with the attributes in the request. A request that carries only the new name could clear sending IPs, the sample rate, BGP settings and NMS settings.

The tool treats every rename as a full read-modify-write. It reads the device, rebuilds the update from the current configuration with only the name changed, sends it, then reads the device again and compares. Anything other than the name that changed is reported as a failure and the run stops.

It is a single file, `kentik_rename.py`, with no runtime dependencies. The schema it follows is `proto/kentik/device/v202504beta2/device.proto` in `kentik/api-schema-public`.

## Scope

The tool supports devices with **NMS**, **Flow**, or both enabled, using the **Agent-based SNMP for Full Monitoring** collection method. Other device types and collection methods haven't been tested.

**In scope**

- Rename one device by ID or current name
- Bulk rename from a CSV, matched by ID or current name
- Export all devices as a CSV template
- Dry run, validation and per-device snapshots
- US and EU regions, or any base URL

**Out of scope**

- Changing any field other than `device_name`
- Labels (separate `UpdateDeviceLabels` endpoint, untouched)
- Creating or deleting devices
- Automatic rollback
- The legacy v5 REST API

## How a rename works

Steps 1–2 run once. Steps 3–7 run for each device, in order. A dry run stops after step 2, or after step 4 when `--dump` is given, so it never writes. Step 5 is the only write.

1. **List devices.** One call with the basic view returns every device's ID and name, used to resolve CSV rows and check for name collisions.
   `GET /device/v202504beta2/device?view=DEVICE_VIEW_BASIC`
2. **Plan and validate.** Each row is resolved to a device and checked: it exists, the new name is valid, no other device holds that name, and no two rows target the same name. Rows that already have their new name are marked as skipped. Any error blocks the run unless `--ignore-errors` is passed.
3. **Read the current configuration.** If the device's name changed since step 1, it is skipped rather than overwritten.
   `GET /device/v202504beta2/device/{id}`
4. **Build the update.** The full `DeviceDetailed` response is mapped onto a `DeviceConcise` request. Only the name changes. Empty values and secrets are left out.
5. **Send the update** *(write)*. The request body is `{"device": {…}}`.
   `PUT /device/v202504beta2/device/{id}`
6. **Re-read and compare.** The device is read again. The name must match, and every other field the tool checks must equal its value from step 3. Skipped with `--no-verify`.
   `GET /device/v202504beta2/device/{id}`
7. **Record the result.** Print `OK` or `FAIL`, and with `--dump` write that device's snapshot folder. A failure stops the run unless `--keep-going` is passed. Any devices left are reported as `NOT RUN`.

## Design decisions

### Resend the full configuration on every rename

*Correctness.* Because `UpdateDevice` replaces the configuration, a partial request risks clearing fields. Rebuilding from a fresh read costs two extra GETs per device and removes that risk.

### Leave empty values out of the request

*Correctness, found in testing.* The API returns every field, including empty ones, but rejects some empty fields that don't apply to the device:

```
HTTP 400: At path: request.device.device_bgp_credential_name -- Expected a value of type `never`, but received: `""`
```

The tool now drops empty strings, lists and objects (recursively, including inside `nms`) and treats `monitoring_template_id: 0` and ID 0 as unset. True/false values are always sent.

### Never send credentials back

*Security.* `device_snmp_community`, `device_bgp_password` and `device_snmp_v3_conf` are documented as removed from requests, and a read may return them masked. Echoing a masked value back could overwrite the real secret, so they are always left out.

### Verify after every write, stop at the first failure

*Safety.* A rename that succeeds but changes something else is treated as a failure. Stopping early limits any damage to one device. `--keep-going` is available once a configuration has been proven safe.

### One device at a time, no name swaps

*Safety.* Sequential calls keep errors attributable to one device and make verification simple. As a result, a name still held by another device is rejected even if that device is renamed later in the same run. A swap needs a temporary name first.

### REST with the standard library instead of generated stubs

*Packaging.* The repository's `gen/python` stubs were removed in commit `138198b`. The REST endpoints that gRPC-Gateway exposes use the same schema and need no protobuf, gRPC or HTTP library. Responses are read in either snake_case or lowerCamelCase.

### Accept hyphens and dots in names

*Validation.* The proto documents letters, digits and underscores, 4–60 characters. Kentik accepts hyphens and dots in practice, and existing device names use them, so the tool allows `[A-Za-z0-9_.-]{4,60}`.

## Field mapping

Reads return `DeviceDetailed`. Updates take `DeviceConcise`. Most fields carry over unchanged. These are the ones that need converting or are deliberately dropped.

| Read (DeviceDetailed) | | Write (DeviceConcise) | Handling |
|---|:-:|---|---|
| `device_name` | → | `device_name` | Replaced with the new name |
| `device_sample_rate` (string) | → | `device_sample_rate` (double) | Converted to a number |
| `plan.id` (string) | → | `plan_id` (uint32) | Converted. Omitted when 0 or empty |
| `site.id` (string) | → | `site_id` (uint32) | Converted. Omitted when 0 or empty |
| `monitoring_template_id` | → | `monitoring_template_id` | Omitted when 0 |
| `nms` (object) | → | `nms` | Copied with empty sub-fields removed |
| `device_snmp_community` | ✕ | n/a | Never sent (secret) |
| `device_bgp_password` | ✕ | n/a | Never sent (secret) |
| `device_snmp_v3_conf` | ✕ | n/a | Never sent (secret) |
| `labels` | ✕ | n/a | Not part of DeviceConcise. Checked after the write |

Copied unchanged when non-empty: `device_subtype`, `cdn_attr`, `device_description`, `sending_ips`, `minimize_snmp`, `device_snmp_ip`, `device_bgp_type`, `device_bgp_neighbor_ip`, `device_bgp_neighbor_ip6`, `device_bgp_neighbor_asn`, `use_bgp_device_id`, `device_bgp_flowspec`, `device_bgp_credential_name`, `flow_snmp_credential_name`, `device_alert`. The post-write check compares all of these plus sample rate, site, plan and labels.

## Safety controls

| Control | What it prevents | Override |
|---|---|---|
| Dry run by default | Any change without an explicit decision | `--apply` |
| Planning checks | Unknown devices, invalid names, name collisions, duplicate targets | `--ignore-errors` applies the valid rows |
| Stale-name check | Overwriting a device renamed by someone else mid-run | None |
| Post-write comparison | A rename that silently changes other settings | `--no-verify` |
| Stop at first failure | One bad mapping repeated across a whole batch | `--keep-going` |
| Retries with backoff | Brief outages and rate limits (429, 502, 503, 504, network errors). 3 retries, 1 s / 2 s / 4 s, 30 s timeout | None |
| Snapshots | Losing the before-state needed to diagnose or undo by hand | Off unless `--dump` |

## Snapshots (`--dump`)

With `--dump`, every row of the CSV gets its own folder, including rows that were skipped or never ran. The default location is `dumps/<YYYYmmdd-HHMMSS>/`. Folders are created `0700` and files `0600`, because the configurations may contain credentials.

```
dumps/20261118-153000/
  101_edge_router_01/
    before.json     # GET before the rename
    request.json    # body sent to UpdateDevice
    response.json   # what UpdateDevice returned
    after.json      # GET after the rename
    status.json     # {"status": "...", "message": "..."}
```

| status | Meaning | Files written |
|---|---|---|
| `renamed` | Renamed and verified | before, request, response, after |
| `failed` | Rejected by the API, or the comparison found other changes | Whatever was collected before the error |
| `skipped` | The device already had the new name | before |
| `not_run` | The run stopped at an earlier failure. Device untouched | status only |
| `dry_run` | No `--apply` given | before, request |

## Configuration

Credentials and region come from `KENTIK_EMAIL`, `KENTIK_API_TOKEN` and `KENTIK_REGION` (`US` or `EU`, default US). They are sent as the `X-CH-Auth-Email` and `X-CH-Auth-API-Token` headers. When a setting is defined in more than one place, the first source in this order wins:

**command-line flags > shell environment > `.env` file > defaults**

The `.env` file is looked for in the current directory, then next to the script, or wherever `--env-file` points. The parser is built in and handles comments, `export` prefixes and quoted values. A value still set to a `<placeholder>` from `.env.example` is rejected with a message naming the setting.

Regions map to `https://grpc.api.kentik.com` (US, the API's declared default host) and `https://grpc.api.kentik.eu` (EU, confirmed by a live run). `--base-url` overrides both.

## Command reference

```sh
# setup
uv sync
cp .env.example .env

# export a CSV template: id, old_name, new_name
uv run kentik-rename list -o devices.csv

# preview, then apply
uv run kentik-rename rename --csv devices.csv --dump
uv run kentik-rename rename --csv devices.csv --apply --dump

# single device
uv run kentik-rename rename --name edge_router_01 --to edge-nyc-01 --apply
```

| Option | Applies to | Effect |
|---|---|---|
| `--region us\|eu` | all | Choose the Kentik cluster. Overrides `KENTIK_REGION` |
| `--env-file PATH` | all | Read settings from a specific file |
| `--base-url URL` | all | Use another API host |
| `-o, --output FILE` | list | Write the CSV to a file instead of the terminal |
| `--csv FILE` | rename | Rows with `id` or `old_name`, and `new_name`. Blank `new_name` rows are skipped |
| `--id` / `--name`, `--to` | rename | Rename a single device |
| `--apply` | rename | Make the changes |
| `--dump [DIR]` | rename | Write per-device snapshots |
| `--keep-going` | rename | Continue after a failed device |
| `--ignore-errors` | rename | Apply valid rows when some rows fail planning |
| `--no-verify` | rename | Skip the re-read and comparison |

Exit codes: 0 success, 1 planning error or a failed device, 2 an API error outside a device rename (for example while listing devices).

## Testing

The suite runs against a local HTTP server that imitates the Device API, including its replace-the-whole-configuration behaviour. A field the tool forgets to send is cleared on the fake server, so the post-write comparison has something real to catch.

| Area | Tests | Covers |
|---|--:|---|
| Payload building | 4 | Read-to-write mapping, secrets left out, empty fields dropped, snake_case responses, name rule |
| Planning | 3 | Match by ID and name, collisions, unknown devices, invalid names, duplicate targets, no-op rows |
| End to end | 10 | Dry run, apply and preserve config, detecting a dropped field, CSV round trip, invalid rows blocking, missing credentials, snapshot layout and permissions, a folder for every row |
| Configuration | 7 | `.env` parsing, precedence, region default and override, placeholders, bad region, missing file |

Run with `uv run pytest`, or `python3 -m unittest discover -s tests` without uv. The tests never read the developer's real `.env`.

## Risks and open questions

| Level | Risk | Detail |
|---|---|---|
| **High** | Secret fields on production devices | Renames have been run against test devices, not devices carrying real traffic. Whether leaving out the SNMP community, BGP password and SNMPv3 settings keeps them intact on the server is unconfirmed, and the post-write comparison can't check fields that reads mask. Do the first production rename on one device, then check SNMP polling and the BGP session. |
| Medium | Beta API version | `v202504beta2` keeps backward compatibility within the version and is supported for 6 months after a successor ships. A new version may move or rename fields, and the mapping would need updating. |
| Medium | Other device types and collection methods | Only NMS and Flow devices using Agent-based SNMP for Full Monitoring have been exercised. Other types may hit more "type never" validation errors on fields that are set but not applicable to them. |
| Low | No automatic rollback | Undoing a batch means a second CSV with the names reversed. Snapshots hold the previous names and configurations, but nothing replays them automatically. |
| Low | Snapshots hold sensitive data | Dump folders are owner-only and git-ignored, but they may contain credential values. Delete them once a run has been checked. |

## Possible next steps

None of these are committed. They follow from the risks above.

- **Rollback command** that reads a dump folder and restores each device's previous name.
- **Batch updates** via `UpdateDevices` (up to 100 per call) for large accounts, once single-device renames are proven on production.
- **CI** that runs the tests on each push after the repository is published.
- **Release tagging** so others can install a fixed version with `uv tool install`.
