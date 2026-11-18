#!/usr/bin/env python3
"""Rename devices in the Kentik portal via the Device API (v202504beta2, REST).

Schema: kentik/api-schema-public proto/kentik/device/v202504beta2/device.proto

UpdateDevice *replaces* a device's configuration with the request body, so a
rename must resend the current configuration with only device_name changed.
This tool reads the device, rebuilds the DeviceConcise payload from it,
changes the name, PUTs it, then re-reads the device and checks that nothing
except the name changed.

Settings: KENTIK_EMAIL, KENTIK_API_TOKEN, KENTIK_REGION (US|EU), from the environment
or a .env file (copy .env.example to .env).
Stdlib only; Python 3.8+.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_VERSION = "v202504beta2"
REGIONS = {
    "us": "https://grpc.api.kentik.com",
    "eu": "https://grpc.api.kentik.eu",
}

# device_name: the proto documents "alphanumeric and underscores, 4-60 chars", but
# Kentik also accepts hyphens and dots in practice.
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{4,60}$")

# DeviceConcise fields that are copied 1:1 (same name, same type) from DeviceDetailed.
PASSTHROUGH_FIELDS = [
    "device_subtype",
    "cdn_attr",
    "device_description",
    "sending_ips",
    "minimize_snmp",
    "device_snmp_ip",
    "device_bgp_type",
    "device_bgp_neighbor_ip",
    "device_bgp_neighbor_ip6",
    "device_bgp_neighbor_asn",
    "use_bgp_device_id",
    "device_bgp_flowspec",
    "nms",
    "device_bgp_credential_name",
    "flow_snmp_credential_name",
    "monitoring_template_id",
    "device_alert",
]

# Secrets: the API documents these as removed from requests, and what GET
# returns for them may be masked. They are never sent back.
SECRET_FIELDS = ["device_snmp_community", "device_bgp_password", "device_snmp_v3_conf"]

# Fields compared before/after a rename to detect collateral changes.
VERIFY_FIELDS = PASSTHROUGH_FIELDS + ["device_sample_rate", "site", "plan", "labels"]


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def field(obj: dict, name: str, default=None):
    """Read a proto field from gRPC-Gateway JSON, which may use snake_case or lowerCamelCase."""
    if not isinstance(obj, dict):
        return default
    if name in obj:
        return obj[name]
    return obj.get(_camel(name), default)


def has_field(obj: dict, name: str) -> bool:
    return name in obj or _camel(name) in obj


class Client:
    def __init__(self, base_url: str, email: str, token: str, timeout: float = 30.0, retries: int = 3):
        self.base = base_url.rstrip("/") + f"/device/{API_VERSION}"
        self.headers = {
            "X-CH-Auth-Email": email,
            "X-CH-Auth-API-Token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.timeout = timeout
        self.retries = retries

    def _request(self, method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                try:
                    msg = json.loads(raw).get("message", raw)
                except ValueError:
                    msg = raw
                if e.code in (429, 502, 503, 504) and attempt < self.retries:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(e.code, msg) from None
            except urllib.error.URLError as e:
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(0, str(e.reason)) from None
        raise AssertionError("unreachable")

    def list_devices(self, basic: bool = True) -> list[dict]:
        query = {"view": "DEVICE_VIEW_BASIC"} if basic else None
        return field(self._request("GET", "/device", query=query), "devices", []) or []

    def get_device(self, device_id: str) -> dict:
        return field(self._request("GET", f"/device/{urllib.parse.quote(device_id)}"), "device", {})

    def update_device(self, concise: dict) -> dict:
        path = f"/device/{urllib.parse.quote(concise['id'])}"
        return field(self._request("PUT", path, body={"device": concise}), "device", {})


def _to_uint(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _prune(value):
    """Drop unset values (None, "", [], {}) recursively.

    GET returns every field, including empty ones, but the server rejects
    some empty fields that don't apply to the device. For example it says
    device_bgp_credential_name 'Expected a value of type `never`, but
    received: ""'. Unset fields must therefore be omitted, not sent empty.
    Booleans and numbers are kept as they are.
    """
    if isinstance(value, dict):
        out = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        out = [_prune(v) for v in value]
        return [v for v in out if v not in (None, "", [], {})]
    return value


# uint32 ids where 0 means "none"; sent only when set.
ZERO_MEANS_UNSET = {"monitoring_template_id"}


def build_update_payload(detailed: dict, new_name: str) -> dict:
    """Map a DeviceDetailed (GET) into a DeviceConcise (PUT) with a new device_name."""
    payload = {"id": str(field(detailed, "id")), "device_name": new_name}
    for name in PASSTHROUGH_FIELDS:
        if not has_field(detailed, name):
            continue
        value = _prune(field(detailed, name))
        if value in (None, "", [], {}) or (name in ZERO_MEANS_UNSET and value == 0):
            continue
        payload[name] = value

    # device_sample_rate: string in DeviceDetailed, double in DeviceConcise.
    rate = field(detailed, "device_sample_rate")
    if rate not in (None, ""):
        try:
            payload["device_sample_rate"] = float(rate)
        except ValueError:
            raise ValueError(f"unexpected device_sample_rate {rate!r}")

    # plan / site: nested objects in DeviceDetailed, uint32 ids in DeviceConcise.
    plan_id = _to_uint(field(field(detailed, "plan", {}), "id"))
    if plan_id:
        payload["plan_id"] = plan_id
    site_id = _to_uint(field(field(detailed, "site", {}), "id"))
    if site_id:
        payload["site_id"] = site_id
    return payload


def _norm(v):
    """Normalize values for comparison: treat missing / empty / zero as equal."""
    if v in (None, "", [], {}, 0, False, "0"):
        return None
    if isinstance(v, dict):
        out = {k: _norm(x) for k, x in v.items()}
        out = {_camel(k) if "_" in k else k: x for k, x in out.items() if x is not None}
        return out or None
    if isinstance(v, list):
        return sorted((json.dumps(_norm(x), sort_keys=True) for x in v)) or None
    return v


def diff_devices(before: dict, after: dict) -> list[str]:
    changes = []
    for name in VERIFY_FIELDS:
        b, a = _norm(field(before, name)), _norm(field(after, name))
        if name == "device_sample_rate":
            b = float(b) if b is not None else None
            a = float(a) if a is not None else None
        if b != a:
            changes.append(f"{name}: {json.dumps(b)} -> {json.dumps(a)}")
    return changes


def read_mapping(path: str) -> list[tuple[str, str]]:
    """CSV with header: one of (id | old_name), and new_name."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = {c.strip().lower() for c in (reader.fieldnames or [])}
        if "new_name" not in cols or not ({"id", "old_name"} & cols):
            raise SystemExit(f"{path}: header must contain new_name and either id or old_name")
        rows = []
        for i, row in enumerate(reader, start=2):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            key = row.get("id") or row.get("old_name")
            if not key and not row.get("new_name"):
                continue
            if not key or not row.get("new_name"):
                raise SystemExit(f"{path}:{i}: missing id/old_name or new_name")
            rows.append((("id:" if row.get("id") else "name:") + key, row["new_name"]))
        return rows


