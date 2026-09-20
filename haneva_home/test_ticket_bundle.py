"""Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s haneva_home -p 'test_ticket*.py' -v"""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from PIL import Image
import app


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_patch = patch.object(app, "DB_PATH", str(self.root / "calendar.db"))
        self.dir_patch = patch.object(app, "TICKET_DIR", str(self.root / "tickets"))
        self.db_patch.start(); self.dir_patch.start()
        with patch.object(app.shopping, "init_db"):
            app.init_db()
        with app.db() as conn:
            conn.execute("INSERT INTO events(title,calendar,start_date,end_date,event_type) VALUES('Test','spolecne','2026-01-01','2026-01-01','zabava')")
        self.body = b"%PDF-1.4\nfixture"

    def tearDown(self):
        self.dir_patch.stop(); self.db_patch.stop(); self.temp.cleanup()

    def scanner(self, count):
        def run(args, **kwargs):
            output = Path(args[-1]); codes = []
            for index in range(count):
                name = f"qr-{index}.png"
                Image.new("RGB", (50, 50), (index*10, 0, 0)).save(output / name)
                codes.append({"file": name, "digest": f"unique-{index}"})
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"codes": codes}).encode())
        return run

    def save(self, count):
        with patch.object(app.subprocess, "run", side_effect=self.scanner(count)):
            return app.save_ticket_bundle(1, "all.pdf", self.body)

    def test_one_and_many_unique_paths_numeric_order(self):
        for count in (1, 2, 12):
            tickets = self.save(count)
            self.assertEqual([t["owner"] for t in tickets], [str(i) for i in range(1, count+1)])
            self.assertEqual(len({t["qr_url"] for t in tickets}), count)
            rows = app.get_tickets(1)
            self.assertEqual(len({r["stored_name"] for r in rows}), 1)
            self.assertEqual(len(list(Path(app.TICKET_DIR).iterdir())), count+1)
            for row in rows:
                self.assertEqual(app.safe_ticket_path(row["stored_name"]).read_bytes(), self.body)

    def test_failed_scan_preserves_all_previous_files(self):
        self.save(3)
        before = [dict(row) for row in app.get_tickets(1)]
        files = sorted(Path(app.TICKET_DIR).iterdir())
        with patch.object(app.subprocess, "run", side_effect=subprocess.TimeoutExpired("scan", 75)):
            with self.assertRaises(ValueError): app.save_ticket_bundle(1, "new.pdf", self.body)
        self.assertEqual(before, [dict(row) for row in app.get_tickets(1)])
        self.assertEqual(files, sorted(Path(app.TICKET_DIR).iterdir()))

    def test_delete_one_keeps_shared_original_then_delete_all(self):
        self.save(3); original = app.safe_ticket_path(app.get_tickets(1)[0]["stored_name"])
        app.delete_ticket(1, "1"); self.assertTrue(original.exists())
        app.delete_ticket(1); self.assertFalse(original.exists())
        self.assertEqual(list(Path(app.TICKET_DIR).iterdir()), [])

    def test_reject_duplicate_scan_result(self):
        def duplicate(args, **kwargs):
            result = self.scanner(2)(args, **kwargs)
            data = json.loads(result.stdout); data["codes"][1]["digest"] = data["codes"][0]["digest"]
            result.stdout = json.dumps(data).encode(); return result
        with patch.object(app.subprocess, "run", side_effect=duplicate):
            with self.assertRaises(ValueError): app.save_ticket_bundle(1, "same.pdf", self.body)
        self.assertEqual(app.get_tickets(1), [])

    def test_concurrent_event_deletion_does_not_leave_orphans(self):
        def remove_event(args, **kwargs):
            result = self.scanner(2)(args, **kwargs)
            with app.db() as conn: conn.execute("DELETE FROM events WHERE id=1")
            return result
        with patch.object(app.subprocess, "run", side_effect=remove_event):
            with self.assertRaises(ValueError): app.save_ticket_bundle(1, "all.pdf", self.body)
        self.assertEqual(list(Path(app.TICKET_DIR).iterdir()), [])

    def test_no_parallel_scans(self):
        app.TICKET_SCAN_LOCK.acquire()
        try:
            with self.assertRaises(ValueError): app.save_ticket_bundle(1, "all.pdf", self.body)
        finally: app.TICKET_SCAN_LOCK.release()

    def test_legacy_files_remain_readable(self):
        for owner in ("hanych", "eva"):
            (Path(app.TICKET_DIR) / (owner+".pdf")).write_bytes(self.body)
            with app.db() as conn:
                conn.execute("INSERT INTO event_tickets(event_id,owner,original_name,stored_name,mime_type,size) VALUES(1,?,'old.pdf',?,'application/pdf',10)", (owner, owner+".pdf"))
        with patch.object(app.shopping, "init_db"): app.init_db()
        self.assertEqual(len(app.get_tickets(1)), 2)
        self.save(1)
        self.assertEqual(len(list(Path(app.TICKET_DIR).iterdir())), 2)

    def test_http_upload_detail_qr_original_delete(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def request(path, method="GET", data=None):
            req = urllib.request.Request(base+path, method=method, data=data,
                headers={"Content-Type": "application/pdf", "X-Filename": "all.pdf"})
            with urllib.request.urlopen(req) as response:
                return response.status, response.headers, response.read()
        try:
            with patch.object(app.subprocess, "run", side_effect=self.scanner(3)):
                status, _, content = request("/api/events/1/tickets", "POST", self.body)
            self.assertEqual(status, 201); self.assertEqual(len(json.loads(content)["tickets"]), 3)
            _, _, content = request("/api/events/1")
            tickets = json.loads(content)["event"]["tickets"]
            crops = []
            for ticket in tickets:
                _, _, original = request(ticket["original_url"]); self.assertEqual(original, self.body)
                _, headers, qr = request(ticket["qr_url"])
                self.assertEqual(headers.get_content_type(), "image/png"); crops.append(qr)
            self.assertEqual(len(set(crops)), 3)
            request("/api/events/1/tickets", "DELETE")
            self.assertEqual(list(Path(app.TICKET_DIR).iterdir()), [])
        finally:
            server.shutdown(); thread.join(); server.server_close()


class ScannerLogicTests(unittest.TestCase):
    """Decoder boundaries are mocked; actual decoding is tested separately when libzbar exists."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.source = self.root / "input.png"; Image.new("RGB", (1000, 1000), "white").save(self.source)
        fake = types.ModuleType("pyzbar.pyzbar"); fake.decode = lambda *a, **k: []
        fake.ZBarSymbol = types.SimpleNamespace(QRCODE=64)
        spec = importlib.util.spec_from_file_location("test_scan_worker", Path(__file__).with_name("ticket_scan.py"))
        self.worker = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"pyzbar.pyzbar": fake}): spec.loader.exec_module(self.worker)

    def tearDown(self): self.temp.cleanup()

    def symbol(self, data, top):
        return types.SimpleNamespace(data=data, rect=Rect(100, top, 100, 100))

    def test_order_and_dedup(self):
        first, second = self.symbol(b"first", 100), self.symbol(b"second", 500)
        with patch.object(self.worker, "decode", side_effect=[[second, first, first], [first], [second]]):
            result = self.worker.scan(self.source, "image/png", self.root)
        self.assertEqual([r["digest"] for r in result], [hashlib.sha256(b"first").hexdigest(), hashlib.sha256(b"second").hexdigest()])

    def test_one_code_and_no_code(self):
        one = self.symbol(b"only", 100)
        with patch.object(self.worker, "decode", return_value=[one]):
            self.assertEqual(len(self.worker.scan(self.source, "image/png", self.root)), 1)
        with patch.object(self.worker, "decode", return_value=[]):
            with self.assertRaises(ValueError): self.worker.scan(self.source, "image/png", self.root)


from collections import namedtuple
Rect = namedtuple("Rect", "left top width height")


class RealDecoderTests(unittest.TestCase):
    def test_real_image_and_multipage_pdf(self):
        try:
            from pyzbar.pyzbar import decode
            import qrcode
            import ticket_scan
        except ImportError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = qrcode.make("HANEVA-TEST-ONE").convert("RGB")
            second = qrcode.make("HANEVA-TEST-TWO").convert("RGB")
            page = Image.new("RGB", (1000, 1400), "white")
            page.paste(first, (100, 100)); page.paste(second, (100, 700))
            source = root / "two.png"; page.save(source)
            result = ticket_scan.scan(source, "image/png", root)
            self.assertEqual(len(result), 2)
            decoded = [decode(Image.open(root / item["file"]))[0].data for item in result]
            self.assertEqual(decoded, [b"HANEVA-TEST-ONE", b"HANEVA-TEST-TWO"])
            source = root / "multiple.pdf"
            first.save(source, "PDF", save_all=True, append_images=[first, second], resolution=100)
            self.assertEqual(len(ticket_scan.scan(source, "application/pdf", root)), 2)


if __name__ == "__main__": unittest.main()
