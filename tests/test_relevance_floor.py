"""The relevance floor on what a record matched (spec 實際使用回饋改善 F3)."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from agent_memory import core, router  # noqa: E402
from agent_memory.storage import _parse_router_toml  # noqa: E402
import router_baseline  # noqa: E402


def pref(root: Path, record_id: str, value: str, scope: str = "global") -> None:
    core.record(root, "preference", {"id": record_id, "scope": scope, "source": "user-stated", "priority": "normal",
                                     "evidence": "Fixture.", "value": value})


def context(paths: dict, cwd: Path, text: str) -> str:
    settings, _, _ = core.router_settings()
    rendered, _ = core._render_context_status(paths["memory"], cwd, text, 2400, False, allow_reindex=False,
                                              agent="claude", settings={**settings, "use_feedback": False})
    return rendered


class RelevanceFloorTests(unittest.TestCase):
    def test_a_fragment_is_not_a_word(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            # 的記 is a fragment of 我的記錄; 系統 the one word the prompt shares.
            pref(root, "pref.fixture.kiln_log", "窯爐溫度都記完了，我的記錄在控制系統上看得到。")
            pref(root, "pref.fixture.podman_only", "容器一律用 podman。")
            core.reindex(root)
            asked = context(paths, paths["neutral"], "那台機器的記憶系統還行嗎？")
            one_word = context(paths, paths["neutral"], "podman")
        self.assertNotIn("pref.fixture.kiln_log", asked)
        # A one-word prompt needs its one word.
        self.assertIn("### pref.fixture.podman_only", one_word)
        # Two words of the prompt, one uncommon, are enough.
        with router_baseline.environment() as paths:
            pref(paths["memory"], "pref.fixture.recall_check", "記憶系統還行嗎要看評分：每週抽題問有沒有用。")
            core.reindex(paths["memory"])
            self.assertIn("### pref.fixture.recall_check", context(paths, paths["neutral"], "那台機器的記憶系統還行嗎？"))

    def test_a_record_of_a_project_the_prompt_does_not_name_needs_uncommon_words(self) -> None:
        class Turn:
            meaningful = {"zircon", "cobalt", "probe"}
            rare = {"zircon"}
            cwd_project = "alpha"

            def __init__(self, named: bool) -> None:
                self.named = named

            def names(self, tag: str, record_id: str = "") -> bool:
                return self.named and tag == "project:beta"

        beta = {"id": "experience.beta.zircon_note", "scope": "project:beta"}
        here = {"id": "experience.alpha.zircon_note", "scope": "project:alpha"}
        everywhere = {"id": "experience.kiln.zircon_note", "scope": "global"}
        two_words = {"zircon", "cobalt"}
        self.assertTrue(router._relevant(Turn(False), here, two_words))
        # Another project's record needs min(2, 3) = 2 uncommon words unless the prompt names it.
        self.assertFalse(router._relevant(Turn(False), beta, two_words))
        self.assertTrue(router._relevant(Turn(True), beta, two_words))
        # Named, it still needs one uncommon word; the cwd's own and a global record do not (V1).
        self.assertFalse(router._relevant(Turn(True), beta, {"cobalt", "probe"}))
        self.assertTrue(router._relevant(Turn(False), here, {"cobalt", "probe"}))
        self.assertTrue(router._relevant(Turn(False), everywhere, {"cobalt", "probe"}))
        self.assertFalse(router._relevant(Turn(False), here, {"zircon"}))  # one of the two needed

    def test_outside_any_project_the_project_the_prompt_names_is_the_main_one(self) -> None:
        # Asked from ~ about beta's store screenshots (上架, 截圖): the words are common in beta, and it is the project asked about.
        class Turn:
            meaningful = {"上架", "截圖"}
            rare: set[str] = set()
            cwd_project = None
            main_project = "beta"

            def names(self, tag: str, record_id: str = "") -> bool:
                return tag == "project:beta"

        named = {"id": "experience.beta.store_screenshots", "scope": "project:beta"}
        other = {"id": "experience.web.thai_localization", "scope": "project:web"}
        self.assertTrue(router._relevant(Turn(), named, {"上架", "截圖"}))
        self.assertFalse(router._relevant(Turn(), other, {"上架", "截圖"}))  # another project still needs uncommon words

    def test_one_word_cut_into_two_bigrams_counts_once(self) -> None:
        # 使用者 is 使用 and 用者 side by side: one word, not the two the floor needs (designer 2026-10-21).
        with router_baseline.environment() as paths:
            root = paths["memory"]
            pref(root, "pref.fixture.user_list", "每個使用者都看得到清單。")
            pref(root, "pref.fixture.user_release", "使用者確認後才放行上線。")
            core.reindex(root)
            two_words = context(paths, paths["neutral"], "使用者 放行")
            one_word = context(paths, paths["neutral"], "使用者")
        self.assertNotIn("pref.fixture.user_list", two_words)  # only 使用者 of the two
        self.assertIn("### pref.fixture.user_release", two_words)
        self.assertIn("### pref.fixture.user_list", one_word)  # every word the prompt has

    def test_a_longer_span_holds_a_word_in_two_characters(self) -> None:
        from types import SimpleNamespace

        def words(query: str, record: str) -> int:
            terms = core._query_terms(query)
            return router._words_held(SimpleNamespace(query=query), {t for t in terms if t in record}, record)

        self.assertEqual(1, words("使用者", "所有使用者"))
        self.assertEqual(2, words("使用者介面", "使用者介面要簡單"))
        self.assertEqual(3, words("使用者介面", "使用的介面與用者"))  # apart, each counts

    def test_the_function_characters_are_configurable(self) -> None:
        from agent_memory.storage import ROUTER_DEFAULT_FILLER_WORDS, ROUTER_DEFAULT_FUNCTION_CHARS

        fillers = list(ROUTER_DEFAULT_FILLER_WORDS)
        settings = _parse_router_toml({"relevance": {"function_chars": "的 了", "rare_share": 0.1}})
        self.assertEqual(({"function_chars": "的了", "rare_share": 0.1, "rare_min_docs": 5, "filler_words": fillers}, []),
                         (settings["relevance"], settings["problems"]))
        broken = _parse_router_toml({"relevance": {"function_chars": "", "rare_share": 2}})
        self.assertEqual({"function_chars": ROUTER_DEFAULT_FUNCTION_CHARS, "rare_share": 0.05, "rare_min_docs": 5,
                          "filler_words": fillers}, broken["relevance"])
        self.assertEqual(2, len(broken["problems"]))

    def test_words_of_conversation_open_nothing(self) -> None:
        # 繼續 alone brought three unrelated records (probe 繼續, designer 2026-10-21).
        with router_baseline.environment() as paths:
            root = paths["memory"]
            pref(root, "pref.fixture.keep_going", "使用者說繼續時，照上一輪的計畫往下做，不要重新問。")
            pref(root, "pref.fixture.thanks_note", "Thanks notes go out on Friday.")
            pref(root, "pref.fixture.podman_only", "容器一律用 podman。")
            pref(root, "pref.fixture.ok_button", "The ok-button stays on the right of the dialog.")
            core.reindex(root)
            chinese = context(paths, paths["neutral"], "繼續")
            english = context(paths, paths["neutral"], "thanks!")
            subject = context(paths, paths["neutral"], "好的，繼續用 podman")
            identifier = context(paths, paths["neutral"], "ok-button")
        self.assertNotIn("pref.fixture.keep_going", chinese)
        self.assertNotIn("pref.fixture.thanks_note", english)
        # What is left is the subject; before, 續用 was a second word podman_only lacked.
        self.assertIn("### pref.fixture.podman_only", subject)
        self.assertIn("### pref.fixture.ok_button", identifier)

    def test_the_filler_words_are_configurable(self) -> None:
        with router_baseline.environment(router_toml='[relevance]\nfiller_words = ["收工"]\n') as paths:
            root = paths["memory"]
            pref(root, "pref.fixture.keep_going", "使用者說繼續時，照上一輪的計畫往下做，不要重新問。")
            pref(root, "pref.fixture.wrap_up", "使用者說收工時，先存檔再停。")
            core.reindex(root)
            # The file's list takes the place of the default one.
            self.assertNotIn("pref.fixture.wrap_up", context(paths, paths["neutral"], "收工"))
            self.assertIn("### pref.fixture.keep_going", context(paths, paths["neutral"], "繼續"))
        broken = _parse_router_toml({"relevance": {"filler_words": "繼續"}})
        self.assertEqual(["relevance.filler_words must be an array of strings; the default filler words apply"],
                         broken["problems"])


class SpanFillerAndCostTests(unittest.TestCase):
    def test_a_word_and_the_character_after_it_are_two_words(self) -> None:
        # 直接發 is 直接 + 發 (發出 in the prompt, 發布 in the record), not one word like 使用者;
        # the ledger tells them apart by how many records hold each bigram (retrieval re-check R2).
        with router_baseline.environment() as paths:
            root = paths["memory"]
            pref(root, "pref.fixture.publish_after_review", "總經理審過就直接發布，不用再等。")
            for n, words in enumerate(("直接改設定檔", "直接重跑測試", "直接回報結果")):
                pref(root, f"pref.fixture.direct_{n}", f"遇到這種情況就{words}。")
            core.reindex(root)
            asked = context(paths, paths["neutral"], "審完你直接發出來")
        self.assertIn("### pref.fixture.publish_after_review", asked)

    def test_words_of_conversation_go_only_from_the_start_never_out_of_the_rest(self) -> None:
        settings = {"relevance": {"filler_words": ["繼續", "好的", "了解", "ok", "thanks"]}}
        kept = ("為了解決 podman", "最好的做法", "OK 按鈕放右邊", "帳號換了，跟團隊說繼續", "ok-button")
        self.assertEqual(list(kept), [router._without_fillers(text, settings).strip() for text in kept])
        self.assertEqual(["用 podman", "", ""],
                         [router._without_fillers(text, settings).strip() for text in ("好的，繼續用 podman", "繼續", "ok, thanks!")])
        # 繼續 at the end is the subject (retrieval re-check R3).
        with router_baseline.environment() as paths:
            pref(paths["memory"], "pref.fixture.pause_rule", "換帳號以後各 session 繼續原本的工作。")
            core.reindex(paths["memory"])
            self.assertIn("### pref.fixture.pause_rule", context(paths, paths["neutral"], "帳號換了，跟團隊說繼續"))

    def test_the_prompt_is_normalized_once_a_turn_whatever_the_candidates(self) -> None:
        from unittest import mock

        from test_router import put

        def calls(count: int) -> int:
            def records(root: Path) -> None:
                for i in range(count):
                    put(root, "experience", f"experience.alpha.pumice_{i:02d}", scope="project:alpha",
                        outcome=f"Pumice run {i} shown near the 使用者介面.")

            with router_baseline.environment(extra_records=records) as paths:
                with mock.patch.object(core, "_normalize_search_text", wraps=core._normalize_search_text) as normalize:
                    core._render_context(paths["memory"], paths["alpha"], "pumice glaze 使用者介面 kiln vent", 3000,
                                         explain=True, allow_reindex=False, agent="claude", feedback=False)
            return normalize.call_count

        # Each candidate normalized the whole prompt again: 4k characters, 465 times (retrieval re-check R5).
        self.assertEqual(calls(3), calls(30))


if __name__ == "__main__":
    unittest.main()
