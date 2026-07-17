# Unified Sales Profile Review Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the existing 8898 review portal show one row per unique customer across all completed sales-profile runs while preserving reviews on their original card versions.

**Architecture:** Add a read-only `run_id=all` scope inside the existing portal. The backend groups succeeded cards by `phone_hmac`, chooses a reviewed version first and otherwise the newest version, and returns all versions on the detail response. The frontend keeps one person in the list and exposes a version selector; review writes continue to target the selected `sales_profile_id` and never copy verdicts.

**Tech Stack:** Python standard-library HTTP server and SQLite, vanilla JavaScript/CSS, `unittest`.

---

### Task 1: Lock the person-level merge contract with tests

**Files:**
- Modify: `tests/test_review_portal.py`

**Step 1:** Add a second completed run containing one newer version of an existing phone and one new phone.

**Step 2:** Add an old-version review and assert `/api/summary` returns unique people, total card versions, batch count, and preserved reviewed-person count.

**Step 3:** Assert `/api/profiles?promotion=all` returns one row per phone, selects the reviewed version as canonical, and reports `version_count`.

**Step 4:** Assert profile detail returns every version and that writes remain attached to the explicitly selected card version.

**Step 5:** Run `python3 -m unittest tests.test_review_portal` and confirm the new tests fail before implementation.

### Task 2: Implement multi-run person aggregation

**Files:**
- Modify: `wechat_cs/review_portal.py`

**Step 1:** Add an `all` run scope without changing existing single-run behavior.

**Step 2:** Query succeeded versions across completed runs and attach review metadata, run metadata, and business state.

**Step 3:** Group by `phone_hmac`; select the most recently reviewed card first, otherwise the newest run/card.

**Step 4:** Make summary and list filters operate on canonical people while reporting both `total` people and `profile_versions`.

**Step 5:** Resolve detail, messages, and review writes by the requested card's own run. Add `versions` and `canonical_profile_id` to detail responses.

**Step 6:** Run focused portal tests until green, then run the full suite.

### Task 3: Expose versions in the existing UI

**Files:**
- Modify: `wechat_cs/review_portal_static/index.html`
- Modify: `wechat_cs/review_portal_static/app.js`
- Modify: `wechat_cs/review_portal_static/styles.css`
- Modify: `tests/test_review_portal.py`

**Step 1:** Update the workbench subtitle and run metadata to describe the unified customer view.

**Step 2:** Add a version selector to the detail header; hide it for single-version people.

**Step 3:** Keep the canonical person selected in the left list when another version is being viewed.

**Step 4:** Add static-asset assertions and run portal tests.

### Task 4: Deploy and verify 8898

**Files:**
- Modify: `README.md`

**Step 1:** Document `--run-id all` and the reviewed-version-first merge rule.

**Step 2:** Back up the active database and verify integrity.

**Step 3:** Restart only port 8898 from this worktree with `--run-id all`; leave 8899 unchanged.

**Step 4:** Verify live `/api/summary` reports 1,278 unique people, 1,280 card versions, 3 batches, and 11 reviewed people.

**Step 5:** Verify the old 11 review rows are byte-for-byte unchanged, review/send boundaries remain closed, static assets load, and the full test suite passes.

**Step 6:** Open the live page and visually verify list, summary, detail, and version switching before handoff.