def plan_renames(devices: list[dict], mapping: list[tuple[str, str]]) -> tuple[list[dict], list[str], list[dict]]:
    """Returns (renames to do, errors, devices that already have their new name)."""
    by_id = {str(field(d, "id")): d for d in devices}
    by_name = {field(d, "device_name"): d for d in devices}
    existing_names = set(by_name)
    plan, errors, unchanged, targets = [], [], [], {}
    for key, new_name in mapping:
        kind, value = key.split(":", 1)
        dev = by_id.get(value) if kind == "id" else by_name.get(value)
        label = f"{kind} {value}"
        if dev is None:
            errors.append(f"{label}: device not found")
            continue
        dev_id, old_name = str(field(dev, "id")), field(dev, "device_name")
        if not NAME_RE.match(new_name):
            errors.append(f"{label}: invalid new name {new_name!r} (4-60 chars: letters, digits, '_', '-', '.')")
            continue
        if new_name == old_name:
            unchanged.append({"id": dev_id, "old_name": old_name, "new_name": new_name})
            continue
        if new_name in targets and targets[new_name] != dev_id:
            errors.append(f"{label}: new name {new_name!r} assigned to more than one device")
            continue
        # Renames run one at a time, so a name still held by another device (even one
        # that is renamed later in the same batch, e.g. a swap) would collide.
        if new_name in existing_names and by_name[new_name] is not dev:
            errors.append(f"{label}: name {new_name!r} already used by device {field(by_name[new_name], 'id')}")
            continue
        targets[new_name] = dev_id
        plan.append({"id": dev_id, "old_name": old_name, "new_name": new_name})
    return plan, errors, unchanged


