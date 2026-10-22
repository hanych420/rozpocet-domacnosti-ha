import os
import tempfile
import unittest

import packing


class PackingTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db = packing.DB_PATH
        packing.DB_PATH = os.path.join(self.tempdir.name, "haneva.db")
        packing.init_db()

    def tearDown(self):
        packing.DB_PATH = self.original_db
        self.tempdir.cleanup()

    def test_shared_list_and_automatic_daily_quantity(self):
        trip = packing.save_trip({"name": "Chalupa", "start_date": "2026-10-22", "end_date": "2026-10-25"})
        state = packing.state()
        saved = state["trips"][0]
        self.assertEqual(saved["duration_days"], 4)
        socks = [item for item in saved["items"] if item["name"] == "Ponožky"]
        self.assertEqual({item["owner"] for item in socks}, {"hanych", "eva"})
        self.assertTrue(all(item["quantity"] == 4 for item in socks))
        self.assertTrue(any(item["owner"] == "shared" for item in saved["items"]))
        self.assertEqual(packing.add_essentials(trip["id"])["added"], 0)

    def test_toggle_edit_and_date_recalculation(self):
        trip = packing.save_trip({"name": "Dovolená", "start_date": "2026-11-01", "end_date": "2026-11-02"})
        item = packing.save_item({"name": "Tričko", "owner": "eva", "category": "Oblečení", "quantity_value": 2, "quantity_mode": "per_day"}, trip_id=trip["id"])
        self.assertEqual(item["quantity"], 4)
        packed = packing.save_item({"packed": True}, item_id=item["id"])
        self.assertTrue(packed["packed"])
        packing.save_trip({"end_date": "2026-11-04"}, trip["id"])
        updated = next(entry for entry in packing.state()["trips"][0]["items"] if entry["id"] == item["id"])
        self.assertEqual(updated["quantity"], 8)

    def test_validation_and_delete(self):
        with self.assertRaisesRegex(ValueError, "Konec"):
            packing.save_trip({"name": "Cesta", "start_date": "2026-12-03", "end_date": "2026-12-01"})
        trip = packing.save_trip({"name": "Cesta", "start_date": "2026-12-01", "end_date": "2026-12-01"})
        with self.assertRaisesRegex(ValueError, "kategorii"):
            packing.save_item({"name": "Věc", "owner": "shared", "category": "Vlastní"}, trip_id=trip["id"])
        packing.delete_trip(trip["id"])
        self.assertEqual(packing.state()["trips"], [])


if __name__ == "__main__":
    unittest.main()
