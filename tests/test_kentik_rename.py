"""Tests run against a local fake Device API that mimics UpdateDevice's
"replace the configuration" behaviour, so a payload that drops a field shows
up as a verification failure.

Run: python3 -m unittest discover -s tests
"""

import argparse
import copy
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import kentik_rename as kr  # noqa: E402

PREFIX = "/device/v202504beta2/device"
EXAMPLE = os.path.join(os.path.dirname(__file__), "..", ".env.example")

DEVICES = {
    "101": {
        "id": "101", "companyId": "9", "deviceName": "edge_router_01", "deviceSubtype": "router",
        "deviceDescription": "core edge", "sendingIps": ["10.0.0.1"], "deviceSampleRate": "1000",
        "plan": {"id": "55", "name": "Default"}, "site": {"id": "7", "siteName": "NYC"},
        "labels": [{"id": "3", "name": "prod"}], "deviceSnmpIp": "10.0.0.1",
        "deviceSnmpCommunity": "********", "deviceBgpType": "device",
        "deviceBgpNeighborIp": "10.0.0.254", "deviceBgpPassword": "********",
        "nms": {"agentId": "a1", "ipAddress": "10.0.0.1", "snmp": {"credentialName": "c", "port": 161, "timeout": "5s"}},
        "minimizeSnmp": False, "cdnAttr": "", "deviceAlert": "",
    },
    "102": {
        "id": "102", "deviceName": "edge_router_02", "deviceSubtype": "router",
        "deviceSampleRate": "512", "sendingIps": ["10.0.0.2"],
        "plan": {"id": "55"}, "site": {"id": "7"}, "labels": [],
    },
}

# DeviceConcise (request) field -> how it maps back onto stored DeviceDetailed.
CONCISE_FIELDS = {kr._camel(f) for f in kr.PASSTHROUGH_FIELDS} | {"deviceName", "deviceSampleRate", "planId", "siteId"}


class FakeKentik(BaseHTTPRequestHandler):
    store: dict = {}
    requests: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        if self.headers.get("X-CH-Auth-Email") and self.headers.get("X-CH-Auth-API-Token"):
            return True
        self._send(401, {"code": 16, "message": "unauthenticated"})
        return False

    def do_GET(self):
        if not self._auth_ok():
            return
        path = self.path.split("?")[0]
        if path == PREFIX:
            if "view=DEVICE_VIEW_BASIC" in self.path:
                devs = [{"id": d["id"], "deviceName": d["deviceName"]} for d in self.store.values()]
            else:
                devs = list(self.store.values())
            return self._send(200, {"devices": devs})
        dev_id = path[len(PREFIX) + 1:]
        if dev_id in self.store:
            return self._send(200, {"device": self.store[dev_id]})
        self._send(404, {"code": 5, "message": "not found"})

    def do_PUT(self):
        if not self._auth_ok():
            return
        dev_id = self.path[len(PREFIX) + 1:]
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeKentik.requests.append(body)
        concise = {kr._camel(k): v for k, v in body["device"].items()}
        if dev_id not in self.store:
            return self._send(404, {"code": 5, "message": "not found"})
        if not kr.NAME_RE.match(concise.get("deviceName", "")):
            return self._send(400, {"code": 3, "message": "invalid device_name"})
        old = self.store[dev_id]
        new = {k: v for k, v in old.items() if kr._camel(k) not in CONCISE_FIELDS and k not in ("plan", "site")}
        # Replace semantics: every DeviceConcise field comes from the request (absent -> cleared).
        for k in CONCISE_FIELDS - {"deviceSampleRate", "planId", "siteId"}:
            if k in concise:
                new[k] = concise[k]
        if "deviceSampleRate" in concise:
            new["deviceSampleRate"] = str(int(concise["deviceSampleRate"]))
        if "planId" in concise:
            new["plan"] = dict(old.get("plan", {}), id=str(concise["planId"]))
        if "siteId" in concise:
            new["site"] = dict(old.get("site", {}), id=str(concise["siteId"]))
        self.store[dev_id] = new
        self._send(200, {"device": new})


class Base(unittest.TestCase):
    def setUp(self):
        FakeKentik.store = copy.deepcopy(DEVICES)
        FakeKentik.requests = []
        self.server = HTTPServer(("127.0.0.1", 0), FakeKentik)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        os.environ["KENTIK_EMAIL"] = "test@example.com"
        os.environ["KENTIK_API_TOKEN"] = "t"
        # Never read the developer's real .env during tests.
        self._orig_default_env = kr.default_env_file
        kr.default_env_file = lambda: None

    def tearDown(self):
        kr.default_env_file = self._orig_default_env
        self.server.shutdown()
        self.server.server_close()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = kr.main(["--base-url", self.url, *args])
        return code, out.getvalue(), err.getvalue()


