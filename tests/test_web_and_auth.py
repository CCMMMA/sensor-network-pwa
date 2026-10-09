import csv
import glob
import gzip
import io
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sensor_network_pwa.access_store import AccessStore
from sensor_network_pwa.config import load_config
from sensor_network_pwa.dashboard import build_public_station_snapshot
from sensor_network_pwa.influx import init_influx_runtime
from sensor_network_pwa.log import logger
from sensor_network_pwa.runtime import runtime
from sensor_network_pwa.storage import iter_csv_files_newest_first, iter_csv_rows, load_station_rows, to_float
from sensor_network_pwa.watchdog import run_watchdog_scan
from sensor_network_pwa.web import create_web_app, stations

STRONG = "Str0ng!Passw0rd"
XSS_FIELD = "</script><script>alert(1)</script>"


class WebAndAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "pathStorage": str(root / "storage"),
                    "webSessionSecret": "test-only-secret",
                    "baseUrl": "https://collector.example.org",
                }
            )
        )
        self.cfg = load_config(str(config))
        self.store = AccessStore(self.cfg["auth_db_path"])
        self.store.ensure_admin("admin", STRONG)
        self.assertEqual(self.store.create_user("bob", STRONG, "bob@example.org"), (True, "User created"))
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.write_row(
            "station",
            self.now,
            {
                "timestamp": self.now.isoformat().replace("+00:00", "Z"),
                "uuid": "station",
                "TempOut": 20,
                XSS_FIELD: 1,
            },
        )
        patcher = patch.dict(runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = create_web_app(self.cfg, self.store)

    def write_row(self, instrument_uuid, dt, row):
        # Same layout as the hourly CSV files written by sensor-network-collector.
        path = (
            Path(self.cfg["storage_root"])
            / instrument_uuid
            / dt.strftime("%Y/%m/%d")
            / f"{instrument_uuid}_{dt.strftime('%Y%m%d')}Z{dt.strftime('%H')}00.csv"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            if is_new:
                writer.writeheader()
            writer.writerow(row)

    def client(self, username=None):
        client = self.app.test_client()
        if username:
            response = client.post("/login", data={"username": username, "password": STRONG})
            self.assertEqual(response.status_code, 302)
        return client

    def test_non_admin_can_open_anomaly_log(self):
        self.store.upsert_anomaly("station", "lost_connectivity", "lost_connectivity:no_data")
        self.store.set_policy("station", "account", "admin")
        items = self.store.list_anomalies_for_user({"username": "bob", "role": "user"})
        self.assertEqual([item["station_uuid"] for item in items], ["station"])
        self.assertEqual(self.client("bob").get("/anomalies").status_code, 200)

    def test_station_page_escapes_field_names_and_tolerates_bad_page(self):
        response = self.client("bob").get("/station/station?page=abc")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn(XSS_FIELD, body)
        island = body.split('<script id="stationBrowseState" type="application/json">')[1].split("</script>")[0]
        self.assertIn(XSS_FIELD, json.loads(island)["numeric_cols"])

    def test_cross_site_writes_are_rejected(self):
        client = self.client("admin")
        form = {"instrument_uuid": "station", "policy": "open"}
        response = client.post("/admin/policy", data=form, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.get_policy("station"), "account")
        for origin in ("http://localhost", "https://collector.example.org", None):
            with self.subTest(origin=origin):
                headers = {"Origin": origin} if origin else {}
                self.assertEqual(client.post("/admin/policy", data=form, headers=headers).status_code, 302)
        self.assertEqual(self.store.get_policy("station"), "open")

    def test_session_cookie_and_security_headers(self):
        response = self.app.test_client().post("/login", data={"username": "bob", "password": STRONG})
        self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_password_change_requires_strong_password(self):
        client = self.client("bob")
        client.post("/change-password", data={"password": "weakpass", "password2": "weakpass"})
        self.assertIsNone(self.store.authenticate("bob", "weakpass"))
        client.post("/change-password", data={"password": "N3w!Passw0rd#", "password2": "N3w!Passw0rd#"})
        self.assertIsNotNone(self.store.authenticate("bob", "N3w!Passw0rd#"))

    def test_weak_reset_password_keeps_token_usable(self):
        token = self.store.create_password_reset_token("bob")
        client = self.client()
        client.post(
            "/reset-password", query_string={"token": token}, data={"password": "weakpass", "password2": "weakpass"}
        )
        self.assertIsNotNone(self.store.get_password_reset_user(token))
        client.post(
            "/reset-password",
            query_string={"token": token},
            data={"password": "N3w!Passw0rd#", "password2": "N3w!Passw0rd#"},
        )
        self.assertIsNotNone(self.store.authenticate("bob", "N3w!Passw0rd#"))

    def test_tokens_are_single_use(self):
        reset = self.store.create_password_reset_token("bob")
        self.assertIsNotNone(self.store.consume_password_reset_token(reset))
        self.assertIsNone(self.store.consume_password_reset_token(reset))
        login = self.store.create_login_token("bob")
        self.assertEqual(self.store.consume_login_token(login)["username"], "bob")
        self.assertIsNone(self.store.consume_login_token(login))

    def test_onboarding_token_is_single_use(self):
        self.store.create_account_request("new@example.org", "please")
        request_id = self.store.list_account_requests("pending")[0]["id"]
        self.store.approve_request(request_id, "admin")
        token = self.store.create_account_request_token(request_id)
        self.assertEqual(self.store.complete_account_request(token, "carol", STRONG), (True, "Account created"))
        ok, _ = self.store.complete_account_request(token, "dave", STRONG)
        self.assertFalse(ok)
        self.assertFalse(self.store.username_exists("dave"))

    def test_default_admin_password_forces_change(self):
        with self.assertLogs(logger, level="WARNING"):
            self.store.ensure_admin("root", "admin")
        self.assertEqual(self.store.get_user("root")["force_password_change"], 1)
        self.assertEqual(self.store.get_user("admin")["force_password_change"], 0)

    def test_uploaded_svg_logo_is_served_sandboxed(self):
        client = self.client("bob")
        svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        response = client.post("/station/station/logo", data={"logo": (io.BytesIO(svg), "logo.svg")})
        self.assertEqual(response.status_code, 302)
        name = Path(self.store.get_station_logo("station")["logo_path"]).name
        response = client.get(f"/assets/station/{name}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("sandbox", response.headers["Content-Security-Policy"])
        response.close()

    def test_download_removes_temporary_archive(self):
        pattern = os.path.join(tempfile.gettempdir(), "collector_download_*.zip")
        before = set(glob.glob(pattern))
        response = self.client("bob").post("/download", data={"instrument": "station"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_data().startswith(b"PK"))
        response.close()
        self.assertEqual(set(glob.glob(pattern)) - before, set())

    def test_invalid_form_values_return_400(self):
        response = self.client("bob").post(
            "/anomalies/silence", data={"station_uuid": "station", "anomaly_type": "x", "hours": "soon"}
        )
        self.assertEqual(response.status_code, 400)
        response = self.client("admin").post(
            "/station/station/chart-settings/import", data={"settings_file": (io.BytesIO(b"[]"), "s.json")}
        )
        self.assertEqual(response.status_code, 400)

    def test_limited_row_load_returns_latest_rows_in_order(self):
        for hours in (3, 2, 1):
            dt = self.now - timedelta(hours=hours)
            for minute in (0, 1):
                self.write_row("multi", dt.replace(minute=minute), {"timestamp": f"{hours}-{minute}"})
        root = self.cfg["storage_root"]
        everything = [row["timestamp"] for row in load_station_rows(root, "multi", limit=None)]
        self.assertEqual(everything, ["3-0", "3-1", "2-0", "2-1", "1-0", "1-1"])
        for limit in (1, 3, 6, 50):
            with self.subTest(limit=limit):
                rows = load_station_rows(root, "multi", limit=limit)
                self.assertEqual([row["timestamp"] for row in rows], everything[-limit:])

    def test_non_finite_values_are_not_numeric(self):
        for value in ("nan", "inf", "-inf", float("nan")):
            self.assertIsNone(to_float(value))
        self.assertEqual(to_float(" 1.5 "), 1.5)
        row = {"timestamp": self.now.isoformat(), "TempOut": "nan"}
        self.write_row("nanstation", self.now, row)
        snapshot = build_public_station_snapshot(
            self.cfg["storage_root"], "nanstation", cfg=self.cfg, access_store=self.store
        )
        self.assertEqual(snapshot["series"], [])

    def test_watchdog_scan_continues_after_station_failure(self):
        self.write_row("other", self.now, {"timestamp": self.now.isoformat(), "TempOut": 1, "HumOut": 2})
        calls = []

        def evaluate(station_uuid, rows, now_dt):
            calls.append(station_uuid)
            if station_uuid == "other":
                raise RuntimeError("boom")
            return {"alarms": []}

        with (
            patch("sensor_network_pwa.watchdog.evaluate_station_anomalies", evaluate),
            self.assertLogs(logger, level="ERROR"),
        ):
            run_watchdog_scan(self.cfg, self.store, self.cfg["storage_root"])
        self.assertEqual(calls, ["other", "station"])

    def scan_with_mail(self, at):
        """Run one watchdog scan at the given time; returns the (recipient, subject, body) sent."""
        sent = []

        def fake_send(cfg, recipients, subject, body):
            sent.append((recipients[0], subject, body))
            return True

        cfg = dict(self.cfg, smtp_enabled=True, smtp_host="smtp.example.org")
        with patch("sensor_network_pwa.watchdog.send_email", fake_send):
            run_watchdog_scan(cfg, self.store, self.cfg["storage_root"], now=at)
        return sent

    def test_failure_is_notified_on_status_change_and_at_notification_time(self):
        minute = timedelta(minutes=1)
        start = self.now + timedelta(hours=1)
        self.assertEqual(self.scan_with_mail(self.now), [])

        sent = self.scan_with_mail(start)
        self.assertEqual([m[0] for m in sent], ["bob@example.org"])
        self.assertIn("FAILURE: station - lost_connectivity", sent[0][1])
        # The following checks of the same failure stay silent until the notification time.
        self.assertEqual(self.scan_with_mail(start + minute), [])
        self.assertEqual(self.scan_with_mail(start + 59 * minute), [])
        sent = self.scan_with_mail(start + 60 * minute)
        self.assertEqual(len(sent), 1)
        self.assertIn("STILL FAILING", sent[0][1])

        # A shorter notification time chosen by the user is honoured.
        self.assertTrue(self.store.set_notification_interval("bob", 15)[0])
        self.assertEqual(self.scan_with_mail(start + 74 * minute), [])
        self.assertEqual(len(self.scan_with_mail(start + 75 * minute)), 1)

        # Data is back: the recovery is notified once, after the hold time.
        back = start + 80 * minute
        self.assertEqual(self.scan_with_mail(back - minute), [])
        for offset in (0, 1, 2, 3, 4, 5, 6):
            when = back + offset * minute
            self.write_row(
                "station",
                when,
                {
                    "timestamp": when.isoformat().replace("+00:00", "Z"),
                    "uuid": "station",
                    "TempOut": 20,
                    XSS_FIELD: 1,
                },
            )
        self.assertEqual(self.scan_with_mail(back), [])
        sent = self.scan_with_mail(back + 6 * minute)
        self.assertEqual(len(sent), 1)
        self.assertIn("BACK TO REGULAR", sent[0][1])
        self.assertEqual(self.scan_with_mail(back + 7 * minute), [])

    def test_notification_actions_stop_reminders(self):
        hour = timedelta(hours=1)
        start = self.now + hour
        self.store.update_user("admin", email="admin@example.org")
        self.assertEqual(len(self.scan_with_mail(start)), 2)
        bob, admin = self.client("bob"), self.client("admin")
        own = self.store.list_notifications_for_user("bob")[0]["id"]
        other = self.store.list_notifications_for_user("admin")[0]["id"]

        page = bob.get("/profile")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"lost_connectivity", page.data)
        self.assertIn(b'<option value="60" selected>', page.data)
        self.assertEqual(self.client().get("/profile").status_code, 302)

        # Another user's notification cannot be changed.
        bob.post(f"/profile/notifications/{other}", data={"action": "clear"})
        self.assertEqual(len(self.store.list_notifications_for_user("admin")), 1)

        bob.post(f"/profile/notifications/{own}", data={"action": "snooze", "hours": "4"})
        admin.post(f"/profile/notifications/{other}", data={"action": "acknowledge"})
        self.assertEqual(self.scan_with_mail(start + 2 * hour), [])
        # The snooze ends, the acknowledgement does not.
        self.assertEqual([m[0] for m in self.scan_with_mail(start + 5 * hour)], ["bob@example.org"])

        bob.post("/profile/notification-interval", data={"minutes": "0"})
        self.assertEqual(self.store.get_notification_interval("bob"), 0)
        self.assertEqual(self.scan_with_mail(start + 30 * hour), [])
        self.assertEqual(bob.post("/profile/notification-interval", data={"minutes": "7"}).status_code, 302)
        self.assertEqual(self.store.get_notification_interval("bob"), 0)

        bob.post(f"/profile/notifications/{own}", data={"action": "clear"})
        self.assertEqual(self.store.list_notifications_for_user("bob"), [])
        self.assertNotIn(b"lost_connectivity", bob.get("/profile").data)

    def test_notification_email_logs_in_to_the_profile_page(self):
        sent = self.scan_with_mail(self.now + timedelta(hours=1))
        link = next(line for line in sent[0][2].splitlines() if "/fast-login?" in line)
        self.assertTrue(link.startswith("https://collector.example.org/fast-login?token="))
        path = link[len("https://collector.example.org") :]

        client = self.client()
        response = client.get(path)
        self.assertEqual(response.headers["Location"], "/profile#notifications")
        self.assertIn(b"bob", client.get("/profile").data)
        # The link is single use: afterwards it leads to the page through the login form.
        response = self.client().get(path)
        self.assertTrue(response.headers["Location"].startswith("/login?next=/profile%23notifications"))
        self.assertEqual(self.client().get("/fast-login?token=bad&next=https://evil.example").status_code, 403)

    def test_login_redirects_only_to_local_paths(self):
        client = self.app.test_client()
        for target in ("https://example.com", "//example.com", "/\\example.com", "/\t/example.com"):
            with self.subTest(target=target):
                response = client.post(
                    "/login", query_string={"next": target}, data={"username": "bob", "password": STRONG}
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/")
        response = client.post(
            "/login", query_string={"next": "/?window=day"}, data={"username": "bob", "password": STRONG}
        )
        self.assertEqual(response.headers["Location"], "/?window=day")

    def test_forced_password_change_blocks_other_pages(self):
        self.store.set_force_password_change("bob", True)
        client = self.client("bob")
        response = client.get("/anomalies")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/change-password")
        self.assertEqual(client.post("/download", data={"instrument": "station"}).status_code, 403)
        client.post("/change-password", data={"password": "N3w!Passw0rd#", "password2": "N3w!Passw0rd#"})
        self.assertEqual(client.get("/anomalies").status_code, 200)

    def test_rows_longer_than_header_are_tolerated(self):
        path = next(Path(self.cfg["storage_root"], "station").rglob("*.csv"))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{self.now.isoformat()},station,21,2,surplus,values\n")
        rows = load_station_rows(self.cfg["storage_root"], "station")
        self.assertTrue(all(None not in row for row in rows))
        self.assertEqual(self.client("admin").get("/api/admin/dashboard").status_code, 200)

    def test_station_name_is_not_rendered_as_html_on_map(self):
        name = "<img src=x onerror=alert(1)>"
        self.write_row("named", self.now, {"timestamp": self.now.isoformat(), "name": name, "lat": 40, "lon": 14})
        body = self.client().get("/").get_data(as_text=True)
        self.assertNotIn(name, body)
        self.assertNotIn(self.cfg["storage_root"], body)

    def test_sample_session_secret_is_replaced_by_stored_secret(self):
        cfg = dict(self.cfg, web_session_secret="replace-with-a-strong-random-secret")
        with self.assertLogs(logger, level="WARNING"):
            first = create_web_app(cfg, self.store).secret_key
        self.assertNotEqual(first, cfg["web_session_secret"])
        with self.assertLogs(logger, level="WARNING"):
            self.assertEqual(create_web_app(dict(cfg, web_session_secret=""), self.store).secret_key, first)
        self.assertEqual(self.app.secret_key, "test-only-secret")

    def test_username_check_requires_onboarding_link(self):
        self.assertEqual(self.client().get("/api/check-username?username=bob").status_code, 403)
        self.store.create_account_request("new@example.org", "please")
        request_id = self.store.list_account_requests("pending")[0]["id"]
        self.store.approve_request(request_id, "admin")
        token = self.store.create_account_request_token(request_id)
        response = self.client().get("/api/check-username", query_string={"username": "bob", "token": token})
        self.assertEqual(response.get_json()["available"], False)

    def test_account_request_needs_valid_email(self):
        for email in ("nobody", "a@b", "a b@example.org", "a@example.org,b@example.org"):
            with self.subTest(email=email):
                self.assertFalse(self.store.create_account_request(email, "")[0])
        self.assertEqual(self.store.list_account_requests(), [])

    def test_restricted_station_is_hidden_from_public_views(self):
        self.store.set_policy("station", "restricted", "admin")
        anonymous = self.client()
        self.assertNotIn("/public/station/station", anonymous.get("/").get_data(as_text=True))
        response = anonymous.get("/public/station/station")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self.assertEqual(anonymous.get("/api/public/station/station/snapshot").status_code, 404)
        bob = self.client("bob")
        self.assertEqual(bob.get("/public/station/station").status_code, 404)
        self.assertEqual(bob.get("/api/public/station/station/snapshot").status_code, 404)
        self.store.set_user_instrument_access("bob", "station", True)
        self.assertEqual(bob.get("/public/station/station").status_code, 200)
        self.assertIn("/public/station/station", bob.get("/").get_data(as_text=True))
        self.assertEqual(self.client("admin").get("/api/public/station/station/snapshot").status_code, 200)
        self.store.set_policy("station", "account", "admin")
        self.assertEqual(anonymous.get("/public/station/station").status_code, 200)

    def test_pages_are_standards_mode_installable_pwa(self):
        client = self.client("admin")
        for path in ("/", "/login", "/station/station", "/public/station/station", "/admin", "/anomalies", "/offline"):
            with self.subTest(path=path):
                body = client.get(path).get_data(as_text=True)
                self.assertTrue(body.lstrip().lower().startswith("<!doctype html>"))
                self.assertEqual(body.count('<link rel="manifest" href="/manifest.webmanifest">'), 1)
                self.assertEqual(body.count('<script src="/static/js/pwa.js" data-sw-url="/service-worker.js"'), 1)
        script = client.get("/static/js/pwa.js")
        self.assertIn("navigator.serviceWorker.register", script.get_data(as_text=True))
        script.close()
        manifest = client.get("/manifest.webmanifest").get_json(force=True)
        self.assertEqual((manifest["display"], manifest["start_url"]), ("standalone", "/"))
        for icon in manifest["icons"]:
            response = client.get(icon["src"])
            size = int(icon["sizes"].split("x")[0])
            data = response.get_data()
            self.assertEqual(response.mimetype, "image/png")
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(int.from_bytes(data[16:20], "big"), size)
        self.assertEqual(client.get("/pwa/icon-64.png").status_code, 404)
        worker = client.get("/service-worker.js")
        self.assertEqual(worker.mimetype, "text/javascript")
        self.assertEqual(worker.headers["Cache-Control"], "no-cache")
        self.assertIn('"/offline"', worker.get_data(as_text=True))

    def test_pages_load_their_scripts_and_styles_from_static_files(self):
        client = self.client("admin")
        pages = (
            "/", "/login", "/station/station", "/station/station/chart-settings", "/public/station/station",
            "/admin", "/admin/dashboard", "/anomalies", "/profile",
        )  # fmt: skip
        assets = set()
        for path in pages:
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                body = response.get_data(as_text=True)
                # Only JSON data islands are inline; behaviour lives in the static files.
                inline = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", body)
                self.assertTrue(all('type="application/json"' in attrs for attrs in inline), inline)
                self.assertNotIn("<style>", body)
                assets.update(re.findall(r'(?:src|href)="(/static/[^"]+)"', body))
        self.assertIn("/static/js/station_browser.js", assets)
        self.assertIn("/static/css/public_station.css", assets)
        for asset in sorted(assets):
            with self.subTest(asset=asset):
                response = client.get(asset, headers={"Accept-Encoding": "gzip"})
                self.assertEqual(response.status_code, 200)
                text = response.get_data()
                if response.headers.get("Content-Encoding") == "gzip":
                    text = gzip.decompress(text)
                self.assertNotIn(b"{{", text)
                self.assertNotIn(b"{%", text)
                response.close()
        packed = client.get("/static/js/station_browser.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(packed.headers["Content-Encoding"], "gzip")
        self.assertIn(b"pageConfig", gzip.decompress(packed.get_data()))

    def test_page_config_island_escapes_station_values(self):
        hostile = "<img src=x onerror=alert(1)>&'"
        self.write_row(hostile, self.now, {"timestamp": self.now.isoformat(), "TempOut": 1, "HumOut": 2})
        body = self.client("admin").get("/").get_data(as_text=True)
        island = body.split('<script id="pageConfig" type="application/json">')[1].split("</script>")[0]
        for char in "<>&'":
            self.assertNotIn(char, island)
        self.assertIn(hostile, [station["uuid"] for station in json.loads(island)["stations"]])

    def test_file_selection_follows_requested_hours(self):
        root = self.cfg["storage_root"]
        base = datetime(2024, 12, 31, 22, 30, tzinfo=timezone.utc)
        for hours in range(5):  # 22:30 on 31 December to 02:30 on 1 January
            dt = base + timedelta(hours=hours)
            self.write_row("hourly", dt, {"timestamp": dt.isoformat(), "TempOut": hours})
        stray = Path(root, "hourly", "notes.csv")
        stray.write_text("timestamp,TempOut\nx,1\n")

        def hours(**bounds):
            return [row["TempOut"] for row in load_station_rows(root, "hourly", limit=None, **bounds)]

        newest_first = [p.name for p in iter_csv_files_newest_first(root, "hourly")]
        self.assertEqual(newest_first[0], "hourly_20250101Z0200.csv")
        self.assertEqual(len(newest_first), 6)
        self.assertEqual(hours(from_date=base + timedelta(hours=1), to_date=base + timedelta(hours=3)), ["1", "2", "3"])
        self.assertEqual(hours(from_date=base.date(), to_date=base.date()), ["0", "1"])
        self.assertEqual(hours(from_date=datetime(2025, 1, 1).date()), ["2", "3", "4"])
        self.assertEqual(hours(to_date=base), ["0"])
        self.assertEqual([row["TempOut"] for row in load_station_rows(root, "hourly", limit=2)], ["3", "4"])

    def test_short_and_blank_csv_rows(self):
        path = Path(self.temp.name, "ragged.csv")
        path.write_text("timestamp,a,b\n\nt1,1\nt2,1,2,3\n")
        self.assertEqual(
            list(iter_csv_rows(path)),
            [{"timestamp": "t1", "a": "1", "b": None}, {"timestamp": "t2", "a": "1", "b": "2"}],
        )

    def test_snapshot_poll_skips_unchanged_data(self):
        client = self.client()
        first = client.get("/api/public/station/station/snapshot").get_json()
        self.assertTrue(first["changed"])
        since = first["snapshot"]["last_timestamp"]
        unchanged = client.get("/api/public/station/station/snapshot", query_string={"since": since}).get_json()
        self.assertEqual(unchanged, {"changed": False})
        forced = client.get("/api/public/station/station/snapshot", query_string={"since": since, "force": "1"})
        self.assertIn("snapshot", forced.get_json())
        later = self.now + timedelta(seconds=30)
        self.write_row(
            "station",
            later,
            {
                "timestamp": later.isoformat().replace("+00:00", "Z"),
                "uuid": "station",
                "TempOut": 21,
                XSS_FIELD: 1,
            },
        )
        changed = client.get("/api/public/station/station/snapshot", query_string={"since": since}).get_json()
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["snapshot"]["rows"], 2)

    def test_large_responses_are_compressed_on_request(self):
        client = self.client("bob")
        plain = client.get("/station/station")
        self.assertIsNone(plain.headers.get("Content-Encoding"))
        packed = client.get("/station/station", headers={"Accept-Encoding": "gzip, br"})
        self.assertEqual(packed.headers["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", packed.headers["Vary"])
        self.assertEqual(gzip.decompress(packed.get_data()), plain.get_data())
        logo = client.post("/download", data={"instrument": "station"}, headers={"Accept-Encoding": "gzip"})
        self.assertIsNone(logo.headers.get("Content-Encoding"))
        logo.close()

    def test_station_chart_is_thinned_on_long_windows(self):
        with patch.object(stations, "STATION_BROWSER_MAX_CHART_POINTS", 4):
            for seconds in range(1, 11):
                dt = self.now - timedelta(seconds=seconds)
                self.write_row("dense", dt.replace(minute=dt.minute), {"timestamp": dt.isoformat(), "TempOut": seconds})
            body = self.client("bob").get("/station/dense?page_size=50").get_data(as_text=True)
        island = body.split('<script id="stationBrowseState" type="application/json">')[1].split("</script>")[0]
        state = json.loads(island)
        self.assertLessEqual(len(state["chart_labels"]), 5)
        self.assertEqual(len(state["numeric_series_aligned"]["TempOut"]), len(state["chart_labels"]))
        self.assertIn("all 10 rows", body)

    def test_cards_use_recent_rows_of_interleaved_devices(self):
        for seconds, row in ((40, {"TempOut": 18, "HumOut": 70}), (20, {"pm_10": 4}), (0, {"TempOut": 19})):
            dt = self.now - timedelta(seconds=seconds)
            self.write_row("mixed", dt, {"timestamp": dt.isoformat(), "TempOut": "", "HumOut": "", "pm_10": "", **row})
        snapshot = build_public_station_snapshot(
            self.cfg["storage_root"], "mixed", cfg=self.cfg, access_store=self.store
        )
        cards = {card["key"]: card["value"] for card in snapshot["cards"]}
        self.assertEqual((cards["temperature"], cards["humidity"], cards["pm10"]), (19, 70, 4))
        self.assertIsNone(cards["pressure"])

    def test_admin_page_manages_users_and_station_rights(self):
        admin = self.client("admin")
        body = admin.get("/admin").get_data(as_text=True)
        for text in ("Station rights", "Save station rights", "Account requests", "bob@example.org"):
            self.assertIn(text, body)

        response = admin.post(
            "/admin/create-user",
            data={"username": "erin", "email": "erin@example.org", "password": STRONG, "force_password_change": "1"},
        )
        self.assertEqual(response.headers["Location"], "/admin#users")
        self.assertEqual(self.store.get_user("erin")["force_password_change"], 1)
        self.assertIn("User erin created", admin.get("/admin").get_data(as_text=True))
        admin.post("/admin/create-user", data={"username": "weak", "password": "short"})
        self.assertIsNone(self.store.get_user("weak"))
        self.assertIn("Password must be at least 12 characters long", admin.get("/admin").get_data(as_text=True))

        self.store.set_policy("station", "restricted", "admin")
        admin.post(
            "/admin/user-permissions",
            data={"username": "bob", "station": ["station", "ghost"], "access": ["station"], "control": ["station"]},
        )
        self.assertEqual(self.store.get_user_instruments("bob"), ["station"])
        self.assertEqual(self.store.get_user_control_stations("bob"), ["station"])
        # Only the listed stations are touched, and unticked rights are removed.
        self.store.set_user_instrument_access("bob", "elsewhere", True)
        admin.post("/admin/user-permissions", data={"username": "bob", "station": ["station"]})
        self.assertEqual(self.store.get_user_instruments("bob"), ["elsewhere"])
        self.assertEqual(self.store.get_user_control_stations("bob"), [])

        admin.post(
            "/admin/station-permissions",
            data={"station_uuid": "station", "user": ["bob", "erin", "nobody"], "access": ["erin"], "control": ["bob"]},
        )
        self.assertEqual(self.store.get_user_instruments("erin"), ["station"])
        self.assertEqual(self.store.get_user_control_stations("bob"), ["station"])
        self.assertNotIn("station", self.store.get_user_instruments("bob"))
        self.assertEqual(
            self.client("bob").post("/admin/station-permissions", data={"station_uuid": "x"}).status_code, 403
        )

    def test_admin_account_actions_protect_the_last_admin(self):
        admin = self.client("admin")

        def act(username, action, **extra):
            admin.post("/admin/user-update", data={"username": username, "action": action, **extra})
            return self.store.get_user(username)

        self.assertEqual(act("bob", "email", email="new@example.org")["email"], "new@example.org")
        self.assertEqual(act("bob", "email", email="not-an-email")["email"], "new@example.org")
        self.assertEqual(act("bob", "deactivate")["active"], 0)
        self.assertIsNone(self.store.authenticate("bob", STRONG))
        self.assertEqual(act("bob", "activate")["active"], 1)
        self.assertEqual(act("admin", "make_user")["role"], "admin")
        self.assertEqual(act("admin", "deactivate")["active"], 1)
        self.assertEqual(self.store.update_user("admin", active=False)[0], False)
        self.assertEqual(act("bob", "make_admin")["role"], "admin")
        # With a second administrator the first one can be demoted, and restored.
        self.assertEqual(self.store.update_user("admin", role="user")[0], True)
        self.assertEqual(self.store.update_user("admin", role="admin")[0], True)
        admin.post("/admin/force-password", data={"username": "bob"})
        self.assertEqual(self.store.get_user("bob")["force_password_change"], 1)
        admin.post("/admin/force-password", data={"username": "bob", "force": "0"})
        self.assertEqual(self.store.get_user("bob")["force_password_change"], 0)

    def test_admin_can_set_user_password(self):
        admin = self.client("admin")
        new_password = "Adm1n!AssignedPassword"

        body = admin.get("/admin").get_data(as_text=True)
        self.assertIn('action="/admin/set-password"', body)

        response = admin.post(
            "/admin/set-password",
            data={
                "username": "bob",
                "password": new_password,
                "password2": "different",
            },
        )
        self.assertEqual(response.headers["Location"], "/admin#users")
        self.assertIsNotNone(self.store.authenticate("bob", STRONG))

        admin.post(
            "/admin/set-password",
            data={
                "username": "bob",
                "password": "weak",
                "password2": "weak",
            },
        )
        self.assertIsNotNone(self.store.authenticate("bob", STRONG))

        admin.post(
            "/admin/set-password",
            data={
                "username": "bob",
                "password": new_password,
                "password2": new_password,
                "force_password_change": "1",
            },
        )
        self.assertIsNone(self.store.authenticate("bob", STRONG))
        self.assertIsNotNone(self.store.authenticate("bob", new_password))
        self.assertEqual(self.store.get_user("bob")["force_password_change"], 1)

        non_admin = self.app.test_client()
        non_admin.post("/login", data={"username": "bob", "password": new_password})
        self.assertEqual(
            non_admin.post(
                "/admin/set-password",
                data={
                    "username": "admin",
                    "password": new_password,
                    "password2": new_password,
                },
            ).status_code,
            403,
        )

    def test_approval_without_email_shows_the_onboarding_link(self):
        self.store.create_account_request("new@example.org", "please")
        request_id = self.store.list_account_requests("pending")[0]["id"]
        admin = self.client("admin")
        response = admin.post(f"/admin/requests/{request_id}/approve")
        self.assertEqual(response.headers["Location"], "/admin#requests")
        body = admin.get("/admin").get_data(as_text=True)
        self.assertIn("https://collector.example.org/request-account/complete?token=", body)
        token = body.split("complete?token=")[1].split("<")[0].strip()
        self.assertEqual(self.store.complete_account_request(token, "newuser", STRONG), (True, "Account created"))

    def test_station_page_time_ranges_paging_and_order(self):
        for minutes in range(1, 121):
            dt = self.now - timedelta(minutes=minutes)
            self.write_row("paged", dt, {"timestamp": dt.isoformat(), "TempOut": minutes})
        client = self.client("bob")

        def page(**args):
            body = client.get("/station/paged", query_string=args).get_data(as_text=True)
            first = body.split("<tbody>")[1].split("</tr>")[0]
            return body, first

        body, first = page(interval="3h")
        self.assertIn("<b>120</b> rows", body)
        self.assertIn("Page 1 of 3", body)
        self.assertIn(">120<", first)  # oldest first
        body, first = page(interval="3h", order="desc", page_size="100", page="2")
        self.assertIn("Page 2 of 2", body)
        self.assertIn(">101<", first)  # newest first: page 2 starts at the 101st newest row
        body, _ = page(interval="hour")
        self.assertIn("<b>61</b> rows", body)  # both ends of the hour are included
        # A remembered page size comes from the cookie; an unknown one falls back to 50.
        client.set_cookie("station_page_size", "250")
        self.assertIn("Page 1 of 1", page(interval="3h")[0])
        self.assertIn("Page 1 of 3", page(interval="3h", page_size="7")[0])

        day = self.now.date().isoformat()
        body, _ = page(from_date=day, to_date=day)
        self.assertIn("custom dates", body)
        self.assertEqual(
            client.get("/station/paged", query_string={"from_date": "2024-01-01", "to_date": "2024-03-01"}).status_code,
            400,
        )
        empty = client.get("/station/paged", query_string={"interval": "hour", "anchor": "2020-01-01T00:00:00Z"})
        self.assertEqual(empty.status_code, 200)
        self.assertIn("No data in this time range", empty.get_data(as_text=True))
        anonymous = self.client().get("/station/paged")
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/login", anonymous.headers["Location"])

    def test_station_csv_export_matches_the_selected_range(self):
        for minutes in (90, 30, 10):
            dt = self.now - timedelta(minutes=minutes)
            self.write_row("csvst", dt, {"timestamp": dt.isoformat(), "TempOut": minutes, "HumOut": 50, "note": "a,b"})
        client = self.client("bob")
        response = client.get("/station/csvst/export.csv", query_string={"interval": "hour"})
        self.assertEqual(response.mimetype, "text/csv")
        self.assertIn("attachment; filename=", response.headers["Content-Disposition"])
        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual([row["TempOut"] for row in rows], ["30", "10"])
        self.assertEqual(rows[0]["note"], "a,b")
        response = client.get(
            "/station/csvst/export.csv", query_string={"interval": "3h", "col": ["timestamp", "HumOut", "missing"]}
        )
        lines = response.get_data(as_text=True).splitlines()
        self.assertEqual(lines[0], "timestamp,HumOut")
        self.assertEqual(len(lines), 4)
        self.assertEqual(
            client.get(
                "/station/csvst/export.csv", query_string={"interval": "hour", "anchor": "2020-01-01T00:00:00Z"}
            ).status_code,
            404,
        )
        self.store.set_policy("csvst", "restricted", "admin")
        self.assertEqual(client.get("/station/csvst/export.csv").status_code, 403)
        self.assertEqual(self.client().get("/station/csvst/export.csv").status_code, 302)

    def test_config_needs_no_collector_settings(self):
        self.assertFalse(self.cfg["enable_influx"])
        self.assertTrue(self.cfg["auth_db_path"].endswith("collector_auth.sqlite"))
        with patch.dict(runtime):
            self.assertIsNone(init_influx_runtime(self.cfg))


if __name__ == "__main__":
    unittest.main()