class Dumper:
    """Writes per-device JSON snapshots under <root>/<id>_<old_name>/.

    Files: before.json (GET before), request.json (PUT body),
    response.json (PUT response), after.json (GET after), and status.json
    (renamed / failed / skipped / not_run / dry_run) for every device in the run.
    Snapshots may contain credentials, so the folder is owner-only (0700/0600).
    """

    def __init__(self, root: str | None):
        self.root = root
        if root:
            os.makedirs(root, mode=0o700, exist_ok=True)

    def write(self, item: dict, name: str, data) -> None:
        if not self.root:
            return
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{item['id']}_{item['old_name']}")
        folder = os.path.join(self.root, safe)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        path = os.path.join(folder, f"{name}.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")


def preview_one(client: Client, item: dict, dumper: Dumper) -> None:
    """Dry run with --dump: save the current config and the request that would be sent."""
    before = client.get_device(item["id"])
    dumper.write(item, "before", before)
    dumper.write(item, "request", {"device": build_update_payload(before, item["new_name"])})


def rename_one(client: Client, item: dict, verify: bool = True, dumper: Dumper | None = None) -> tuple[bool, str]:
    dumper = dumper or Dumper(None)
    before = client.get_device(item["id"])
    dumper.write(item, "before", before)
    if field(before, "device_name") != item["old_name"]:
        return False, f"name changed since listing (now {field(before, 'device_name')!r}); skipped"
    payload = build_update_payload(before, item["new_name"])
    dumper.write(item, "request", {"device": payload})
    dumper.write(item, "response", client.update_device(payload))
    if not verify:
        return True, "renamed"
    after = client.get_device(item["id"])
    dumper.write(item, "after", after)
    if field(after, "device_name") != item["new_name"]:
        return False, f"update returned OK but name is {field(after, 'device_name')!r}"
    changes = diff_devices(before, after)
    if changes:
        return False, "renamed, but OTHER FIELDS CHANGED:\n      " + "\n      ".join(changes)
    return True, "renamed and verified"