class PayloadTests(unittest.TestCase):
    def test_maps_detailed_to_concise(self):
        p = kr.build_update_payload(DEVICES["101"], "new_name_1")
        self.assertEqual(p["id"], "101")
        self.assertEqual(p["device_name"], "new_name_1")
        self.assertEqual(p["device_sample_rate"], 1000.0)
        self.assertEqual(p["plan_id"], 55)
        self.assertEqual(p["site_id"], 7)
        self.assertEqual(p["sending_ips"], ["10.0.0.1"])
        self.assertEqual(p["nms"]["snmp"]["timeout"], "5s")
        for secret in kr.SECRET_FIELDS:
            self.assertNotIn(secret, p)
            self.assertNotIn(kr._camel(secret), p)

    def test_omits_empty_fields(self):
        # Real-world 400: device_bgp_credential_name 'Expected a value of type `never`, but received: ""'
        d = dict(DEVICES["101"], deviceBgpCredentialName="", flowSnmpCredentialName="",
                 monitoringTemplateId=0, deviceBgpFlowspec=False, site={"id": "0"},
                 nms={"agentId": "", "ipAddress": "", "snmp": {"credentialName": "", "port": 0}})
        p = kr.build_update_payload(d, "new_name_1")
        for k in ("device_bgp_credential_name", "flow_snmp_credential_name", "cdn_attr",
                  "device_alert", "monitoring_template_id", "site_id"):
            self.assertNotIn(k, p)
        self.assertEqual(p["nms"], {"snmp": {"port": 0}})
        self.assertIs(p["device_bgp_flowspec"], False)
        self.assertIs(p["minimize_snmp"], False)

    def test_accepts_snake_case_responses(self):
        d = {"id": "1", "device_name": "abcd", "device_sample_rate": "10", "plan": {"id": "2"}, "device_subtype": "router"}
        p = kr.build_update_payload(d, "efgh")
        self.assertEqual((p["plan_id"], p["device_subtype"], p["device_sample_rate"]), (2, "router", 10.0))

    def test_name_validation(self):
        for good in ("abcd", "edge_01", "A" * 60, "hzn-vmx-cr1", "edge.nyc.01", "a-b.c_d"):
            self.assertTrue(kr.NAME_RE.match(good), good)
        for bad in ("abc", "A" * 61, "edge 01", "edge/01", "edge@01", "édge01", ""):
            self.assertFalse(kr.NAME_RE.match(bad), bad)


class PlanTests(unittest.TestCase):
    devs = [{"id": "1", "deviceName": "aaaa"}, {"id": "2", "deviceName": "bbbb"}]

    def test_by_id_and_name(self):
        plan, errors, _ = kr.plan_renames(self.devs, [("id:1", "cccc"), ("name:bbbb", "dddd")])
        self.assertEqual(errors, [])
        self.assertEqual([(p["id"], p["new_name"]) for p in plan], [("1", "cccc"), ("2", "dddd")])

    def test_rejects_collisions_unknown_and_invalid(self):
        plan, errors, _ = kr.plan_renames(self.devs, [
            ("id:1", "bbbb"),     # taken by device 2
            ("id:9", "zzzz"),     # unknown
            ("id:2", "bad name"),  # invalid chars
        ])
        self.assertEqual(plan, [])
        self.assertEqual(len(errors), 3)

    def test_rejects_duplicate_targets_and_skips_noop(self):
        plan, errors, _ = kr.plan_renames(self.devs, [("id:1", "eeee"), ("id:2", "eeee"), ("id:1", "aaaa")])
        self.assertEqual(len(plan), 1)
        self.assertEqual(len(errors), 1)


