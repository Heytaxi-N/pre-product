import time
import unittest
from unittest.mock import patch

import pick_products


class AutoFieldTests(unittest.TestCase):
    def test_fill_missing_feishu_fields_only_updates_eligible_records(self):
        class FakeFeishu:
            def __init__(self):
                self.records = {
                    "rec_target": {"状态": "待编辑", "信息": "始祖鸟 男款冲锋衣"},
                    "rec_done": {"状态": "已完成", "信息": "男款冲锋衣"},
                    "rec_filled": {"状态": "待编辑", "信息": "女款冲锋衣", "分类": ["上装"]},
                }
                self.updates = []

            def get_field_options(self, _name):
                return {"品牌分类-始祖鸟"}

            def list_records(self):
                return [{"record_id": key, "fields": value.copy()}
                        for key, value in self.records.items()]

            def get_record(self, record_id):
                return self.records[record_id].copy()

            def update_record(self, record_id, fields):
                self.records[record_id].update(fields)
                self.updates.append((record_id, fields))

        feishu = FakeFeishu()
        fs_cfg = {"info_field": "信息", "category_field": "分类", "model_field": "型号"}
        with patch.object(
            pick_products,
            "build_auto_fields",
            return_value=({"分类": ["品牌分类-始祖鸟"], "型号": "尺码：M、L"}, [], None),
        ):
            result = pick_products.fill_missing_feishu_fields(feishu, fs_cfg, {})

        self.assertEqual({"rec_target": {"分类": ["品牌分类-始祖鸟"], "型号": "尺码：M、L"}},
                         dict(feishu.updates))
        self.assertEqual(1, result["updated"])
        self.assertEqual(3, result["scanned"])

    def test_cost_and_sale_price(self):
        self.assertEqual(40, pick_products.parse_cost_price("上新 💰40，尺码齐全"))
        self.assertEqual(35, pick_products.parse_cost_price("女款 P35"))
        self.assertEqual(35, pick_products.parse_cost_price("女款 p35"))
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

    def test_feishu_category_options_prefix_brand_children_and_dedupe(self):
        categories = [
            {"name": "未分类", "parent_id": 0},
            {"name": "店长推荐", "parent_id": 0},
            {"name": "【上装】短袖/打底/外套等", "parent_id": 0},
            {"name": "品牌分类", "parent_id": 0},
            {"name": "凯乐石", "parent_id": 1},
            {"name": "凯乐石", "parent_id": 1},
        ]
        self.assertEqual(
            ["店长推荐", "【上装】短袖/打底/外套等", "品牌分类-凯乐石"],
            pick_products.build_feishu_category_options(categories),
        )

    def test_category_kind_is_single_and_ignores_mentioned_outfit_items(self):
        categories = pick_products.load_weidian_categories()
        text = "鸟家男女同款连帽口袋防晒衣，揣进包里，搭配运动裤也好看"
        result = pick_products.match_weidian_categories(text, {}, categories)
        self.assertEqual(
            ["【上装】短袖/打底/外套等", "【男装】猛男点这里",
             "【女装】美女看这里", "始祖鸟"],
            result,
        )

    def test_category_brand_is_required_and_unknown_brand_uses_other_major_brand(self):
        categories = pick_products.load_weidian_categories()
        result = pick_products.match_weidian_categories(
            "男女同款轻量防晒外套", {}, categories
        )
        self.assertEqual(
            ["【上装】短袖/打底/外套等", "【男装】猛男点这里",
             "【女装】美女看这里", "其他大牌←戳"],
            result,
        )

    def test_category_recognizes_fjallraven_and_does_not_add_accessory(self):
        categories = pick_products.load_weidian_categories()
        result = pick_products.match_weidian_categories(
            "女神同款北极狐抓绒夹克，搭配裤子更好看", {}, categories
        )
        self.assertEqual(
            ["【上装】短袖/打底/外套等", "【女装】美女看这里", "北极狐FJALL"],
            result,
        )

    def test_ai_categories_keep_explicit_text_matches(self):
        categories = pick_products.load_weidian_categories()
        response = {"choices": [{"message": {"content": '{"categories":["其他大牌←戳"]}'}}]}
        with patch.object(pick_products, "http_post_json", return_value=response):
            result = pick_products.match_weidian_categories(
                "乐飞叶女士短款软壳外套", {"base_url": "https://example.com", "api_key": "key"}, categories
            )
        self.assertEqual(
            ["【上装】短袖/打底/外套等", "【女装】美女看这里", "其他大牌←戳"],
            result,
        )

    def test_model_matches_whitelist_and_base_color(self):
        models = pick_products.load_weidian_models()
        matched = pick_products.match_weidian_models(
            "颜色：玫瑰红、黑色，尺码2：m、l、xl", {}, models
        )
        self.assertEqual(
            [{"name": "颜色", "values": ["红色", "黑色"]},
             {"name": "尺码2", "values": ["M", "L", "XL"]}],
            matched,
        )
        self.assertEqual(
            "颜色：红色、黑色\n尺码2：M、L、XL",
            pick_products.format_model_field(matched),
        )

    def test_model_sizes_are_sorted_ascending(self):
        matched = pick_products.match_weidian_models(
            "尺码：L、S、XL、M", {}, pick_products.load_weidian_models()
        )
        self.assertEqual(
            "尺码2：S、M、L、XL",
            pick_products.format_model_field(matched),
        )

    def test_model_does_not_invent_values(self):
        self.assertEqual([], pick_products.match_weidian_models("荧光青、7XL", {},
                                                               pick_products.load_weidian_models()))

    def test_hat_without_explicit_size_gets_one_size(self):
        models = pick_products.load_weidian_models()
        self.assertEqual(
            "颜色：黑色\n尺码：均码",
            pick_products.format_model_field(
                pick_products.match_weidian_models("棒球帽 黑色", {}, models)
            ),
        )
        self.assertNotIn(
            "均码",
            pick_products.format_model_field(
                pick_products.match_weidian_models("棒球帽 黑色 帽围58cm", {}, models)
            ),
        )
        self.assertNotIn(
            "均码",
            pick_products.format_model_field(
                pick_products.match_weidian_models("连帽外套 黑色", {}, models)
            ),
        )

    def test_build_auto_fields_writes_model(self):
        fields, categories, cost = pick_products.build_auto_fields(
            "凯乐石 女款短袖 颜色：玫瑰红、黑色 尺码2：m、l、xl 💰40", {}, {}
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
        self.assertEqual(
            "颜色：红色、黑色\n尺码2：M、L、XL",
            fields["型号"],
        )

    def test_model_color_ignores_words_inside_copy(self):
        text = "深紫色、浅紫色，黄皮友好，衬肤显白，连帽软壳外套"
        matched = pick_products.match_weidian_models(
            text, {}, pick_products.load_weidian_models()
        )
        self.assertEqual(
            [{"name": "颜色", "values": ["紫色", "浅紫色"]}],
            matched,
        )

    def test_model_ai_cannot_add_unmentioned_colors(self):
        response = {
            "choices": [{"message": {"content":
                '{"models":[{"name":"颜色","values":["黄色","白色"]}]}'}}]
        }
        with patch.object(pick_products, "http_post_json", return_value=response):
            matched = pick_products.match_weidian_models(
                "颜色：浅紫色", {"base_url": "https://example.com", "api_key": "key"}
            )
        self.assertEqual([{"name": "颜色", "values": ["浅紫色"]}], matched)

    def test_build_auto_fields_filters_missing_feishu_options(self):
        fields, _, _ = pick_products.build_auto_fields(
            "始祖鸟 男款冲锋衣 💰40", {}, {}, {"品牌分类-始祖鸟"}
        )
        self.assertEqual(["品牌分类-始祖鸟"], fields["分类"])

    def test_mines_recent_feishu_copy_into_local_keywords(self):
        now = int(time.time() * 1000)
        categories = [{"name": "上装", "parent_id": 0}]
        records = [
            {"fields": {"创建时间": now, "信息": "轻量软壳冲锋衣", "分类": ["上装"]}},
            {"fields": {"创建时间": now, "信息": "黑色软壳外套",
                                                "分类": "【上装】短袖/打底/外套等、品牌分类-其他大牌←戳"}},
            {"fields": {"创建时间": now, "信息": "户外速干短裤", "分类": ["下装"]}},
            {"fields": {"创建时间": now, "信息": "黑色户外运动裤", "分类": ["下装"]}},
            {"fields": {"创建时间": now - 40 * 86400 * 1000,
                         "信息": "历史羽绒服", "分类": ["上装"]}},
        ]
        keywords = pick_products.build_local_category_keywords(
            records, categories, now=now / 1000
        )
        self.assertIn("软壳", keywords["上装"])
        self.assertNotIn("羽绒", keywords["上装"])
        self.assertNotIn("户外", keywords["上装"])
        self.assertNotIn("户外", keywords["下装"])


if __name__ == "__main__":
    unittest.main()