def load_env_file(path: str) -> dict:
    """Parse a .env file: KEY=VALUE lines, optional 'export ', quotes, # comments."""
    values = {}
    with open(path, encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise SystemExit(f"{path}:{lineno}: expected KEY=VALUE")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            values[key] = value
    return values


def default_env_file() -> str | None:
    """.env in the current directory, else next to this script."""
    for folder in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = os.path.join(folder, ".env")
        if os.path.isfile(path):
            return path
    return None


def resolve_settings(args) -> tuple[str, str, str]:
    """Return (base_url, email, token).

    Precedence: command-line flags > real environment variables > .env file > defaults.
    """
    env_path = args.env_file or default_env_file()
    if args.env_file and not os.path.isfile(args.env_file):
        raise SystemExit(f"--env-file {args.env_file}: file not found")
    file_values = load_env_file(env_path) if env_path else {}

    def get(name):
        value = os.environ.get(name) or file_values.get(name) or ""
        if value.startswith("<") and value.endswith(">"):
            raise SystemExit(f"{name} still has the placeholder {value!r}; set a real value in {env_path or 'the environment'}.")
        return value

    email, token = get("KENTIK_EMAIL"), get("KENTIK_API_TOKEN")
    if not email or not token:
        hint = f" (looked in {env_path})" if env_path else " (no .env found; copy .env.example to .env)"
        raise SystemExit(f"Set KENTIK_EMAIL and KENTIK_API_TOKEN in .env or the environment{hint}.")

    if args.base_url:
        return args.base_url, email, token
    region = (args.region or get("KENTIK_REGION") or "us").strip().lower()
    if region not in REGIONS:
        raise SystemExit(f"Unknown region {region!r}; use one of: {', '.join(r.upper() for r in REGIONS)}.")
    return REGIONS[region], email, token


def make_client(args) -> Client:
    base_url, email, token = resolve_settings(args)
    return Client(base_url, email, token)


def cmd_list(args) -> int:
    devices = make_client(args).list_devices(basic=True)
    out = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        w = csv.writer(out)
        w.writerow(["id", "old_name", "new_name"])
        for d in sorted(devices, key=lambda d: field(d, "device_name") or ""):
            w.writerow([field(d, "id"), field(d, "device_name"), ""])
    finally:
        if out is not sys.stdout:
            out.close()
    print(f"{len(devices)} devices", file=sys.stderr)
    return 0


def cmd_rename(args) -> int:
    if args.csv:
        mapping = read_mapping(args.csv)
    elif args.to and (args.id or args.name):
        mapping = [(("id:" + args.id) if args.id else ("name:" + args.name), args.to)]
    else:
        raise SystemExit("Use --csv FILE, or --id/--name together with --to.")

    client = make_client(args)
    plan, errors, unchanged = plan_renames(client.list_devices(basic=True), mapping)
    for e in errors:
        print(f"ERROR {e}", file=sys.stderr)
    dumper = Dumper(args.dump)

    if unchanged:
        print(f"{len(unchanged)} device(s) already have the new name, skipped:")
        for u in unchanged:
            print(f"  [{u['id']}] {u['old_name']}")
            if args.dump:
                dumper.write(u, "before", client.get_device(u["id"]))
                dumper.write(u, "status", {"status": "skipped", "message": "already has the new name"})
    if not plan:
        print("Nothing to rename.")
        if args.dump:
            print(f"Snapshots saved to {args.dump}/")
        return 1 if errors else 0

    print(f"{len(plan)} rename(s) planned:")
    for p in plan:
        print(f"  [{p['id']}] {p['old_name']} -> {p['new_name']}")
    if errors and not args.ignore_errors:
        print("Fix the errors above, or pass --ignore-errors to rename the valid rows.", file=sys.stderr)
        return 1
    if not args.apply:
        if args.dump:
            for p in plan:
                preview_one(client, p, dumper)
                dumper.write(p, "status", {"status": "dry_run", "message": "not applied"})
            print(f"\nSaved current configs and planned requests to {args.dump}/")
        print("\nDry run. Re-run with --apply to make these changes.")
        return 0

    failures = 0
    for i, p in enumerate(plan):
        try:
            ok, msg = rename_one(client, p, verify=not args.no_verify, dumper=dumper)
        except (ApiError, ValueError) as e:
            ok, msg = False, str(e)
        failures += not ok
        dumper.write(p, "status", {"status": "renamed" if ok else "failed", "message": msg})
        print(f"{'OK  ' if ok else 'FAIL'} [{p['id']}] {p['old_name']} -> {p['new_name']}: {msg}")
        if not ok and not args.keep_going:
            print("Stopping at first failure (use --keep-going to continue).", file=sys.stderr)
            for rest in plan[i + 1:]:
                print(f"NOT RUN [{rest['id']}] {rest['old_name']} -> {rest['new_name']}")
                dumper.write(rest, "status", {"status": "not_run", "message": "stopped after an earlier failure"})
            break
    if args.dump:
        print(f"Snapshots saved to {args.dump}/")
    return 1 if failures else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Rename Kentik devices via the Device API.")
    ap.add_argument("--region", type=str.lower, choices=REGIONS,
                    help="Kentik region; overrides KENTIK_REGION (default: us)")
    ap.add_argument("--env-file", metavar="PATH",
                    help="Read settings from this file (default: .env in the current dir, else next to the script)")
    ap.add_argument("--base-url", help="Override API base URL (e.g. for testing)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="Export devices as a CSV template (id, old_name, new_name)")
    p.add_argument("-o", "--output", help="Write CSV to this file instead of stdout")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("rename", help="Rename one device or many from a CSV (dry run unless --apply)")
    p.add_argument("--csv", help="CSV with columns id or old_name, and new_name")
    p.add_argument("--id", help="Device ID to rename")
    p.add_argument("--name", help="Current device name to rename")
    p.add_argument("--to", help="New device name")
    p.add_argument("--apply", action="store_true", help="Actually perform the renames")
    p.add_argument("--keep-going", action="store_true", help="Continue after a failed rename")
    p.add_argument("--ignore-errors", action="store_true", help="Apply valid rows even if some rows are invalid")
    p.add_argument("--no-verify", action="store_true", help="Skip the re-read check after each rename")
    p.add_argument(
        "--dump", nargs="?", metavar="DIR", const=time.strftime("dumps/%Y%m%d-%H%M%S"),
        help="Save before/request/response/after JSON per device into DIR "
             "(default: dumps/<timestamp>). With a dry run, saves before + planned request.",
    )
    p.set_defaults(func=cmd_rename)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ApiError as e:
        print(f"API error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
