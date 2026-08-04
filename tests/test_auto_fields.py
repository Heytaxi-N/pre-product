import unittest

import pick_products


class AutoFieldTests(unittest.TestCase):
    def test_cost_and_sale_price(self):
        self.assertEqual(40, pick_products.parse_cost_price("上新 💰40，尺码齐全"))
        self.assertEqual(58, pick_products.calculate_sale_price(40))
        self.assertEqual(128, pick_products.calculate_sale_price(100))

    def test_category_fallback_matches_brand_and_type(self):
        categories = pick_products.load_weidian_categories()
        result = pick_products.match_weidian_categories(
            "始祖鸟 男款冲锋衣", {}, categories
        )
        self.assertEqual(
            ["【上装】短袖/打底/外套等", "【男装】猛男点这里", "始祖鸟"],
            result,
        )

    def test_build_auto_fields_omits_model(self):
        fields, categories, cost = pick_products.build_auto_fields(
            "凯乐石 女款短袖 💰40", {}, {}
        )
        self.assertEqual(
            ["【上装】短袖/打底/外套等", "【女装】美女看这里", "凯乐石"],
            categories,
        )
        self.assertEqual(
            ["【上装】短袖/打底/外套等", "【女装】美女看这里", "品牌分类-凯乐石"],
            fields["分类"],
        )
        self.assertEqual(40, cost)
        self.assertEqual(58, fields["售价"])
        self.assertNotIn("型号", fields)

    def test_build_auto_fields_filters_missing_feishu_options(self):
        fields, _, _ = pick_products.build_auto_fields(
            "始祖鸟 男款冲锋衣 💰40", {}, {}, {"品牌分类-始祖鸟"}
        )
        self.assertEqual(["品牌分类-始祖鸟"], fields["分类"])


if __name__ == "__main__":
    unittest.main()
