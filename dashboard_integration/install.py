#!/usr/bin/env python3
"""Install the WeChat CS proxy and static panel into an existing Dashboard.

This installer is intentionally small and idempotent. It never copies the raw
WeChat export or local SQLite database to the Dashboard host.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


REQUIRE_LINE = "const { handleWechatCsProxy } = require('./wechat_cs_proxy');"
HANDLER_LINE = "  if (await handleWechatCsProxy(req, res, requestUrl, urlPath)) return true;"
NAV_LINK = '<a class="section-btn" href="/wechat-cs/index.html">AI 客服</a>'


def validate(dashboard_dir: Path, project_root: Path) -> None:
    dashboard_dir = dashboard_dir.resolve()
    project_root = project_root.resolve()
    server_path = dashboard_dir / "server.js"
    index_path = dashboard_dir / "index.html"
    if not server_path.is_file():
        raise RuntimeError(f"server.js not found under {dashboard_dir}")
    if not index_path.is_file():
        raise RuntimeError(f"index.html not found under {dashboard_dir}")
    server_text = server_path.read_text(encoding="utf-8")
    if REQUIRE_LINE not in server_text and "const zlib = require('zlib');" not in server_text:
        raise RuntimeError("unsupported server.js: zlib require marker missing")
    if HANDLER_LINE not in server_text and "async function handleApiRequest(req, res, requestUrl, urlPath) {" not in server_text:
        raise RuntimeError("unsupported server.js: API handler marker missing")
    index_text = index_path.read_text(encoding="utf-8")
    if NAV_LINK not in index_text and '<button class="section-btn" data-section="ai">AI 工具</button>' not in index_text:
        raise RuntimeError("unsupported index.html: section nav marker missing")
    required = [
        project_root / "dashboard_integration" / "wechat_cs_proxy.js",
        *(project_root / "wechat_cs" / "static" / name for name in ("index.html", "app.js", "styles.css", "favicon.svg")),
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("missing deployment asset: " + ", ".join(missing))


def patch_server(server_path: Path) -> bool:
    text = server_path.read_text(encoding="utf-8")
    changed = False
    if REQUIRE_LINE not in text:
        marker = "const zlib = require('zlib');"
        if marker not in text:
            raise RuntimeError("unsupported server.js: zlib require marker missing")
        text = text.replace(marker, marker + "\n" + REQUIRE_LINE, 1)
        changed = True
    if HANDLER_LINE not in text:
        marker = "async function handleApiRequest(req, res, requestUrl, urlPath) {"
        if marker not in text:
            raise RuntimeError("unsupported server.js: API handler marker missing")
        text = text.replace(marker, marker + "\n" + HANDLER_LINE + "\n", 1)
        changed = True
    if changed:
        backup = server_path.with_suffix(".js.before-wechat-cs")
        if not backup.exists():
            shutil.copy2(server_path, backup)
        server_path.write_text(text, encoding="utf-8")
    return changed


def patch_index(index_path: Path) -> bool:
    text = index_path.read_text(encoding="utf-8")
    if NAV_LINK in text:
        return False
    marker = '<button class="section-btn" data-section="ai">AI 工具</button>'
    if marker not in text:
        raise RuntimeError("unsupported index.html: section nav marker missing")
    backup = index_path.with_suffix(".html.before-wechat-cs")
    if not backup.exists():
        shutil.copy2(index_path, backup)
    text = text.replace(marker, marker + "\n    " + NAV_LINK, 1)
    index_path.write_text(text, encoding="utf-8")
    return True


def install(dashboard_dir: Path, project_root: Path) -> None:
    dashboard_dir = dashboard_dir.resolve()
    project_root = project_root.resolve()
    validate(dashboard_dir, project_root)
    server_path = dashboard_dir / "server.js"

    shutil.copy2(project_root / "dashboard_integration" / "wechat_cs_proxy.js", dashboard_dir / "wechat_cs_proxy.js")
    target_static = dashboard_dir / "wechat-cs"
    target_static.mkdir(parents=True, exist_ok=True)
    source_static = project_root / "wechat_cs" / "static"
    for name in ("index.html", "app.js", "styles.css", "favicon.svg"):
        source = source_static / name
        if not source.is_file():
            raise RuntimeError(f"missing local dashboard asset: {source}")
        shutil.copy2(source, target_static / name)
    server_changed = patch_server(server_path)
    index_changed = patch_index(dashboard_dir / "index.html")
    print(f"installed dashboard panel: {target_static / 'index.html'}")
    print(f"server.js patched: {server_changed}")
    print(f"index.html patched: {index_changed}")
    print("configure WECHAT_CS_BASE_URL and WECHAT_CS_TOKEN before restart")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dashboard-dir", required=True, type=Path)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true", help="validate compatibility without writing")
    args = parser.parse_args()
    if args.check:
        validate(args.dashboard_dir, args.project_root)
        print("dashboard integration check passed")
    else:
        install(args.dashboard_dir, args.project_root)


if __name__ == "__main__":
    main()
