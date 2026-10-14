"""Routed retrieval (spec 記憶路由樹 v1.1): node membership, budgets, routing.

Everything runs in the closed fixture environment of tests/router_baseline.py
(never ~/.config or the live ledger), with extra records written before the
index is built. Budgets are measured the way spec 8.3 (b) defines them: each
`### <id>` block of an explain render, with its selected_because line, goes
to the node its `node=` names; the whole PROJECT LESSONS section goes to the
project.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import router_baseline  # noqa: E402
from agent_memory import core, router  # noqa: E402
from agent_memory.lexical_index import compute_routing, node_for  # noqa: E402
from agent_memory.storage import encode_record, load_router_settings, router_defaults  # noqa: E402

FIXED_TIME = "2026-10-09T20:21:55+00:00"
LONG = " ".join(["The sample was logged, sieved, weighed, and photographed before storage."] * 8)

# A synthetic question of the same shape: CJK words with the short options -a and -b.
Q1 = "想加一個指令：claude -a 用帳號 a 登入，claude -b 換成帳號 b，做得到嗎？"
Q2 = next(case["text"] for case in router_baseline.load_cases() if case["name"] == "long-chinese-noise")


def put(memory: Path, kind: str, record_id: str, *, scope: str = "global", tags: list[str] | None = None, **fields: Any) -> None:
    """Write one canonical record the way `memoryctl record` would."""
    payload: dict[str, Any] = {"id": record_id, "evidence": "Router test fixture."}
    if kind in {"preference", "fact", "task"}:
        payload["value"] = fields.pop("value")
    elif kind == "decision":
        payload.update(
            problem=fields.pop("problem", "Fixture problem."),
            decision=fields.pop("decision"),
            rationale=fields.pop("rationale", "Fixture rationale."),
            constraints=fields.pop("constraints", ["Fixture constraint."]),
            alternatives_rejected=["Fixture alternative."],
            consequences=["Fixture consequence."],
            revisit_when=["Fixture revisit condition."],
        )
    elif kind == "experience":
        outcome = fields.pop("outcome")
        payload.update(
            situation=fields.pop("situation", "Fixture situation."),
            expectation="Fixture expectation.",
            action="Fixture action.",
            observed_outcome=outcome,
            applicability="Fixture applicability.",
            decision_links=fields.pop("links", []),
        )
    elif kind == "failure":
        payload.update(
            symptom=fields.pop("symptom"),
            attempt="Fixture attempt.",
            root_cause="Fixture root cause.",
            verified_fix=fields.pop("fix", "Fixture fix."),
            retry_when=fields.pop("retry_when", "Fixture retry condition."),
        )
    metadata = {
        "schema_version": core.SCHEMA_VERSION,
        "id": record_id,
        "kind": kind,
        "status": "active",
        "scope": scope,
        "source": fields.pop("source", "locally-verified"),
        "priority": fields.pop("priority", "normal"),
        "tags": tags or [],
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
        "last_verified": "2026-10-10",
        "expires_at": fields.pop("expires_at", "2099-12-31" if kind == "fact" else None),
        "version": 1,
        "supersedes_event": None,
    }
    if kind == "experience":
        metadata["decision_links"] = payload["decision_links"]
    triggers = fields.pop("triggers", None)
    if triggers is not None:
        metadata["triggers"] = triggers
    for key in ("origin", "applies", "topic"):
        if key in fields:
            metadata[key] = fields.pop(key)
    assert not fields, fields
    path = memory / "records" / kind / f"{record_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encode_record(metadata, core._body_for(kind, payload)), encoding="utf-8")


def corpus(memory: Path) -> None:
    # Spec §0 question 2: knowledge/memory answers; a ci experience and a
    # flutter failure collide with its bigrams on the flat path only.
    put(memory, "experience", "experience.memory.tree_routing_probe", tags=["memory"],
        outcome="把 rag 檢索拆成樹狀節點以後，每個節點各自的 index 都比較準，資料越來越大也不會出問題。")
    put(memory, "experience", "experience.ci.bigram_collision", tags=["ci"],
        outcome="資料越來越大以後 workflow 的 index 會出問題，要拆成不同工具。")
    put(memory, "failure", "failure.flutter.asset_oom", symptom="資料越來越大時節點會出問題，rag 的 index 爆掉。")
    # Spec §0 question 1: only the facts node knows the answer.
    put(memory, "fact", "fact.claude_code.profile_dir_fixture", tags=["claude-code", "config"],
        value="CLAUDE_CONFIG_DIR 讓 claude 的全域設定與登入帳號放在指定目錄；claude -a、claude -b 各用一個目錄。")
    # 8.4 (f): a three-character topic word.
    put(memory, "experience", "experience.web.browser_settings", tags=["web"],
        outcome="Browser settings pages need the dev tools panel closed before screenshots.")
    # 8.3 (b) fixture A: thirty long project hits, no knowledge hit.
    for index in range(30):
        put(memory, "experience", f"experience.alpha.pumice_core_{index:02d}",
            outcome=f"Pumice drill core {index:02d} was catalogued. {LONG}")
    # 8.3 (b) fixture B: thirty long knowledge hits, no project hit.
    for index in range(30):
        put(memory, "experience", f"experience.web.scoria_vesicle_{index:02d}",
            outcome=f"Scoria vesicle web gallery {index:02d} was rebuilt. {LONG}")
    # 8.5 (e): a global record the token rule moved into the project and a
    # project-scoped one, matched by the same words.
    put(memory, "experience", "experience.alpha.deposit_one", outcome="A tephra ignimbrite deposit was mapped.")
    put(memory, "experience", "experience.vent.deposit_two", scope="project:alpha",
        outcome="A tephra ignimbrite deposit was mapped.")
    # 8.5 (f): a project decision pulls a knowledge experience as evidence.
    put(memory, "decision", "decision.alpha.lahar_policy", decision="Sound the lahar siren before the policy deadline.")
    put(memory, "experience", "experience.web.lahar_siren", outcome="The lahar siren web dashboard lagged the policy.",
        links=[{"id": "decision.alpha.lahar_policy", "effect": "supports"}])
    # 8.5 (g): a strong decision must not floor a weaker experience.
    put(memory, "decision", "decision.alpha.tuff_breccia", tags=["tuff", "breccia"],
        decision="Store tuff breccia cores upright.")
    put(memory, "experience", "experience.alpha.welded_note", outcome="Welded tuff breccia cracked when stored flat.")
    # Ruling 19 (3): an English-only fact keyed by its entity word alone, and
    # a knowledge record matched by that one word, which still needs two.
    put(memory, "fact", "fact.zirconia.kiln_profile", tags=["zirconia"], value="The kiln profile is stored in kilnctl.")
    put(memory, "experience", "experience.web.zirconia_glaze", outcome="Zirconia glaze note.")
    # Ruling 24: a knowledge experience links to a project decision; the
    # decision belongs to the project, never to knowledge.
    put(memory, "decision", "decision.alpha.fumarole_rule", decision="Log every fumarole vent reading.")
    put(memory, "experience", "experience.web.fumarole_probe", outcome="The fumarole web vent page lagged.",
        links=[{"id": "decision.alpha.fumarole_rule", "effect": "supports"}])
    # Ruling 26: a knowledge decision too long for the node, which pulls in
    # evidence, and a modest experience its score would floor out.
    put(memory, "decision", "decision.web.obsidian_huge_rule", tags=["obsidian", "glass", "flake"],
        decision="Obsidian glass flake handling. " + LONG + " " + LONG + " " + LONG)
    put(memory, "experience", "experience.web.huge_rule_evidence", outcome="Something unrelated happened.",
        links=[{"id": "decision.web.obsidian_huge_rule", "effect": "supports"}])
    put(memory, "experience", "experience.web.obsidian_note", outcome="Obsidian glass needs care on the web shelf.")
    # Ruling 25: more project decisions than half the project budget holds.
    for index in range(4):
        put(memory, "decision", f"decision.alpha.andesite_gate_{index}", decision=f"Andesite gate {index}. {LONG}")
    # Ruling 29: a failure whose id names the project is the project's.
    put(memory, "failure", "failure.alpha.core_tray_dropped", symptom="The core tray dropped during dacite logging.",
        fix="Strap the dacite core tray.")
    # Ruling 37: a decision with more than five constraints.
    put(memory, "decision", "decision.alpha.rhyolite_rules", decision="Rhyolite cores follow seven rules.",
        constraints=[f"Rhyolite rule {n}." for n in range(1, 8)])
    # Ruling 38: global failures whose id names a knowledge topic.
    put(memory, "failure", "failure.web.cache_header_stale", symptom="The web cache header stayed stale after a deploy.")
    put(memory, "failure", "failure.web.cache_header_twice", symptom="The web cache header was sent twice after a deploy.")
    # Ruling 41: a topic that exists only as global failures.
    put(memory, "failure", "failure.git.squash_lost_work", symptom="A squash with reset --soft lost the commit work.",
        retry_when="Before every reset --soft squash.")
    # Learned in project beta, applying everywhere (spec 學到的地方與適用範圍 v1).
    put(memory, "experience", "experience.beta.gate_printed_only", applies="global", topic="shell",
        origin="project:beta", outcome="The shell gate printed the free memory and ran anyway.")
    # Ruling 52 b: the shell question opens memory before shell; memory's
    # weaker failure fits the knowledge budget alone but not beside the
    # shell lesson, and must not take its place.
    put(memory, "failure", "failure.shell.gate_fixture_must_block",
        symptom="The free memory check printed and the shell gate ran anyway. " + LONG,
        fix="Make the gate exit non-zero and chain the run with &&.")
    put(memory, "failure", "failure.memory.capsule_overflowed",
        symptom="The memory capsule overflowed when free memory ran low. " + LONG + " " + LONG)
    # Ruling 52 b: inside one topic the failure takes its place among the
    # topic's knowledge by score; each pair fits the budget one at a time.
    put(memory, "decision", "decision.basalt.column_rule", tags=["basalt", "column", "cooling"],
        decision="Basalt column cooling joints are logged by hand. " + LONG + " " + LONG)
    put(memory, "failure", "failure.basalt.column_photo_lost", symptom="A basalt column photo was lost. " + LONG + " " + LONG)
    put(memory, "experience", "experience.gabbro.sample_layer_note", outcome="A gabbro sample layer was noted. " + LONG + " " + LONG)
    put(memory, "failure", "failure.gabbro.cumulate_sample_mislabeled", tags=["gabbro", "cumulate", "sample", "label"],
        symptom="The gabbro cumulate sample label was swapped. " + LONG)
    # Ruling 53 c: a failure learned in beta carries the origin line, about
    # ten tokens; the web record is sized so it fits beside the failure's
    # room only if that room is measured without the line.
    put(memory, "failure", "failure.tuffring.rim_survey_lost", origin="project:beta",
        symptom="The tuffring rim survey was lost. " + LONG)
    put(memory, "experience", "experience.web.ignimbrite_sheet_map", outcome="The ignimbrite sheet map was redrawn. " + "x" * 708)
    # Ruling 53 d: lapilli's failure must not use scree's knowledge item.
    put(memory, "experience", "experience.lapilli.bag_note_one", outcome="Lapilli bag note one.")
    put(memory, "experience", "experience.lapilli.bag_note_two", outcome="Lapilli bag note two.")
    put(memory, "failure", "failure.lapilli.bag_torn", symptom="The lapilli bag tore open.")
    put(memory, "experience", "experience.scree.slope_note", outcome="Scree slope note.")
    # Ruling 52 b: two failure-only topics whose failures tie on score; each
    # fits the knowledge budget alone but not both, and the smaller must win
    # the tie although its id sorts last.
    put(memory, "failure", "failure.aaatopic.big_lesson", symptom="Shared marker words. " + LONG + " " + LONG)
    put(memory, "failure", "failure.zzztopic.small_lesson", symptom="Shared marker words. " + LONG)
    # 8.7: an expired normal preference loads nowhere on the routed path.
    put(memory, "preference", "pref.fixture.expired_andesite", expires_at="2000-01-01",
        value="Andesite reports use the obsolete andesite template.")


def blocks(rendered: str) -> list[tuple[str, str, str]]:
    """(id, node, block text exactly as _take measured it) for each record."""
    out = []
    for match in re.finditer(r"^### (\S+)$", rendered, re.M):
        start = match.start()
        end = rendered.find("\n\n", start)
        text = rendered[start: end if end >= 0 else len(rendered)]
        node = re.search(r"; node=(\S+)$", text, re.M)
        out.append((match.group(1), node.group(1) if node else "", "\n" + text))
    return out


def lessons_text(rendered: str) -> str:
    start = rendered.find("\n## PROJECT LESSONS\n")
    if start < 0:
        return ""
    end = rendered.find("\n\n## ", start + 1)
    return rendered[start:end]


def routing_line(rendered: str) -> str:
    return next(line for line in rendered.splitlines() if line.startswith("Routing: "))


def source_ids(rendered: str) -> list[str]:
    tail = rendered.split("## SOURCE IDS", 1)[1]
    return [line[2:] for line in tail.splitlines() if line.startswith("- ")]


class NodeForTests(unittest.TestCase):
    """8.1 (a): membership from a fixed routing the test builds itself."""

    ROUTING = {
        "projects": {
            "ledger": ["ledger"],
            "my-app": ["myapp"],
            "big-web-2026": ["big_web"],
            "the-zoo": ["thezoo"],
        },
        "sources": {"ledger": "ledger", "orphan": None},
        "collisions": {},
    }

    def node(self, **metadata: Any) -> str:
        return node_for(metadata, json.loads(json.dumps(self.ROUTING)))

    def test_layers_and_projects(self) -> None:
        cases = [
            (dict(kind="experience", id="experience.ledger.margin_note", scope="global"), "project/ledger"),
            (dict(kind="decision", id="decision.ledger.group_rule", scope="workspace:desktop"), "project/ledger"),
            # Ruling 29: a failure whose id names a project is that project's.
            (dict(kind="failure", id="failure.ledger.stale_quote", scope="global"), "project/ledger/failures"),
            (dict(kind="failure", id="failure.git.reset_soft", scope="global"), "failures"),
            (dict(kind="fact", id="fact.ledger.broker", scope="global"), "facts"),
            (dict(kind="preference", id="pref.ledger.style", scope="project:ledger"), "rules"),
            (
                dict(kind="lesson", id="lesson.ledger.recheck", scope="global", origin={"source": "ledger"}),
                "project/ledger/sources/lessons",
            ),
            (dict(kind="decision", id="federation.ledger.adr-1", scope="global", tags=["federation:ledger"]), "project/ledger/sources"),
            (dict(kind="note", id="federation.ledger.note-1", scope="global", tags=["federation:ledger"]), "project/ledger/sources"),
            (dict(kind="lesson", id="federation.ledger.group-2", scope="global", tags=["federation:ledger"]), "project/ledger/sources/lessons"),
            (dict(kind="experience", id="experience.tooling.tmux_keys", scope="workspace:desktop"), "knowledge/tooling"),
            (dict(kind="experience", id="experience.myapp.turn_order", scope="global"), "project/my-app"),
            (dict(kind="experience", id="experience.big_web.deploy", scope="global"), "project/big-web-2026"),
            (dict(kind="experience", id="experience.flutter.oom", scope="project:the-zoo"), "project/the-zoo"),
            (dict(kind="experience", id="experience.flutter.hot_reload", scope="global"), "knowledge/flutter"),
            (dict(kind="lesson", id="lesson.thezoo.feed", scope="project:the-zoo"), "project/the-zoo/sources/lessons"),
            (dict(kind="task", id="task.anything.capsule", scope="global"), "history"),
        ]
        for metadata, expected in cases:
            with self.subTest(record=metadata["id"]):
                self.assertEqual(expected, self.node(**metadata))

    def test_odd_records_still_get_a_node(self) -> None:
        for metadata in (
            dict(kind="experience", id="loner", scope="global"),
            dict(kind="decision", id="federation.orphan.adr-1", scope="global", tags=["federation:orphan"]),
            dict(kind="lesson", id="lesson.orphan.x", scope="global", origin={"source": "orphan"}),
            dict(kind="experience"),
            dict(),
        ):
            with self.subTest(metadata=metadata):
                self.assertTrue(self.node(**metadata))
        self.assertEqual("knowledge/misc", self.node(kind="experience", id="loner", scope="global"))
        self.assertEqual(
            "knowledge/orphan",
            self.node(kind="decision", id="federation.orphan.adr-1", scope="global", tags=["federation:orphan"]),
        )


class LearningFieldTests(unittest.TestCase):
    """Spec 學到的地方與適用範圍 v1: origin, applies, topic."""

    ROUTING = {"projects": {"the-zoo": ["thezoo"], "my-app": ["myapp"]}, "sources": {}, "collisions": {}}

    def node(self, **metadata: Any) -> str:
        return node_for(metadata, json.loads(json.dumps(self.ROUTING)))

    def test_applies_routes_before_the_naming_rules(self) -> None:
        cases = [
            (dict(kind="failure", id="failure.thezoo.gate_printed", scope="project:the-zoo", applies="global"), "failures"),
            (dict(kind="experience", id="experience.thezoo.ci_wait", scope="global", applies="global", topic="ci"), "knowledge/ci"),
            (dict(kind="experience", id="experience.thezoo.ci_wait", scope="global", applies="global"), "knowledge/misc"),
            (dict(kind="failure", id="failure.git.squash", scope="global", applies="origin", origin="project:my-app"), "project/my-app/failures"),
            (dict(kind="decision", id="decision.web.layout", scope="global", applies="origin", origin=["project:the-zoo", "project:my-app"]), "project/the-zoo"),
            # machine scope stays a hard filter: applies does not move it.
            (dict(kind="failure", id="failure.machine.gate", scope="machine:old", applies="origin"), "failures"),
            # applies = origin with no project origin falls back to the old rules.
            (dict(kind="failure", id="failure.thezoo.x", scope="global", applies="origin"), "project/the-zoo/failures"),
        ]
        for metadata, expected in cases:
            with self.subTest(record=metadata):
                self.assertEqual(expected, self.node(**metadata))

    def test_without_the_fields_nothing_moves(self) -> None:
        for metadata, expected in (
            (dict(kind="failure", id="failure.thezoo.gate", scope="global"), "project/the-zoo/failures"),
            (dict(kind="failure", id="failure.git.squash", scope="global"), "failures"),
            (dict(kind="experience", id="experience.ci.wait", scope="global"), "knowledge/ci"),
        ):
            self.assertEqual(expected, self.node(**metadata))

    def test_derived_origins_and_topics(self) -> None:
        from agent_memory.lexical_index import record_origins, record_topic

        routing = json.loads(json.dumps(self.ROUTING))
        self.assertEqual(["project:the-zoo"], record_origins({"id": "failure.thezoo.x", "scope": "global"}, routing))
        self.assertEqual(["machine:old"], record_origins({"id": "failure.machine.x", "scope": "machine:old"}, routing))
        self.assertEqual(["global"], record_origins({"id": "failure.git.x", "scope": "global"}, routing))
        self.assertEqual(["project:a", "project:b"], record_origins({"id": "failure.git.x", "origin": ["project:a", "project:b"]}, routing))
        self.assertEqual("git", record_topic({"id": "failure.git.x"}, routing))
        self.assertIsNone(record_topic({"id": "failure.thezoo.x"}, routing))
        self.assertEqual("shell", record_topic({"id": "failure.thezoo.x", "topic": "shell"}, routing))

    def test_validation(self) -> None:
        from agent_memory.lexical_index import learning_field_problems

        routing = json.loads(json.dumps(self.ROUTING))
        ok = {"id": "failure.shell.x", "scope": "global", "origin": ["project:the-zoo", "machine:old"], "applies": "global", "topic": "shell"}
        self.assertEqual([], learning_field_problems("failure", ok, routing))
        for bad in (
            {**ok, "origin": "zoo"}, {**ok, "origin": []}, {**ok, "origin": ["global", "global"]},
            {**ok, "applies": "everywhere"}, {**ok, "scope": "machine:old"}, {**ok, "topic": "Shell"},
            {**ok, "topic": "thezoo"},
        ):
            with self.subTest(bad=bad):
                self.assertTrue(learning_field_problems("failure", bad, routing))
        self.assertTrue(learning_field_problems("fact", {"id": "fact.x.y", "topic": "x"}, routing))
        self.assertEqual([], learning_field_problems("lesson", {"id": "lesson.x.y", "origin": {"source": "s"}}, routing))
        self.assertTrue(learning_field_problems("lesson", {"id": "lesson.x.y", "origin": "project:the-zoo"}, routing))

    def test_record_and_supersede_keep_the_fields(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            payload = {
                "id": "failure.shell.check_blocks", "scope": "global", "source": "locally-verified", "evidence": "Test.",
                "symptom": "s.", "attempt": "a.", "root_cause": "r.", "verified_fix": "f.", "retry_when": "w.",
                "origin": ["project:alpha", "project:beta"], "applies": "global", "topic": "shell",
            }
            core.record(root, "failure", payload)
            metadata, _ = core.decode_record(root / "records" / "failure" / "failure.shell.check_blocks.md")
            self.assertEqual((["project:alpha", "project:beta"], "global", "shell"),
                             (metadata["origin"], metadata["applies"], metadata["topic"]))
            core.supersede(root, "failure.shell.check_blocks", {
                **payload, "applies": "origin", "origin": "project:alpha",
                "change_driver": "new-evidence", "change_reason": "Only alpha.",
            })
            metadata, _ = core.decode_record(root / "records" / "failure" / "failure.shell.check_blocks.md")
            self.assertEqual(("project:alpha", "origin"), (metadata["origin"], metadata["applies"]))
            self.assertEqual(0, len(core.validate(root)[1]))
            with self.assertRaises(core.MemoryError):
                core.record(root, "failure", {**payload, "id": "failure.shell.bad_origin", "origin": "nowhere"})

    def test_applies_origin_needs_a_project_routing_knows(self) -> None:
        # Ruling 52 f: project:alpah (a typo of alpha) would load nowhere.
        with router_baseline.environment() as paths:
            root = paths["memory"]
            payload = {
                "id": "failure.shell.typo_origin", "scope": "global", "source": "locally-verified", "evidence": "Test.",
                "symptom": "s.", "attempt": "a.", "root_cause": "r.", "verified_fix": "f.", "retry_when": "w.",
                "origin": ["project:alpha", "project:alpah"], "applies": "origin",
            }
            with self.assertRaisesRegex(core.MemoryError, "alpah"):
                core.record(root, "failure", payload)
            core.record(root, "failure", {**payload, "applies": "global"})
            with self.assertRaisesRegex(core.MemoryError, "alpah"):
                core.supersede(root, "failure.shell.typo_origin", {
                    **payload, "change_driver": "new-evidence", "change_reason": "Only there.",
                })
            core.supersede(root, "failure.shell.typo_origin", {
                **payload, "origin": "project:alpha", "change_driver": "new-evidence", "change_reason": "Only alpha.",
            })
            self.assertEqual([], core.check_origin_projects(root))
            # One already in the ledger (written before the rule) is a warning.
            path = root / "records" / "failure" / "failure.shell.typo_origin.md"
            path.write_text(path.read_text(encoding="utf-8").replace('"project:alpha"', '"project:alpah"'), encoding="utf-8")
            self.assertEqual(
                ["failure.shell.typo_origin: applies = origin but origin project alpah is not a project routing knows"],
                core.check_origin_projects(root),
            )


class ComputeRoutingTests(unittest.TestCase):
    """Tokens come from configuration, never from the records."""

    def routing(self, router_toml: str | None, scopes: list[str]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            if router_toml is not None:
                (Path(tmp) / "router.toml").write_text(router_toml, encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_MEMORY_CONFIG_HOME": tmp}):
                return compute_routing(scopes)

    def test_a_project_scoped_record_does_not_make_its_topic_a_token(self) -> None:
        routing = self.routing('[project_tokens]\n"the-zoo" = ["thezoo"]\n', ["project:the-zoo"])
        self.assertIn("thezoo", routing["projects"]["the-zoo"])
        self.assertNotIn("flutter", routing["projects"]["the-zoo"])
        self.assertEqual(
            "knowledge/flutter",
            node_for({"kind": "experience", "id": "experience.flutter.hot_reload", "scope": "global"}, routing),
        )

    def test_a_shared_token_belongs_to_neither_project(self) -> None:
        routing = self.routing(
            '[project_tokens]\n"the-zoo" = ["flutter"]\n"my-app" = ["flutter"]\n',
            ["project:the-zoo", "project:my-app"],
        )
        self.assertEqual({"flutter": ["my-app", "the-zoo"]}, routing["collisions"])
        self.assertNotIn("flutter", routing["projects"]["the-zoo"])
        self.assertNotIn("flutter", routing["projects"]["my-app"])
        self.assertEqual(
            "knowledge/flutter",
            node_for({"kind": "experience", "id": "experience.flutter.hot_reload", "scope": "global"}, routing),
        )

    def test_configured_tokens_and_no_built_in_projects(self) -> None:
        bare = self.routing(None, ["project:my-app", "project:big-web-2026", "project:ledger"])
        self.assertEqual([], bare["projects"]["my-app"])
        self.assertEqual([], bare["projects"]["big-web-2026"])
        self.assertEqual(["ledger"], bare["projects"]["ledger"])
        routing = self.routing(
            '[project_tokens]\n"my-app" = ["myapp"]\n"big-web-2026" = ["big_web"]\n',
            ["project:my-app", "project:big-web-2026", "workspace:desktop"],
        )
        self.assertIn("myapp", routing["projects"]["my-app"])
        self.assertIn("big_web", routing["projects"]["big-web-2026"])
        self.assertNotIn("workspace:desktop", routing["projects"])


class RouterSettingsTests(unittest.TestCase):
    """8.3 (f) and ruling 14: router.toml validation and its health key."""

    def settings(self, text: str | None) -> tuple[dict[str, Any], str, str | None]:
        with tempfile.TemporaryDirectory() as tmp:
            if text is not None:
                (Path(tmp) / "router.toml").write_text(text, encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_MEMORY_CONFIG_HOME": tmp}):
                return core.router_settings()

    def test_the_default_budgets_fit_both_agents(self) -> None:
        self.assertEqual([], core.router_budget_problems(router_defaults()["budgets"]))
        for agent, reserve in core.ROUTER_FIXED_RESERVE.items():
            budgets = router_defaults()["budgets"][agent]
            total = sum(budgets[node] for node in core.ROUTER_CONTENT_NODES) + reserve
            self.assertEqual(core.AGENT_HOOK_BUDGETS[agent][1], total)
        settings, status, code = self.settings(None)
        self.assertEqual(("ok", None), (status, code))
        self.assertEqual(router_defaults()["budgets"], settings["budgets"])

    def test_the_example_file_is_the_defaults(self) -> None:
        example = Path(__file__).resolve().parents[1] / "docs" / "router.example.toml"
        settings, defaults = load_router_settings(example), router_defaults()
        self.assertEqual((None, []), (settings["code"], settings["problems"]))
        for key in ("enabled", "knowledge_max_topics", "budgets", "project_tokens", "cwd_topics", "topics", "speed_bump_enabled"):
            self.assertEqual(defaults[key], settings[key], key)
        # The example spells out the built-in speed bumps, each compiling.
        from agent_memory.storage import DEFAULT_SPEED_BUMPS, check_speed_bumps

        usable, problems = check_speed_bumps(settings["speed_bumps"])
        self.assertEqual([], problems)
        keys = ("id", "pattern", "reason", "failure_ids", "ignore_case")
        self.assertEqual(
            [{k: bump.get(k) for k in keys} for bump in check_speed_bumps(DEFAULT_SPEED_BUMPS)[0]],
            [{k: bump.get(k) for k in keys} for bump in usable],
        )

    def test_an_oversized_budget_falls_back_to_the_defaults(self) -> None:
        settings, status, code = self.settings("[budgets.codex]\nproject = 1500\n")
        self.assertEqual(("error", "router-budget-invalid"), (status, code))
        self.assertEqual(router_defaults()["budgets"], settings["budgets"])

    def test_a_small_project_budget_is_invalid(self) -> None:
        _, status, code = self.settings("[budgets.claude]\nproject = 300\n")
        self.assertEqual(("error", "router-budget-invalid"), (status, code))

    def test_a_bad_section_falls_back_alone(self) -> None:
        settings, status, code = self.settings('[budgets.claude]\nrules = 150\n\n[budgets.codex]\nrules = "x"\n')
        self.assertEqual(("ok", "router-toml-invalid"), (status, code))
        self.assertEqual(150, settings["budgets"]["claude"]["rules"])
        self.assertEqual(router_defaults()["budgets"]["codex"], settings["budgets"]["codex"])
        self.assertTrue(settings["enabled"])
        settings, status, code = self.settings('enabled = false\n\n[budgets.codex]\nrules = "x"\n')
        self.assertFalse(settings["enabled"])
        self.assertEqual(("ok", "disabled,router-toml-invalid"), (status, code))
        settings, _, code = self.settings("enabled = false\n\n[budgets.codex\n")
        self.assertFalse(settings["enabled"])
        self.assertEqual("disabled,router-toml-invalid", code)
        settings, _, code = self.settings("knowledge_max_topics = 3\n")
        self.assertEqual((2, "router-toml-invalid"), (settings["knowledge_max_topics"], code))

    def test_disabled_and_unusable_files(self) -> None:
        self.assertEqual(("ok", "disabled"), self.settings("enabled = false\n")[1:])
        settings, status, code = self.settings("enabled = [\n")
        self.assertEqual(("ok", "router-toml-invalid"), (status, code))
        self.assertTrue(settings["enabled"])
        settings, status, code = self.settings("enable = false\n")
        self.assertEqual(("ok", "router-toml-invalid"), (status, code))
        self.assertTrue(settings["enabled"])


class RoutedRenderTests(unittest.TestCase):
    """The routed pack on the fixture corpus, router enabled by default."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._env = router_baseline.environment(extra_records=corpus)
        cls.paths = cls._env.__enter__()
        cls.root = cls.paths["memory"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._env.__exit__(None, None, None)

    def render(self, query: str, where: str = "neutral", agent: str | None = "claude", **kwargs: Any) -> str:
        budget = core.AGENT_HOOK_BUDGETS[agent][1] if agent else 3000
        return core._render_context(
            self.root, self.paths[where], query, budget, explain=True, allow_reindex=False, agent=agent, **kwargs
        )

    def hook(self, query: str, where: str = "neutral", agent: str = "claude") -> dict[str, Any]:
        event = {
            "session_id": "router-test",
            "cwd": str(self.paths[where]),
            "hook_event_name": "UserPromptSubmit",
            "prompt": query,
        }
        return core.handle_hook(self.root, "user-prompt-submit", event, agent=agent)

    def health(self) -> dict[str, Any]:
        return json.loads((self.paths["config"] / "runtime" / "health.json").read_text(encoding="utf-8"))["hooks"]

    # --- membership on the built index -------------------------------------

    def test_every_index_row_has_exactly_one_node(self) -> None:
        connection = core._open_index(self.root, allow_reindex=False)
        try:
            rows = connection.execute("SELECT record_id, node FROM records").fetchall()
        finally:
            connection.close()
        self.assertTrue(rows)
        self.assertEqual([], [str(row[0]) for row in rows if not str(row[1])])
        canonical = {
            path.stem
            for path in (self.root / "records").glob("*/*.md")
            if core.decode_record(path)[0].get("status") == "active"
        }
        federated = {str(row[0]) for row in rows if str(row[0]).startswith("federation.")}
        self.assertTrue(federated)
        self.assertEqual(len(rows), len(canonical) + len(federated))
        self.assertEqual(canonical | federated, {str(row[0]) for row in rows})

    def test_reindex_reports_routing(self) -> None:
        from agent_memory.lexical_index import routing_report

        lines = routing_report(self.root)
        self.assertIn("  project/alpha: ", "\n".join(lines))
        self.assertIn("  alpha: alpha", lines)
        self.assertTrue(any(line.startswith("Fact entities: ") for line in lines))
        self.assertIn("  web: 38, 6", lines)

    # --- 8.3 (b): node budgets ---------------------------------------------

    def tally(self, rendered: str, agent: str) -> dict[str, dict[str, int]]:
        main = routing_line(rendered).split("project=", 1)[1].split(";", 1)[0]
        second = routing_line(rendered).split("second=", 1)[1].split(";", 1)[0]
        groups: dict[str, dict[str, int]] = {}

        def add(group: str, tokens: int) -> None:
            entry = groups.setdefault(group, {"tokens": 0, "items": 0})
            entry["tokens"] += tokens
            entry["items"] += 1

        for record_id, node, text in blocks(rendered):
            if node == "failures":
                group = "project" if main != "-" else "knowledge"
            elif node.startswith(f"project/{main}") and main != "-":
                group = "project"
            elif node.startswith(f"project/{second}") and second != "-":
                group = "project_second"
            elif node.startswith("knowledge/"):
                group = "knowledge"
            else:
                group = node
            add(group, core.estimate_tokens(text))
        lessons = lessons_text(rendered)
        if lessons:
            groups.setdefault("project", {"tokens": 0, "items": 0})["tokens"] += core.estimate_tokens(lessons)
        return groups

    def test_fixture_a_project_borrows_only_what_knowledge_left(self) -> None:
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                rendered = self.render("pumice drill core catalogued", "alpha", agent)
                budgets = router_defaults()["budgets"][agent]
                tally = self.tally(rendered, agent)
                self.assertNotIn("knowledge", tally)
                self.assertGreater(tally["project"]["tokens"], budgets["project"])
                self.assertLessEqual(tally["project"]["tokens"], budgets["project"] + budgets["project_borrow_max"])
                self.assertLessEqual(tally["project"]["items"], core.ROUTER_NODE_MAX_ITEMS[agent]["project"])
                self.assertLessEqual(core.estimate_tokens(rendered), core.AGENT_HOOK_BUDGETS[agent][1])

    def test_fixture_b_knowledge_never_borrows(self) -> None:
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                rendered = self.render("scoria vesicle web gallery rebuilt", "alpha", agent)
                budgets = router_defaults()["budgets"][agent]
                tally = self.tally(rendered, agent)
                self.assertIn("knowledge=web", routing_line(rendered))
                self.assertGreaterEqual(tally["knowledge"]["items"], 1)
                self.assertLessEqual(tally["knowledge"]["tokens"], budgets["knowledge"])
                self.assertLessEqual(tally["knowledge"]["items"], core.ROUTER_NODE_MAX_ITEMS[agent]["knowledge"])
                self.assertLessEqual(tally.get("project", {"tokens": 0})["tokens"], budgets["project"])
                self.assertLessEqual(core.estimate_tokens(rendered), core.AGENT_HOOK_BUDGETS[agent][1])

    # --- 8.3 (e): one failing node leaves the others -------------------------

    def test_a_failing_node_drops_only_its_own_section(self) -> None:
        with mock.patch.object(router, "knowledge_candidates", side_effect=RuntimeError("boom")):
            output = self.hook("lahar siren web policy deadline", "alpha")
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("### decision.alpha.lahar_policy", context)
        self.assertIn("## SOURCE IDS", context)
        state = self.health()["user_prompt_submit"]
        self.assertEqual("ok", state["status"])
        self.assertIn("node-failed:knowledge/web", state["code"])

    # --- 8.3 (f): an invalid budget is reported and the defaults answer -------

    def test_an_invalid_budget_file_renders_like_the_defaults(self) -> None:
        query = "lahar siren web policy deadline"
        expected = self.hook(query, "alpha", "codex")
        self.assertEqual("ok", self.health()["router"]["status"])
        target = self.paths["config"] / "router.toml"
        target.write_text("[budgets.codex]\nproject = 1500\n", encoding="utf-8")
        try:
            actual = self.hook(query, "alpha", "codex")
            state = self.health()["router"]
        finally:
            target.unlink()
        self.assertEqual(("error", "router-budget-invalid"), (state["status"], state.get("code")))
        self.assertEqual(expected, actual)

    # --- 8.4: knowledge topics ---------------------------------------------

    def test_question_two_opens_only_knowledge_memory(self) -> None:
        rendered = self.render(Q2)
        self.assertIn("project=-; second=-; knowledge=memory; facts=closed", routing_line(rendered))
        self.assertEqual({"knowledge/memory"}, {node for _, node, _ in blocks(rendered)})
        self.assertIn("### experience.memory.tree_routing_probe", rendered)
        for prefix in ("failure.flutter.", "experience.ci.", "failure.ci.", "fact."):
            self.assertNotIn(f"### {prefix}", rendered)
        # The same question on the flat path is the noise the router removes.
        flat, _ = core._render_context_status(
            self.root, self.paths["neutral"], Q2, 3000, True, allow_reindex=False,
            settings={**router_defaults(), "enabled": False},
        )
        self.assertIn("### experience.ci.bigram_collision", flat)

    def test_a_three_character_topic_word_opens_its_topic(self) -> None:
        self.assertIn("knowledge=web;", routing_line(self.render("請幫我看瀏覽器的設定")))
        self.assertIn("knowledge=-;", routing_line(self.render("分數怎麼算")))

    def test_the_cwd_topic_takes_a_seat(self) -> None:
        settings = {**router_defaults(), "cwd_topics": {str(self.paths["neutral"]): "memory"}}
        turn = router._Turn(self.root, self.paths["neutral"], "請幫我看瀏覽器的設定", False)
        try:
            self.assertEqual(["memory", "web"], router.choose_topics(turn, settings))
            self.assertEqual(["memory"], router.choose_topics(turn, {**settings, "knowledge_max_topics": 1}))
        finally:
            turn.close()

    # --- 8.5: scores inside a node -----------------------------------------

    def scores(self, rendered: str) -> dict[str, int]:
        return {
            record_id: int(re.search(r"score=(\d+)", text).group(1))
            for record_id, _, text in blocks(rendered)
        }

    def test_global_and_project_scoped_records_score_alike_in_a_node(self) -> None:
        rendered = self.render("tephra ignimbrite deposit", "alpha")
        found = {record_id: node for record_id, node, _ in blocks(rendered)}
        self.assertEqual("project/alpha", found["experience.alpha.deposit_one"])
        self.assertEqual("project/alpha", found["experience.vent.deposit_two"])
        scores = self.scores(rendered)
        self.assertEqual(scores["experience.alpha.deposit_one"], scores["experience.vent.deposit_two"])

    def test_pulled_evidence_belongs_to_the_node_that_pulled_it(self) -> None:
        rendered = self.render("lahar siren web policy deadline", "alpha")
        self.assertIn("knowledge=web", routing_line(rendered))
        found = [(record_id, node) for record_id, node, _ in blocks(rendered)]
        self.assertIn(("decision.alpha.lahar_policy", "project/alpha"), found)
        self.assertIn(("experience.web.lahar_siren", "project/alpha"), found)
        self.assertEqual(1, [record_id for record_id, _ in found].count("experience.web.lahar_siren"))
        self.assertEqual(1, source_ids(rendered).count("experience.web.lahar_siren"))

    def test_each_project_subindex_floors_against_its_own_top(self) -> None:
        rendered = self.render("tuff breccia cores", "alpha")
        scores = self.scores(rendered)
        decision, experience = scores["decision.alpha.tuff_breccia"], scores["experience.alpha.welded_note"]
        self.assertLess(experience, core.RELEVANCE_FLOOR * decision)

    # --- Phase 1.2 (rulings 24-30) --------------------------------------------

    def test_knowledge_never_holds_a_project_member(self) -> None:
        rendered = self.render("fumarole web vent reading", "alpha")
        self.assertIn("knowledge=web", routing_line(rendered))
        found = dict((record_id, node) for record_id, node, _ in blocks(rendered))
        self.assertEqual("project/alpha", found.get("decision.alpha.fumarole_rule"))
        self.assertNotIn("knowledge/web", [n for i, n, _ in blocks(rendered) if i == "decision.alpha.fumarole_rule"])

    def test_pulled_evidence_is_a_pointer_and_decisions_are_slim(self) -> None:
        rendered = self.render("lahar siren web policy deadline", "alpha")
        pointer = next(text for record_id, _, text in blocks(rendered) if record_id == "experience.web.lahar_siren")
        self.assertIn("- kind: experience（證據鏈，來自 decision.alpha.lahar_policy）", pointer)
        self.assertIn("- read: memoryctl search --id experience.web.lahar_siren", pointer)
        self.assertNotIn("- applicability:", pointer)
        without_explain = "\n".join(line for line in pointer.splitlines() if not line.startswith("- selected_because:"))
        self.assertLessEqual(core.estimate_tokens(without_explain), 120)
        decision = next(text for record_id, _, text in blocks(rendered) if record_id == "decision.alpha.lahar_policy")
        self.assertIn("- constraints:", decision)
        self.assertIn("- evidence_counts: supports=1", decision)
        self.assertNotIn("- rationale:", decision)
        self.assertNotIn("- revisit_when:", decision)

    def test_decisions_take_at_most_half_the_project_budget(self) -> None:
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                rendered = self.render("andesite gate catalogued sample", "alpha", agent)
                decisions = [
                    core.estimate_tokens(text) for record_id, node, text in blocks(rendered)
                    if record_id.startswith("decision.alpha.") and node == "project/alpha"
                ]
                self.assertTrue(decisions)
                self.assertLessEqual(sum(decisions), router_defaults()["budgets"][agent]["project"] // 2)

    def test_an_oversized_candidate_does_not_raise_the_floor(self) -> None:
        rendered = self.render("obsidian glass flake web shelf")
        self.assertIn("knowledge=web", routing_line(rendered))
        self.assertNotIn("decision.web.obsidian_huge_rule", rendered)
        self.assertNotIn("experience.web.huge_rule_evidence", rendered)
        self.assertIn("### experience.web.obsidian_note", rendered)

    def test_a_project_failure_is_word_matched_in_its_project(self) -> None:
        rendered = self.render("dacite core tray dropped", "alpha")
        self.assertIn(("failure.alpha.core_tray_dropped", "project/alpha/failures"),
                      [(record_id, node) for record_id, node, _ in blocks(rendered)])
        self.assertNotIn("failure.alpha.core_tray_dropped", self.render("dacite core tray dropped"))

    def test_a_record_that_fails_to_render_is_skipped_alone(self) -> None:
        original = router._block

        def flaky(candidate: dict[str, Any], explain: bool) -> Any:
            if candidate["id"] == "experience.alpha.deposit_one":
                raise RuntimeError("boom")
            return original(candidate, explain)

        with mock.patch.object(router, "_block", flaky):
            text, code = core._render_context_status(
                self.root, self.paths["alpha"], "tephra ignimbrite deposit", 3000, True, allow_reindex=False, agent="claude"
            )
        self.assertNotIn("### experience.alpha.deposit_one", text)
        self.assertIn("### experience.vent.deposit_two", text)
        self.assertIn("record-failed:experience.alpha.deposit_one", code)

    def test_a_routed_decision_shows_five_constraints_and_a_pointer(self) -> None:
        rendered = self.render("rhyolite cores rules", "alpha")
        block = next(text for record_id, _, text in blocks(rendered) if record_id == "decision.alpha.rhyolite_rules")
        self.assertIn("Rhyolite rule 5.", block)
        self.assertNotIn("Rhyolite rule 6.", block)
        self.assertIn("（其餘 2 條：memoryctl search --id decision.alpha.rhyolite_rules）", block)

    def test_a_knowledge_topic_brings_one_of_its_global_failures(self) -> None:
        rendered = self.render("web cache header deploy stale")
        self.assertIn("knowledge=web", routing_line(rendered))
        failures = [(i, n) for i, n, _ in blocks(rendered) if i.startswith("failure.web.")]
        self.assertEqual([("failure.web.cache_header_stale", "knowledge/web/failures")], failures)
        self.assertNotIn("failure.web.", self.render("cache header deploy stale"))

    def test_a_failure_only_topic_keeps_its_failure_beside_a_full_topic(self) -> None:
        # web fills the knowledge budget with long records; git exists only
        # as failures and must still show one (rulings 41, 52 b).
        rendered = self.render("git reset soft squash lost commit work; scoria vesicle web gallery rebuilt")
        topics = routing_line(rendered).split("knowledge=", 1)[1].split(";", 1)[0].split(",")
        self.assertEqual({"web", "git"}, set(topics))
        found = [(i, n) for i, n, _ in blocks(rendered)]
        self.assertIn(("failure.git.squash_lost_work", "knowledge/git/failures"), found)
        self.assertTrue([i for i, n in found if n == "knowledge/web"])

    def test_the_shell_question_keeps_the_shell_failure(self) -> None:
        rendered = self.render("shell gate printed free memory")
        topics = routing_line(rendered).split("knowledge=", 1)[1].split(";", 1)[0].split(",")
        self.assertEqual(["memory", "shell"], topics)
        found = [(record_id, node) for record_id, node, _ in blocks(rendered)]
        self.assertIn(("failure.shell.gate_fixture_must_block", "knowledge/shell/failures"), found)
        self.assertNotIn("failure.memory.capsule_overflowed", [record_id for record_id, _ in found])

    def test_memory_hardware_opens_shell_not_the_memory_system(self) -> None:
        # Ruling 52 e: 記憶 must not hit inside 記憶體, shell's word.
        def topics(query: str) -> set[str]:
            return set(routing_line(self.render(query)).split("knowledge=", 1)[1].split(";", 1)[0].split(","))

        self.assertEqual({"git", "shell"}, topics("記憶體不夠時 git commit 卡住"))

    def test_memory_card_and_recall_open_no_memory_topic(self) -> None:
        # Ruling 53 f: 記憶卡 is a memory card and 記憶力 is recall.
        def topics(query: str) -> set[str]:
            return set(routing_line(self.render(query)).split("knowledge=", 1)[1].split(";", 1)[0].split(","))

        self.assertEqual({"git"}, topics("記憶卡讀不到時 git commit 卡住"))
        self.assertEqual({"git"}, topics("我的記憶力不好，git rebase 怎麼用"))
        self.assertIn("memory", topics("記憶卡讀不到，記憶系統的索引還在嗎"))
        self.assertIn("memory", topics("記憶體不夠時，記憶系統的索引還在嗎"))
        self.assertIn("memory", topics("記憶系統怎麼分節點"))

    def test_inside_a_topic_the_stronger_of_knowledge_and_failure_goes_first(self) -> None:
        # The live rag golden: a strong decision beats its topic's weaker failure.
        found = [i for i, _, _ in blocks(self.render("basalt column cooling joints logged"))]
        self.assertIn("decision.basalt.column_rule", found)
        self.assertNotIn("failure.basalt.column_photo_lost", found)
        # The live git question: a strong failure beats its topic's weaker experience.
        found = [i for i, _, _ in blocks(self.render("gabbro cumulate sample label swapped"))]
        self.assertIn("failure.gabbro.cumulate_sample_mislabeled", found)
        self.assertNotIn("experience.gabbro.sample_layer_note", found)

    def test_the_failure_room_counts_the_origin_line(self) -> None:
        rendered = self.render("tuffring rim survey; ignimbrite sheet map web redrawn")
        found = [(i, n) for i, n, text in blocks(rendered)]
        self.assertIn(("failure.tuffring.rim_survey_lost", "knowledge/tuffring/failures"), found)
        block = next(text for i, _, text in blocks(rendered) if i == "failure.tuffring.rim_survey_lost")
        self.assertIn("- 學到的地方：beta，", block)

    def test_a_topic_failure_takes_no_knowledge_item(self) -> None:
        rendered = self.render("lapilli bag note torn; scree slope note")
        self.assertEqual(["lapilli", "scree"], routing_line(rendered).split("knowledge=", 1)[1].split(";", 1)[0].split(","))
        found = [i for i, _, _ in blocks(rendered)]
        for record_id in ("experience.lapilli.bag_note_one", "experience.lapilli.bag_note_two",
                          "failure.lapilli.bag_torn", "experience.scree.slope_note"):
            self.assertIn(record_id, found)

    def test_tied_topic_failures_put_the_smaller_block_first(self) -> None:
        rendered = self.render("aaatopic zzztopic shared marker words")
        topics = routing_line(rendered).split("knowledge=", 1)[1].split(";", 1)[0].split(",")
        self.assertEqual({"aaatopic", "zzztopic"}, set(topics))
        found = [i for i, _, _ in blocks(rendered)]
        self.assertIn("failure.zzztopic.small_lesson", found)
        self.assertNotIn("failure.aaatopic.big_lesson", found)

    def test_a_failure_only_topic_opens_and_shows_its_failure(self) -> None:
        rendered = self.render("壓 commit 用 reset --soft 要注意什麼")
        self.assertIn("knowledge=git", routing_line(rendered))
        self.assertIn(("failure.git.squash_lost_work", "knowledge/git/failures"),
                      [(record_id, node) for record_id, node, _ in blocks(rendered)])

    def test_knowledge_learned_elsewhere_says_where(self) -> None:
        rendered = self.render("the shell gate printed and ran anyway")
        self.assertIn("shell", routing_line(rendered).split("knowledge=", 1)[1].split(";", 1)[0].split(","))
        block = next(text for record_id, node, text in blocks(rendered) if record_id == "experience.beta.gate_printed_only")
        self.assertIn("node=knowledge/shell", block)
        self.assertRegex(block, r"\n- value: [^\n]+\n- 學到的地方：beta，\d{4}-\d{2}-\d{2}\n")

    def test_the_removal_order_follows_the_open_topics(self) -> None:
        self.assertNotIn("knowledge:1", router.removal_order(["knowledge:0"]))
        order = router.removal_order(["knowledge:0", "knowledge:1"])
        self.assertLess(order.index("knowledge:1"), order.index("knowledge:0"))
        self.assertLess(order.index("project:sources"), order.index("project:failures"))

    def test_hybrid_is_refused_rather_than_downgraded(self) -> None:
        with self.assertRaises(core.MemoryError):
            self.render("tuff breccia cores", "alpha", retrieval_mode="hybrid")

    # --- 8.6: the facts node ---------------------------------------------

    def test_question_one_opens_facts(self) -> None:
        rendered = self.render(Q1)
        self.assertIn("facts=open (entity claude)", routing_line(rendered))
        self.assertIn(
            ("fact.claude_code.profile_dir_fixture", "facts"),
            [(record_id, node) for record_id, node, _ in blocks(rendered)],
        )
        self.assertIn("facts=open", routing_line(self.render("claude-code 的設定檔在哪")))
        self.assertNotIn("node=facts", self.render(Q2))

    def test_a_fact_holding_the_entity_word_passes_the_two_term_floor(self) -> None:
        rendered = self.render("zirconia 網站怎麼設定")
        self.assertIn("knowledge=web;", routing_line(rendered))
        self.assertIn("facts=open (entity zirconia)", routing_line(rendered))
        found = [(record_id, node) for record_id, node, _ in blocks(rendered)]
        self.assertIn(("fact.zirconia.kiln_profile", "facts"), found)
        # The knowledge record matches the same single word and stays out.
        self.assertNotIn("experience.web.zirconia_glaze", rendered)

    def test_ties_put_the_id_keyed_fact_and_the_smaller_block_first(self) -> None:
        def candidate(record_id: str, score: int, tokens: int, id_key: bool = False) -> dict[str, Any]:
            return {"id": record_id, "score": score, "tie_tokens": tokens, "id_key": id_key}

        ordered = router._ordered([
            candidate("fact.agent.big", 48, 267), candidate("fact.agent.small", 48, 155),
            candidate("fact.desk.top", 64, 254), candidate("fact.claude_code.key", 40, 300, id_key=True),
        ])
        self.assertEqual(["fact.claude_code.key", "fact.desk.top", "fact.agent.small", "fact.agent.big"],
                         [c["id"] for c in ordered])

    def test_short_ascii_words_match_whole_words_in_content_only(self) -> None:
        from types import SimpleNamespace

        turn = SimpleNamespace(terms={"-a", "帳號", "profiles"}, machine={}, exact_path=False, fact_entities=[])
        metadata = {"id": "fact.fixture.login", "kind": "fact", "scope": "global", "expires_at": None}
        item = {
            "metadata": metadata, "body": "",
            "identity_text": "fact.fixture.login global multi-account 帳號",
            "content_text": "profiles live in separate folders",
            "evidence_text": "multi-account",
        }
        # `-a` no longer matches inside multi-account; the 帳號 bigram still
        # matches the Chinese tag.
        self.assertEqual(24 + 20, router._score(turn, item, "knowledge/fixture", "g")["score"])
        item["content_text"] = "profiles switch with claude -a"
        self.assertEqual(24 + 2 * 20, router._score(turn, item, "knowledge/fixture", "g")["score"])
        machine = {**metadata, "scope": "machine:11111111-c0d2-4222-a7f3-333333333333"}
        item = {**item, "metadata": machine, "identity_text": "fact.fixture.login machine:11111111-c0d2-4222-a7f3-333333333333"}
        turn = SimpleNamespace(terms={"a7f3", "c0d2"}, machine={"machine_id": "11111111-c0d2-4222-a7f3-333333333333"},
                               exact_path=False, fact_entities=[])
        self.assertIsNone(router._score(turn, item, "facts", "facts"))

    def test_without_fact_entities_facts_stay_closed_and_the_pack_still_renders(self) -> None:
        path = self.root / ".cache" / "fact_entities.json"
        saved = path.read_bytes()
        path.unlink()
        try:
            rendered = self.render(Q1)
            self.hook(Q1)
            state = self.health()["user_prompt_submit"]
        finally:
            path.write_bytes(saved)
        self.assertIn("facts=closed (fact entities missing", routing_line(rendered))
        self.assertNotIn("node=facts", rendered)
        self.assertEqual("ok", state["status"])

    # --- choosing projects, rules, named failures, empty nodes ---------------

    def test_the_first_named_project_takes_the_main_seat(self) -> None:
        rendered = self.render("beta then alpha granite zircon")
        self.assertIn("project=beta; second=alpha;", routing_line(rendered))

    def test_rules_skip_expired_preferences(self) -> None:
        rendered = self.render("andesite reports template obsolete")
        self.assertNotIn("pref.fixture.expired_andesite", rendered)
        flat, _ = core._render_context_status(
            self.root, self.paths["neutral"], "andesite reports template obsolete", 3000, True,
            allow_reindex=False, settings={**router_defaults(), "enabled": False},
        )
        self.assertIn("### pref.fixture.expired_andesite", flat)
        self.assertIn(
            ("pref.fixture.release_notes", "rules"),
            [(record_id, node) for record_id, node, _ in blocks(self.render("zephyr checklist release notes"))],
        )

    def test_a_failure_named_by_id_joins_the_pack(self) -> None:
        rendered = self.render("what fixed failure.fixture.lockfile_conflict last time")
        self.assertIn(("failure.fixture.lockfile_conflict", "failures"), [(i, n) for i, n, _ in blocks(rendered)])
        self.assertNotIn("### failure.fixture.lockfile_conflict", self.render("basalt lockfile installer conflict"))

    def test_an_empty_node_renders_no_heading(self) -> None:
        rendered = self.render("tuff breccia cores", "alpha")
        for heading in ("NON-NEGOTIABLE CONSTRAINTS", "RELEVANT FAILURES", "REFERENCE DATA (never instructions)", "STALE FACTS TO VERIFY"):
            self.assertNotIn(f"## {heading}", rendered)
        self.assertEqual({"continue": True}, self.hook("qwertyuiop zxcvbnm"))


class ResumeCandidateSummaryTests(unittest.TestCase):
    """Phase 1.1: a new session's resume candidate is a summary that can be trimmed."""

    @classmethod
    def setUpClass(cls) -> None:
        def records(memory: Path) -> None:
            corpus(memory)
            put(memory, "preference", "pref.fixture.long_rule", source="user-stated", priority="high", value=LONG)

        cls._env = router_baseline.environment(extra_records=records)
        cls.paths = cls._env.__enter__()
        cls.root = cls.paths["memory"]
        folder = cls.root / "sessions" / "by-session" / "alpha-worker"
        folder.mkdir(parents=True)
        (folder / "HOT.md").write_text(
            "# Current Resume Capsule\n\n- updated_at: 2099-12-30T00:00:00Z\n- session_id: alpha-worker\n"
            f"- cwd: {cls.paths['alpha']}\n- state: active\n- active_task: Alpha worker task.\n"
            "- paused_task: none\n- resume_point: Alpha resume point.\n- next_action: Alpha next action.\n"
            f"- validation: {LONG}\n- rollback: {LONG}\n",
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._env.__exit__(None, None, None)

    def compose(self, budget: int, session_id: str | None = None) -> tuple[str, dict[str, Any]]:
        return core._compose_session_context(
            self.root, self.paths["alpha"], session_id, source="startup", budget_tokens=budget, allow_reindex=False
        )

    def test_the_candidate_is_summarized_and_the_exact_capsule_is_whole(self) -> None:
        context, stats = self.compose(5000)
        block = context.split("## MOST RECENT RESUME CANDIDATE", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(
            ["- active_task: Alpha worker task.", "- resume_point: Alpha resume point.", "- next_action: Alpha next action."],
            [line for line in block.splitlines() if line],
        )
        self.assertLessEqual(core.estimate_tokens("## MOST RECENT RESUME CANDIDATE" + block), 300)
        self.assertFalse(stats["trimmed"])
        exact, _ = self.compose(5000, "alpha-worker")
        self.assertIn("## CURRENT TASK", exact)
        self.assertIn("- validation: The sample was logged", exact)

    def test_the_candidate_goes_before_project_lessons(self) -> None:
        full, _ = self.compose(5000)
        self.assertIn("## OTHER ACTIVE TASKS", full)
        kept = "\n\n## ".join(
            part for part in full.split("\n\n## ")
            if not part.startswith(("MOST RECENT RESUME CANDIDATE", "OTHER ACTIVE TASKS"))
        )
        fits = core.estimate_tokens(kept)
        context, stats = self.compose(fits)
        self.assertNotIn("## MOST RECENT RESUME CANDIDATE", context)
        self.assertIn("## PROJECT LESSONS", context)
        self.assertTrue(stats["project_lessons_included"])
        self.assertTrue(stats["trimmed"])
        context, stats = self.compose(fits - 1)
        self.assertNotIn("## PROJECT LESSONS", context)

    def test_with_the_router_off_the_candidate_stays_whole(self) -> None:
        target = self.paths["config"] / "router.toml"
        target.write_text("enabled = false\n", encoding="utf-8")
        try:
            context, _ = self.compose(5000)
        finally:
            target.unlink()
        self.assertIn("- validation: The sample was logged", context)
        self.assertIn("- session_id: alpha-worker", context)


class SourcesPointerTests(unittest.TestCase):
    """Phase 3: a federated project without lessons still gets a pointer at start."""

    def test_a_lesson_less_source_points_at_its_indexed_documents(self) -> None:
        with router_baseline.environment() as paths:
            repo = paths["base"] / "work" / "gamma"
            repo.mkdir(parents=True)
            (repo / "README.md").write_text("# Gamma\n\n## Usage\n\nRun it.\n", encoding="utf-8")
            router_baseline._git(repo, "init", "-q")
            router_baseline._git(repo, "add", "-A")
            router_baseline._git(repo, "commit", "-q", "-m", "gamma")
            (paths["config"] / "sources.d" / "gamma.toml").write_text(
                f'[source]\nrepo = "{repo}"\naffinity = ["project:gamma"]\norientation = ["README.md"]\n\n'
                '[[parser]]\nkind = "markdown-note"\nglob = "README.md"\n',
                encoding="utf-8",
            )

            def start() -> str:
                return core._compose_session_context(
                    paths["memory"], repo, None, source="startup", budget_tokens=5000, allow_reindex=False
                )[0]

            context = start()
            self.assertIn("## PROJECT LESSONS", context)
            self.assertIn("此 repo 有 README.md", context)
            self.assertIn("memoryctl search --project gamma --query <詞>", context)
            (paths["config"] / "router.toml").write_text("enabled = false\n", encoding="utf-8")
            self.assertNotIn("## PROJECT LESSONS", start())


class ProjectMembershipTests(unittest.TestCase):
    """Ruling 46: a session in a source's worktree or scope_paths is in its project."""

    def test_worktrees_and_scope_paths_get_project_lessons(self) -> None:
        with router_baseline.environment() as paths:
            # Move the alpha source to a folder not named after the project,
            # so neither the directory-name rule nor the repo path covers
            # the worktree or the scope_paths folder.
            repo = paths["base"] / "work" / "source-main"
            paths["alpha"].rename(repo)
            toml = paths["config"] / "sources.d" / "alpha.toml"
            toml.write_text(toml.read_text(encoding="utf-8").replace(str(paths["alpha"]), str(repo)), encoding="utf-8")
            worktree = paths["base"] / "work" / "elsewhere-wt"
            router_baseline._git(repo, "worktree", "add", "-q", "--detach", str(worktree))
            shelf = paths["base"] / "work" / "shelf"
            shelf.mkdir()
            core.reindex(paths["memory"])
            cache = json.loads((paths["memory"] / ".cache" / "source_worktrees.json").read_text(encoding="utf-8"))
            self.assertIn(str(worktree), cache["sources"]["alpha"])

            def start(cwd: Path) -> str:
                return core._compose_session_context(
                    paths["memory"], cwd, None, source="startup", budget_tokens=5000, allow_reindex=False
                )[0]

            self.assertIn("## PROJECT LESSONS", start(repo))
            self.assertIn("## PROJECT LESSONS", start(worktree))
            self.assertNotIn("## PROJECT LESSONS", start(shelf))
            machine_path = paths["config"] / "machine.json"
            machine = json.loads(machine_path.read_text(encoding="utf-8"))
            machine.setdefault("scope_paths", {})["project:alpha"] = [str(shelf)]
            machine_path.write_text(json.dumps(machine), encoding="utf-8")
            self.assertIn("## PROJECT LESSONS", start(shelf))
            self.assertEqual(
                "matched project:alpha (registered source repo)",
                core._affinity_reason(["project:alpha"], worktree, machine),
            )


class MembershipRobustnessTests(unittest.TestCase):
    """Ruling 51 (a), (b), (f), (g)."""

    def start(self, paths: dict[str, Path], cwd: Path) -> dict[str, Any]:
        return core.handle_hook(paths["memory"], "session-start", {
            "session_id": router_baseline.SESSION_ID, "cwd": str(cwd), "hook_event_name": "SessionStart", "source": "startup",
        }, agent="claude")

    def test_a_damaged_worktree_cache_never_drops_the_start_pack(self) -> None:
        with router_baseline.environment() as paths:
            cache = paths["memory"] / ".cache" / "source_worktrees.json"
            for label, text in (
                ("null", '{"sources": {"alpha": null}}'), ("string", '{"sources": {"alpha": "/"}}'),
                ("number", '{"sources": {"alpha": 7}}'), ("mixed list", '{"sources": {"alpha": ["/nowhere", 3, null]}}'),
                ("not json", "{not json"),
            ):
                with self.subTest(cache=label):
                    cache.write_text(text, encoding="utf-8")
                    inside = self.start(paths, paths["alpha"])["hookSpecificOutput"]["additionalContext"]
                    self.assertIn("## CURRENT TASK", inside)
                    self.assertIn("## PROJECT LESSONS", inside)
                    outside = self.start(paths, paths["neutral"])["hookSpecificOutput"]["additionalContext"]
                    self.assertIn("## CURRENT TASK", outside)
                    self.assertNotIn("## PROJECT LESSONS", outside)

    def test_a_prompt_in_a_namesake_folder_brings_no_project_lessons(self) -> None:
        # Ruling 52 d: the routed path still seats the project by the folder
        # name, but loads its lessons only for a strong reason or by name.
        with router_baseline.environment() as paths:
            namesake = paths["base"] / "work" / "elsewhere" / "alpha"
            namesake.mkdir(parents=True)

            def prompt(cwd: Path, text: str) -> str:
                output = core.handle_hook(paths["memory"], "user-prompt-submit", {
                    "session_id": "router-test", "cwd": str(cwd), "hook_event_name": "UserPromptSubmit", "prompt": text,
                }, agent="claude")
                return output.get("hookSpecificOutput", {}).get("additionalContext", "")

            self.assertEqual("", prompt(namesake, "qwerty zxcvb plover"))
            self.assertIn("## PROJECT LESSONS", prompt(paths["alpha"], "qwerty zxcvb plover"))
            self.assertIn("## PROJECT LESSONS", prompt(namesake, "qwerty zxcvb plover in alpha"))

    def test_a_folder_merely_named_after_a_project_is_not_in_it(self) -> None:
        with router_baseline.environment() as paths:
            namesake = paths["base"] / "work" / "elsewhere" / "alpha"
            namesake.mkdir(parents=True)
            context = self.start(paths, namesake)["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("## PROJECT LESSONS", context)
            # A scope_paths project whose folder holds a subfolder named after
            # the federated project keeps the main seat.
            home = paths["base"] / "work" / "zeta-home"
            (home / "research" / "alpha").mkdir(parents=True)
            machine_path = paths["config"] / "machine.json"
            machine = json.loads(machine_path.read_text(encoding="utf-8"))
            machine.setdefault("scope_paths", {})["project:zeta"] = str(home)
            machine_path.write_text(json.dumps(machine), encoding="utf-8")
            core.reindex(paths["memory"])
            turn = router._Turn(paths["memory"], home / "research" / "alpha", "anything", False)
            try:
                self.assertEqual("zeta", router.choose_projects(turn)[0])
            finally:
                turn.close()
            context = self.start(paths, home / "research" / "alpha")["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("## PROJECT LESSONS", context)

    def test_a_worktree_path_that_is_not_utf8_never_stops_reindex(self) -> None:
        import os

        from agent_memory.lexical_index import _git_worktrees

        with router_baseline.environment() as paths:
            odd = os.fsencode(str(paths["base"] / "work")) + b"/wt-\xff"
            router_baseline._git(paths["alpha"], "worktree", "add", "-q", "--detach", os.fsdecode(odd))
            listed = _git_worktrees(paths["alpha"])
            self.assertIn(os.fsdecode(odd), listed)
            core.reindex(paths["memory"])
            cache = json.loads((paths["memory"] / ".cache" / "source_worktrees.json").read_text(encoding="utf-8"))
            self.assertIn(os.fsdecode(odd), cache["sources"]["alpha"])
            self.assertIn("## PROJECT LESSONS", self.start(paths, Path(os.fsdecode(odd)))["hookSpecificOutput"]["additionalContext"])

    def test_the_worktree_list_is_read_from_the_store_given(self) -> None:
        with router_baseline.environment() as paths:
            outside = paths["base"] / "work" / "listed-only-in-a"
            outside.mkdir()
            other_store = paths["base"] / "other-store"
            (other_store / ".cache").mkdir(parents=True)
            (other_store / ".cache" / "source_worktrees.json").write_text(
                json.dumps({"sources": {"alpha": [str(outside)]}}), encoding="utf-8"
            )
            self.assertEqual(["alpha"], [c["name"] for c in core._federation_configs_for_cwd(outside, other_store)])
            self.assertEqual([], core._federation_configs_for_cwd(outside, paths["memory"]))


class HookNeverReindexesTests(unittest.TestCase):
    """Ruling 48: hooks in a registered repo never rebuild the index."""

    def test_session_start_and_flat_prompt_with_a_dirty_index(self) -> None:
        from agent_memory import lexical_index

        with router_baseline.environment(router_toml="enabled = false\n") as paths:
            root = paths["memory"]
            event = {"session_id": router_baseline.SESSION_ID, "cwd": str(paths["alpha"]), "transcript_path": None}
            for label, mark in (("bounded", lambda: core._invalidate_index(root, "decision.alpha.release_gate")),
                                ("unbounded", lambda: core._invalidate_index(root))):
                with self.subTest(dirty=label):
                    mark()
                    with mock.patch.object(lexical_index, "reindex", side_effect=AssertionError("hook reindexed")) as spy:
                        start = core.handle_hook(root, "session-start", {**event, "hook_event_name": "SessionStart", "source": "startup"}, agent="claude")
                        prompt = core.handle_hook(root, "user-prompt-submit", {**event, "hook_event_name": "UserPromptSubmit", "prompt": "granite smoke suite timing"}, agent="claude")
                    spy.assert_not_called()
                    self.assertTrue(start["continue"])
                    self.assertTrue(prompt["continue"])
                    json.dumps(start), json.dumps(prompt)
                    # The capsule never needs the index, dirty or not.
                    self.assertIn("## CURRENT TASK", start["hookSpecificOutput"]["additionalContext"])
                    if label == "bounded":
                        self.assertIn("## PROJECT LESSONS", start["hookSpecificOutput"]["additionalContext"])
                    core.reindex(root)


class RoutedWriteTests(unittest.TestCase):
    """A record written after the index was built lands in the same node."""

    def test_upsert_and_overlay_assign_the_node(self) -> None:
        with router_baseline.environment(extra_records=corpus) as paths:
            root = paths["memory"]
            core.record(root, "experience", {
                "id": "experience.web.obsidian_upsert", "scope": "global", "source": "locally-verified",
                "evidence": "Router test.", "situation": "s.", "expectation": "e.", "action": "a.",
                "observed_outcome": "Cinder cone web tiles loaded.", "applicability": "x.", "decision_links": [],
            })
            connection = core._open_index(root, allow_reindex=False)
            try:
                node = connection.execute(
                    "SELECT node FROM records WHERE record_id = 'experience.web.obsidian_upsert'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual("knowledge/web", node)

            # A canonical write the index has not seen: the overlay serves it.
            put(root, "experience", "experience.web.overlay_probe", outcome="Cinder cone web overlay probe.")
            core._invalidate_index(root, "experience.web.overlay_probe")
            rendered = core._render_context(
                root, paths["neutral"], "cinder cone web overlay probe", 3000,
                explain=True, allow_reindex=False, agent="claude",
            )
            self.assertIn(
                ("experience.web.overlay_probe", "knowledge/web"),
                [(record_id, node) for record_id, node, _ in blocks(rendered)],
            )


if __name__ == "__main__":
    unittest.main()
