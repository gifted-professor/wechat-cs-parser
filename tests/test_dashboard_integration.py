from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