class EndToEndTests(Base):
    def test_dry_run_changes_nothing(self):
        code, out, _ = self.run_cli("rename", "--id", "101", "--to", "edge_nyc_01")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", out)
        self.assertEqual(FakeKentik.requests, [])

    def test_apply_renames_and_preserves_config(self):
        before = copy.deepcopy(FakeKentik.store["101"])
        code, out, err = self.run_cli("rename", "--name", "edge_router_01", "--to", "edge_nyc_01", "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn("renamed and verified", out)
        after = FakeKentik.store["101"]
        self.assertEqual(after["deviceName"], "edge_nyc_01")
        for k in ("sendingIps", "deviceSnmpIp", "nms", "deviceBgpNeighborIp", "labels", "deviceDescription"):
            self.assertEqual(after[k], before[k], k)
        self.assertEqual(after["plan"]["id"], "55")
        self.assertEqual(after["deviceSampleRate"], "1000")

    def test_verification_catches_dropped_field(self):
        orig = kr.build_update_payload
        kr.build_update_payload = lambda d, n: {k: v for k, v in orig(d, n).items() if k != "sending_ips"}
        try:
            code, out, _ = self.run_cli("rename", "--id", "101", "--to", "edge_nyc_01", "--apply")
        finally:
            kr.build_update_payload = orig
        self.assertEqual(code, 1)
        self.assertIn("OTHER FIELDS CHANGED", out)
        self.assertIn("sending_ips", out)

    def test_csv_list_then_bulk_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "devices.csv")
            self.assertEqual(self.run_cli("list", "-o", path)[0], 0)
            with open(path) as f:
                lines = f.read().splitlines()
            self.assertEqual(lines[0], "id,old_name,new_name")
            with open(path, "w") as f:
                f.write("id,old_name,new_name\n101,edge_router_01,nyc_edge_01\n102,edge_router_02,nyc_edge_02\n")
            code, out, err = self.run_cli("rename", "--csv", path, "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(FakeKentik.store["101"]["deviceName"], "nyc_edge_01")
        self.assertEqual(FakeKentik.store["102"]["deviceName"], "nyc_edge_02")

    def test_invalid_rows_block_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.csv")
            with open(path, "w") as f:
                f.write("old_name,new_name\nedge_router_01,ok_name_1\nedge_router_02,bad name\n")
            code, _, err = self.run_cli("rename", "--csv", path, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("invalid new name", err)
        self.assertEqual(FakeKentik.requests, [])

    def test_dump_saves_snapshots_on_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump = os.path.join(tmp, "snap")
            code, out, err = self.run_cli("rename", "--id", "101", "--to", "edge.nyc-01", "--apply", "--dump", dump)
            self.assertEqual(code, 0, out + err)
            folder = os.path.join(dump, "101_edge_router_01")
            self.assertEqual(sorted(os.listdir(folder)), ["after.json", "before.json", "request.json", "response.json", "status.json"])
            load = lambda n: json.load(open(os.path.join(folder, n + ".json")))
            self.assertEqual(load("before")["deviceName"], "edge_router_01")
            self.assertEqual(load("after")["deviceName"], "edge.nyc-01")
            self.assertEqual(load("request")["device"]["device_name"], "edge.nyc-01")
            self.assertEqual(oct(os.stat(dump).st_mode & 0o777), "0o700")
            self.assertEqual(oct(os.stat(os.path.join(folder, "before.json")).st_mode & 0o777), "0o600")

    def test_dump_on_dry_run_saves_plan_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump = os.path.join(tmp, "snap")
            code, out, _ = self.run_cli("rename", "--id", "102", "--to", "nyc_edge_02", "--dump", dump)
            self.assertEqual(code, 0)
            self.assertEqual(sorted(os.listdir(os.path.join(dump, "102_edge_router_02"))), ["before.json", "request.json", "status.json"])
        self.assertEqual(FakeKentik.requests, [])
        self.assertEqual(FakeKentik.store["102"]["deviceName"], "edge_router_02")

    def test_dump_has_a_folder_for_every_csv_row(self):
        # Row 1 already has its new name (skipped), row 2 fails, row 3 never runs.
        FakeKentik.store["103"] = dict(DEVICES["102"], id="103", deviceName="edge_router_03")
        with tempfile.TemporaryDirectory() as tmp:
            path, dump = os.path.join(tmp, "m.csv"), os.path.join(tmp, "snap")
            with open(path, "w") as f:
                f.write("id,new_name\n101,edge_router_01\n102,nyc_edge_02\n103,nyc_edge_03\n")
            orig = kr.Client.update_device
            kr.Client.update_device = lambda self, c: (_ for _ in ()).throw(kr.ApiError(400, "boom"))
            try:
                code, out, _ = self.run_cli("rename", "--csv", path, "--apply", "--dump", dump)
            finally:
                kr.Client.update_device = orig
            self.assertEqual(code, 1)
            self.assertEqual(sorted(os.listdir(dump)), ["101_edge_router_01", "102_edge_router_02", "103_edge_router_03"])
            status = lambda d: json.load(open(os.path.join(dump, d, "status.json")))["status"]
            self.assertEqual(status("101_edge_router_01"), "skipped")
            self.assertEqual(status("102_edge_router_02"), "failed")
            self.assertEqual(status("103_edge_router_03"), "not_run")
            self.assertTrue(os.path.exists(os.path.join(dump, "101_edge_router_01", "before.json")))
            self.assertTrue(os.path.exists(os.path.join(dump, "102_edge_router_02", "request.json")))
            self.assertIn("NOT RUN [103]", out)
            self.assertIn("already have the new name", out)

    def test_dump_default_folder_is_timestamped(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                code, out, _ = self.run_cli("rename", "--id", "102", "--to", "nyc_edge_02", "--dump")
                self.assertEqual(code, 0)
                runs = os.listdir(os.path.join(tmp, "dumps"))
                self.assertEqual(len(runs), 1)
                self.assertRegex(runs[0], r"^\d{8}-\d{6}$")
            finally:
                os.chdir(cwd)

    def test_missing_credentials(self):
        del os.environ["KENTIK_API_TOKEN"]
        with self.assertRaises(SystemExit):
            self.run_cli("list")


class EnvFileTests(unittest.TestCase):
    KEYS = ("KENTIK_EMAIL", "KENTIK_API_TOKEN", "KENTIK_REGION")

    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in self.KEYS}
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_default = kr.default_env_file
        kr.default_env_file = lambda: None

    def tearDown(self):
        kr.default_env_file = self.orig_default
        self.tmp.cleanup()
        for k, v in self.saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def env(self, text):
        path = os.path.join(self.tmp.name, ".env")
        with open(path, "w") as f:
            f.write(text)
        return path

    def settings(self, *argv):
        ns = argparse.Namespace(region=None, base_url=None, env_file=None)
        for k, v in zip(argv[::2], argv[1::2]):
            setattr(ns, k, v)
        return kr.resolve_settings(ns)

    def test_parses_env_file_formats(self):
        path = self.env('# comment\n\nexport KENTIK_EMAIL="me@x.com"\n'
                        "KENTIK_API_TOKEN='abc#123'\nKENTIK_REGION=EU   # inline comment\n")
        self.assertEqual(kr.load_env_file(path),
                         {"KENTIK_EMAIL": "me@x.com", "KENTIK_API_TOKEN": "abc#123", "KENTIK_REGION": "EU"})

    def test_env_file_supplies_credentials_and_region(self):
        path = self.env("KENTIK_EMAIL=me@x.com\nKENTIK_API_TOKEN=tok\nKENTIK_REGION=EU\n")
        self.assertEqual(self.settings("env_file", path), (kr.REGIONS["eu"], "me@x.com", "tok"))

    def test_region_defaults_to_us_and_flag_overrides(self):
        path = self.env("KENTIK_EMAIL=a\nKENTIK_API_TOKEN=b\n")
        self.assertEqual(self.settings("env_file", path)[0], kr.REGIONS["us"])
        path = self.env("KENTIK_EMAIL=a\nKENTIK_API_TOKEN=b\nKENTIK_REGION=eu\n")
        self.assertEqual(self.settings("env_file", path, "region", "us")[0], kr.REGIONS["us"])

    def test_real_environment_beats_env_file(self):
        path = self.env("KENTIK_EMAIL=file@x.com\nKENTIK_API_TOKEN=filetok\n")
        os.environ["KENTIK_API_TOKEN"] = "envtok"
        self.assertEqual(self.settings("env_file", path)[1:], ("file@x.com", "envtok"))

    def test_rejects_placeholders_and_bad_region(self):
        with self.assertRaises(SystemExit) as cm:
            self.settings("env_file", self.env(open(EXAMPLE).read()))
        self.assertIn("placeholder", str(cm.exception))
        with self.assertRaises(SystemExit) as cm:
            self.settings("env_file", self.env("KENTIK_EMAIL=a\nKENTIK_API_TOKEN=b\nKENTIK_REGION=APAC\n"))
        self.assertIn("Unknown region", str(cm.exception))

    def test_missing_env_file_flag(self):
        with self.assertRaises(SystemExit):
            self.settings("env_file", os.path.join(self.tmp.name, "nope.env"))

    def test_cli_reads_env_file(self):
        path = self.env("KENTIK_EMAIL=a\nKENTIK_API_TOKEN=b\nKENTIK_REGION=eu\n")
        captured = {}
        orig = kr.Client.list_devices
        def fake_list(self, basic=True):
            captured["base"] = self.base
            captured["email"] = self.headers["X-CH-Auth-Email"]
            return []
        kr.Client.list_devices = fake_list
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(kr.main(["--env-file", path, "list"]), 0)
        finally:
            kr.Client.list_devices = orig
        self.assertEqual(captured, {"base": kr.REGIONS["eu"] + "/device/v202504beta2", "email": "a"})


if __name__ == "__main__":
    unittest.main()
