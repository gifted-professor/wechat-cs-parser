from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dashboard_integration.install import HANDLER_LINE, NAV_LINK, REQUIRE_LINE, install, validate


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DashboardInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dashboard = Path(self.temp_dir.name)
        (self.dashboard / "server.js").write_text(
            "const zlib = require('zlib');\n"
            "async function handleApiRequest(req, res, requestUrl, urlPath) {\n"
            "  return false;\n"
            "}\n",
            encoding="utf-8",
        )
        (self.dashboard / "index.html").write_text(
            '<nav><button class="section-btn" data-section="ai">AI 工具</button></nav>',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_installer_copies_complete_panel_and_is_idempotent(self) -> None:
        validate(self.dashboard, PROJECT_ROOT)
        install(self.dashboard, PROJECT_ROOT)
        install(self.dashboard, PROJECT_ROOT)

        server = (self.dashboard / "server.js").read_text(encoding="utf-8")
        index = (self.dashboard / "index.html").read_text(encoding="utf-8")
        self.assertEqual(server.count(REQUIRE_LINE), 1)
        self.assertEqual(server.count(HANDLER_LINE), 1)
        self.assertEqual(index.count(NAV_LINK), 1)
        self.assertTrue((self.dashboard / "server.js.before-wechat-cs").is_file())
        self.assertTrue((self.dashboard / "index.html.before-wechat-cs").is_file())
        self.assertTrue((self.dashboard / "wechat_cs_proxy.js").is_file())

        panel = self.dashboard / "wechat-cs"
        self.assertEqual(
            {path.name for path in panel.iterdir()},
            {"index.html", "app.js", "styles.css", "favicon.svg"},
        )
        panel_html = (panel / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="styles.css"', panel_html)
        self.assertIn('src="app.js"', panel_html)


class DashboardActionQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (PROJECT_ROOT / "wechat_cs" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.javascript = (PROJECT_ROOT / "wechat_cs" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.proxy_source = (
            PROJECT_ROOT / "dashboard_integration" / "wechat_cs_proxy.js"
        ).read_text(encoding="utf-8")

    def test_today_action_is_first_and_uses_only_human_review_actions(self) -> None:
        first_nav = re.search(r'<button class="nav-item active" data-view="([^"]+)"', self.html)
        self.assertIsNotNone(first_nav)
        self.assertEqual(first_nav.group(1), "actions")
        self.assertIn('id="view-actions"', self.html)
        self.assertIn('value="aolai1"', self.html)
        self.assertIn('id="actionDate"', self.html)
        for lane in ("replyNowList", "proactiveList", "suppressedList"):
            self.assertIn(f'id="{lane}"', self.html)

        self.assertIn("/action-queue?profile=", self.javascript)
        self.assertIn("&date=", self.javascript)
        self.assertIn("&limit=20", self.javascript)
        self.assertIn("/action-queue/${encodeURIComponent(queueItem.action_id)}", self.javascript)
        self.assertIn("/action-queue/${encodeURIComponent(item.action_id)}/draft", self.javascript)
        self.assertIn("/action-queue/${encodeURIComponent(item.action_id)}/feedback", self.javascript)

        combined = self.html + self.javascript
        for label in ("查看详情", "生成回复建议", "复制建议", "编辑建议", "采纳", "拒绝"):
            self.assertIn(label, combined)
        self.assertIn("安全闸门已关闭", self.html)
        self.assertIn("联系前必须人工复核最新状态", self.html)
        self.assertIn("historical_snapshot_ready", self.javascript)
        self.assertIn("历史快照促单候选；实时回复已关闭", self.javascript)
        self.assertIn("联系前人工核对：新消息、刚下单、售后、拒绝联系", self.javascript)
        self.assertIn("系统没有生成替代建议", self.javascript)
        buttons = re.findall(r"<button\b[^>]*>.*?</button>", self.html, re.DOTALL)
        send_buttons = [button for button in buttons if "发送" in button]
        self.assertEqual(send_buttons, [])
        self.assertNotIn("phone_hmac", self.javascript)
        self.assertNotIn("raw_wechat", self.javascript)

    def test_sales_profile_pilot_has_review_only_two_column_workbench(self) -> None:
        self.assertIn('data-view="profiles"', self.html)
        self.assertIn('id="view-profiles"', self.html)
        self.assertIn("50 人画像验收", self.html)
        self.assertIn('id="salesProfileFilters"', self.html)
        self.assertIn('id="salesProfileStratum"', self.html)
        self.assertIn('id="salesProfileStatus"', self.html)
        self.assertIn('id="salesProfileList"', self.html)
        self.assertIn('id="salesProfileDetail"', self.html)
        self.assertIn('id="salesProfileReviewForm"', self.html)

        combined = self.html + self.javascript
        for label in (
            "客户价值",
            "商品偏好",
            "时间节律",
            "购买驱动力",
            "历史承诺",
            "当前机会",
            "建议联系理由",
            "自然开场",
            "风险",
            "未知项",
            "事实准确度",
            "洞察实用性",
            "销售真实感",
            "时机质量",
            "证据质量",
        ):
            self.assertIn(label, combined)
        self.assertIn("历史快照截止时间", self.html)
        self.assertIn("联系前核对最新状态", self.html)
        self.assertIn("<details", self.html)
        self.assertIn('value="approved"', self.html)
        self.assertIn('value="edited"', self.html)
        self.assertIn('value="rejected"', self.html)

        self.assertIn(
            "/sales-profile-pilot?run_id=latest&status=",
            self.javascript,
        )
        self.assertIn(
            "/sales-profile-pilot/${encodeURIComponent(salesProfileId)}",
            self.javascript,
        )
        self.assertIn(
            "/sales-profile-pilot/${encodeURIComponent(profile.sales_profile_id)}/review",
            self.javascript,
        )
        for contract_field in (
            "profile_json",
            "deterministic_facts_json",
            "accepted_events",
            "evidence_json",
            "card_version",
        ):
            self.assertIn(contract_field, self.javascript)
        for allowed_status in (
            "pending",
            "running",
            "succeeded",
            "failed",
            "reviewed",
            "unreviewed",
        ):
            self.assertIn(f'value="{allowed_status}"', self.html)

        buttons = re.findall(r"<button\b[^>]*>.*?</button>", self.html, re.DOTALL)
        self.assertFalse(any("发送" in button for button in buttons))
        self.assertNotIn("/sales-profile-pilot/run", self.javascript)
        self.assertNotIn("/sales-profile-pilot/send", self.javascript)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for proxy safety test")
    def test_proxy_hydrates_allowlist_and_strips_private_keys_recursively(self) -> None:
        proxy_path = PROJECT_ROOT / "dashboard_integration" / "wechat_cs_proxy.js"
        with tempfile.TemporaryDirectory() as temp_dir:
            private_map = Path(temp_dir) / "private-map.json"
            private_map.write_text(
                json.dumps(
                    {
                        "customer_aaaaaaaaaaaaaaaa": {
                            "display_name": "林女士",
                            "owner": "小雨",
                            "account_label": "aolai1",
                            "contact_hint": "微信会话置顶",
                            "phone": "13800138000",
                            "raw_wechat_id": "wx-private-map-value",
                            "extra": "must-not-hydrate",
                        },
                        "customer_cccccccccccccccc": {
                            "display_name": "联系 13800138000",
                            "owner": "wxid_private_owner",
                            "contact_hint": "phone_abcdef0123456789",
                        },
                        "customer_dddddddddddddddd": {
                            "contact_hint": "+86 138-0013-8000",
                        },
                        "customer_eeeeeeeeeeeeeeee": {
                            "contact_hint": "微信号 abc12345",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            script = """
const proxy = require(%s);
const payload = {
  status: 'ready',
  phone_hmac: 'private-phone-hmac',
  lanes: {
    reply_now: [{
      action_id: 'action_safe',
      customer_key: 'customer_aaaaaaaaaaaaaaaa',
      display_name: 'upstream-name-must-not-win',
      raw_wechat_id: 'upstream-raw-id',
      nested: { mobile: 'private-mobile', safe: 'kept' }
    }],
    proactive_today: [],
    suppressed: []
  }
};
process.stdout.write(JSON.stringify({
  hydrated: proxy.hydrateActionPayload(payload),
  anonymous: proxy.hydrateActionPayload({customer_key: 'customer_bbbbbbbbbbbbbbbb', display_name: 'upstream-private'}, null),
  unsafeMapped: proxy.hydrateActionPayload({customer_key: 'customer_cccccccccccccccc'}),
  formattedPhone: proxy.hydrateActionPayload({customer_key: 'customer_dddddddddddddddd'}),
  wechatIdentifier: proxy.hydrateActionPayload({customer_key: 'customer_eeeeeeeeeeeeeeee'}),
  routes: {
    list: proxy.isAllowedActionRoute('GET', '/action-queue'),
    detail: proxy.isAllowedActionRoute('GET', '/action-queue/action_abc123'),
    draft: proxy.isAllowedActionRoute('POST', '/action-queue/action_abc123/draft'),
    feedback: proxy.isAllowedActionRoute('POST', '/action-queue/action_abc123/feedback'),
    customers: proxy.isAllowedActionRoute('GET', '/customer-insights'),
    legacyDraft: proxy.isAllowedActionRoute('POST', '/drafts'),
    profileList: proxy.isAllowedActionRoute('GET', '/sales-profile-pilot'),
    profileDetail: proxy.isAllowedActionRoute('GET', '/sales-profile-pilot/sales_profile_abc123'),
    profileReview: proxy.isAllowedActionRoute('POST', '/sales-profile-pilot/sales_profile_abc123/review'),
    profileRunGet: proxy.isAllowedActionRoute('GET', '/sales-profile-pilot/run'),
    profileRunPost: proxy.isAllowedActionRoute('POST', '/sales-profile-pilot/run'),
    profileRunReview: proxy.isAllowedActionRoute('POST', '/sales-profile-pilot/run/review'),
    profileSendGet: proxy.isAllowedActionRoute('GET', '/sales-profile-pilot/send'),
    profileSend: proxy.isAllowedActionRoute('POST', '/sales-profile-pilot/sales_profile_abc123/send'),
    profileTrailingSlash: proxy.isAllowedActionRoute('GET', '/sales-profile-pilot/'),
    profileReviewExtra: proxy.isAllowedActionRoute('POST', '/sales-profile-pilot/sales_profile_abc123/review/extra'),
    profileWrongMethod: proxy.isAllowedActionRoute('POST', '/sales-profile-pilot/sales_profile_abc123')
  }
}));
""" % json.dumps(str(proxy_path))
            environment = dict(__import__("os").environ)
            environment["WECHAT_CS_PRIVATE_MAP_PATH"] = str(private_map)
            result = subprocess.run(
                ["node", "-e", script],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
        result_payload = json.loads(result.stdout)
        payload = result_payload["hydrated"]
        item = payload["lanes"]["reply_now"][0]
        self.assertEqual(
            {key: item[key] for key in ("display_name", "owner", "account_label", "contact_hint")},
            {
                "display_name": "林女士",
                "owner": "小雨",
                "account_label": "aolai1",
                "contact_hint": "微信会话置顶",
            },
        )
        self.assertEqual(item["nested"], {"safe": "kept"})
        serialized = json.dumps(payload, ensure_ascii=False)
        for private_value in (
            "private-phone-hmac",
            "upstream-raw-id",
            "private-mobile",
            "13800138000",
            "wx-private-map-value",
            "must-not-hydrate",
            "upstream-name-must-not-win",
        ):
            self.assertNotIn(private_value, serialized)
        self.assertEqual(
            result_payload["anonymous"],
            {"customer_key": "customer_bbbbbbbbbbbbbbbb", "display_name": "匿名客户"},
        )
        self.assertEqual(
            result_payload["unsafeMapped"],
            {"customer_key": "customer_cccccccccccccccc", "display_name": "匿名客户"},
        )
        self.assertEqual(
            result_payload["formattedPhone"],
            {"customer_key": "customer_dddddddddddddddd", "display_name": "匿名客户"},
        )
        self.assertEqual(
            result_payload["wechatIdentifier"],
            {"customer_key": "customer_eeeeeeeeeeeeeeee", "display_name": "匿名客户"},
        )
        self.assertEqual(
            result_payload["routes"],
            {
                "list": True,
                "detail": True,
                "draft": True,
                "feedback": True,
                "customers": False,
                "legacyDraft": False,
                "profileList": True,
                "profileDetail": True,
                "profileReview": True,
                "profileRunGet": False,
                "profileRunPost": False,
                "profileRunReview": False,
                "profileSendGet": False,
                "profileSend": False,
                "profileTrailingSlash": False,
                "profileReviewExtra": False,
                "profileWrongMethod": False,
            },
        )

        self.assertIn("SENSITIVE_VALUE_RE", self.proxy_source)

        self.assertIn("WECHAT_CS_PRIVATE_MAP_PATH", self.proxy_source)
        self.assertIn("PRIVATE_DISPLAY_FIELDS", self.proxy_source)
        self.assertIn("WECHAT_CS_DASHBOARD_TOKEN", self.proxy_source)
        self.assertNotIn("console.", self.proxy_source)


if __name__ == "__main__":
    unittest.main()
