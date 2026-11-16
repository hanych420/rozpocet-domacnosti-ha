import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

import christmas


class ChristmasTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        christmas.DB_PATH = str(root / "christmas.db")
        christmas.IMAGE_DIR = str(root / "images")
        christmas.init_db()

    def tearDown(self):
        self.temp.cleanup()

    def create_wish(self, recipient="honzicek", actor="honzicek"):
        return christmas.save_wish(
            {
                "recipient_key": recipient,
                "title": "Stavebnice",
                "price": "1299,50",
                "desire_level": 1,
                "purchase_url": "https://example.com/darek",
                "notes": "Modrá varianta",
            },
            actor,
        )

    def test_recipient_cannot_see_whether_wish_is_reserved(self):
        wish = self.create_wish()
        own = christmas.state("honzicek")["groups"][3]["wishes"][0]
        self.assertFalse(own["reservation_visible"])
        self.assertNotIn("reservation", own)
        self.assertNotIn("reserved_by", own)

        christmas.set_reservation(wish["id"], "tatinek", True)
        own_after = christmas.state("honzicek")["groups"][3]["wishes"][0]
        self.assertFalse(own_after["reservation_visible"])
        self.assertNotIn("reservation", own_after)
        self.assertNotIn("reserved_by", own_after)

        visible = christmas.state("maminka")["groups"][3]["wishes"][0]
        self.assertEqual(visible["reservation"]["reserved_by"], "tatinek")
        self.assertEqual(visible["reservation"]["reserved_by_name"], "Tatínek")

    def test_user_can_only_add_to_own_list(self):
        with self.assertRaises(PermissionError):
            self.create_wish("maminka", "honzicek")
        with self.assertRaises(PermissionError):
            self.create_wish("rodice", "maminka")
        with self.assertRaises(PermissionError):
            self.create_wish("babicka", "maminka")

    def test_only_own_group_allows_adding(self):
        state = christmas.state("honzicek")
        allowed = [group["key"] for group in state["groups"] if group["can_add"]]
        self.assertEqual(allowed, ["honzicek"])

    def test_user_cannot_move_wish_to_someone_elses_list(self):
        wish = self.create_wish()
        payload = {
            "recipient_key": "maminka",
            "title": "Přesunuté přání",
            "desire_level": 2,
        }
        with self.assertRaises(PermissionError):
            christmas.save_wish(payload, "honzicek", wish["id"])

    def test_only_one_person_can_reserve_a_wish(self):
        wish = self.create_wish()
        christmas.set_reservation(wish["id"], "tatinek", True)
        with self.assertRaises(ValueError):
            christmas.set_reservation(wish["id"], "maminka", True)
        with self.assertRaises(PermissionError):
            christmas.set_reservation(wish["id"], "maminka", False)

    def test_photo_is_normalized_and_removed_with_wish(self):
        wish = self.create_wish()
        source = BytesIO()
        Image.new("RGB", (400, 300), "#b33a36").save(source, "JPEG")
        saved = christmas.save_wish_image(
            wish["id"], "honzicek", "darek.jpg", source.getvalue()
        )
        filename = saved["image_url"].rsplit("/", 1)[-1]
        body, mime = christmas.read_image(filename)
        self.assertEqual(mime, "image/webp")
        self.assertTrue(body.startswith(b"RIFF"))
        self.assertTrue((Path(christmas.IMAGE_DIR) / filename).exists())

        christmas.delete_wish(wish["id"], "honzicek")
        self.assertFalse((Path(christmas.IMAGE_DIR) / filename).exists())


if __name__ == "__main__":
    unittest.main()

