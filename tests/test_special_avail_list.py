"""Regression coverage for shared wholesaler records; no database required."""
import importlib.util
from pathlib import Path
import unittest

from bson import ObjectId
from mongoengine.errors import FieldDoesNotExist


# Load just this model; avoid unrelated application imports and configuration.
spec = importlib.util.spec_from_file_location(
    "special_avail_list_regression_model",
    Path(__file__).resolve().parents[1] / "models" / "special_avail_list.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
SpecialAvailList = module.SpecialAvailList


class SpecialAvailListCompatibilityTests(unittest.TestCase):
    def record(self, **extra):
        return {
            "_id": ObjectId(),
            "wholesaler_name": "Regression wholesaler",
            "sender_emails": ["avail@example.com"],
            "podio_item_ids": [123],
            "active": True,
            **extra,
        }

    def test_mongoose_version_record_loads_and_preserves_version(self):
        for version in (0, 3):
            with self.subTest(version=version):
                doc = SpecialAvailList._from_son(self.record(__v=version))
                doc.validate()
                self.assertEqual(doc.mongoose_version, version)
                self.assertEqual(doc.sender_emails, ["avail@example.com"])
                self.assertEqual(doc.podio_item_ids, [123])
                self.assertEqual(doc.to_mongo()["__v"], version)

    def test_legacy_record_without_version_stays_compatible(self):
        doc = SpecialAvailList._from_son(self.record())
        doc.validate()
        self.assertIsNone(doc.mongoose_version)
        self.assertNotIn("__v", doc.to_mongo())

    def test_other_unknown_fields_are_still_rejected(self):
        with self.assertRaises(FieldDoesNotExist):
            SpecialAvailList._from_son(self.record(unexpected_field=True))


if __name__ == "__main__":
    unittest.main()
