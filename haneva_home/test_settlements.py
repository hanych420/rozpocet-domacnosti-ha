import os
import tempfile
import unittest

import settlements


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db = settlements.DB_PATH
        settlements.DB_PATH = os.path.join(self.tempdir.name, "haneva.db")
        settlements.init_db()

    def tearDown(self):
        settlements.DB_PATH = self.original_db
        self.tempdir.cleanup()

    def save(self, **values):
        payload = {
            "entry_type": "expense",
            "paid_by": "hanych",
            "amount": 100,
            "title": "Společný nákup",
            "occurred_on": "2026-10-07",
        }
        payload.update(values)
        return settlements.save_entry(payload)

    def test_expenses_split_equally_and_settlement_reduces_balance(self):
        hanych = self.save(amount="1000", title="Veterinář")
        self.save(paid_by="eva", amount=400, title="Nákup")
        self.save(entry_type="settlement", paid_by="eva", amount=250, title="")

        data = settlements.summary("2026-10")
        self.assertEqual(data["paid"], {"hanych": 1000.0, "eva": 400.0})
        self.assertEqual(data["personal_expenses"], 1400.0)
        self.assertEqual(data["balance"], 50.0)
        self.assertEqual(data["balance_status"]["from"], "Eva")
        self.assertEqual(data["balance_status"]["to"], "Hanych")
        self.assertEqual(data["balance_status"]["amount"], 50.0)
        settlement = next(item for item in data["entries"] if item["entry_type"] == "settlement")
        self.assertEqual(settlement["paid_by_name"], "Eva")
        self.assertEqual(settlement["received_by_name"], "Hanych")

        settlements.save_entry({"amount": 1200}, hanych["id"])
        updated = settlements.summary("2026-10")
        self.assertEqual(updated["paid"]["hanych"], 1200.0)
        self.assertEqual(updated["balance"], 150.0)

    def test_opposite_settlement_and_delete(self):
        self.save(paid_by="eva", amount=600)
        payment = self.save(entry_type="settlement", paid_by="hanych", amount=100, title="")
        data = settlements.summary("2026-10")
        self.assertEqual(data["balance"], -200.0)
        self.assertEqual(data["balance_status"]["from"], "Hanych")
        self.assertEqual(data["balance_status"]["to"], "Eva")
        settlements.delete_entry(payment["id"])
        self.assertEqual(settlements.summary("2026-10")["balance"], -300.0)

    def test_validation_and_empty_state(self):
        empty = settlements.summary("2026-10")
        self.assertEqual(empty["balance"], 0)
        self.assertEqual(empty["balance_status"]["text"], "Máte vyrovnáno.")
        with self.assertRaisesRegex(ValueError, "Částka"):
            self.save(amount=0)
        with self.assertRaisesRegex(ValueError, "kdo platil"):
            self.save(paid_by="někdo")
        with self.assertRaisesRegex(ValueError, "za co"):
            self.save(title="")


if __name__ == "__main__":
    unittest.main()
