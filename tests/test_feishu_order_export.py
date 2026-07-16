from __future__ import annotations

import unittest

from scripts.export_feishu_orders_readonly import normalize_cell


class FeishuOrderExportTests(unittest.TestCase):
    def test_single_select_cells_are_flattened_for_order_normalization(self) -> None:
        self.assertEqual(normalize_cell("refund_type", ["退"]), "退")
        self.assertEqual(normalize_cell("platform", ["微信"]), "微信")
        self.assertIsNone(normalize_cell("return_status", []))

    def test_non_select_arrays_are_preserved_and_multi_select_shape_fails_closed(self) -> None:
        self.assertEqual(normalize_cell("category", ["外套"]), ["外套"])
        with self.assertRaises(RuntimeError):
            normalize_cell("refund_type", ["退", "补"])


if __name__ == "__main__":
    unittest.main()
