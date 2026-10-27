"""Whether the agent used a memory is judged from what it wrote, not asked
for (spec 實際使用回饋改善 F12)."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from agent_memory import core  # noqa: E402
import router_baseline  # noqa: E402

BODY = "granite smoke suite timing; release notes zephyr checklist migration"


def context(paths: dict, name: str, **fields: str) -> str:
    hook = {"session-start": "SessionStart", "user-prompt-submit": "UserPromptSubmit"}[name]
    output = core.handle_hook(paths["memory"], name, {
        "session_id": "s1", "cwd": str(paths["alpha"]), "hook_event_name": hook, **fields,
    }, agent="claude")
    return output.get("hookSpecificOutput", {}).get("additionalContext", "")


class CitationLineTests(unittest.TestCase):
    def test_routed_packs_no_longer_ask_for_a_citation_and_the_flat_path_still_does(self) -> None:
        with router_baseline.environment() as paths:
            start = context(paths, "session-start", source="startup")
            prompt = context(paths, "user-prompt-submit", prompt=BODY)
        with router_baseline.environment(router_toml="enabled = false\n") as paths:
            flat_start = context(paths, "session-start", source="startup")
            flat_prompt = context(paths, "user-prompt-submit", prompt=BODY)
        self.assertIn("### decision.alpha.release_gate", prompt)
        self.assertNotIn(core.CITATION_REQUEST, start)
        self.assertNotIn(core.CITATION_REQUEST, prompt)
        self.assertIn(core.CITATION_REQUEST, flat_start)
        self.assertIn(core.CITATION_REQUEST, flat_prompt)


BLOCK = ("### decision.alpha.kiln_gate\n- kind: decision\n- scope: project:alpha\n"
         "- value: The kiln gate script lives at /w/alpha/src/gate.sh and exits 99 below the cone; "
         "we run it with `gate --strict` before every firing schedule review.")


def used(output: str, *messages: str) -> tuple[str, str] | None:
    from agent_memory import usage

    echo = usage.Echo()
    for message in messages:
        echo.add(message)
    features = usage.UsageDetector(None).features("decision.alpha.kiln_gate", BLOCK)
    return usage.match(features, usage.normalize(output), echo, [None])


class UsageRuleTests(unittest.TestCase):
    def test_an_id_a_particular_string_or_a_phrase_of_the_record_is_use(self) -> None:
        self.assertEqual(("id", "decision.alpha.kiln_gate"), used("Per decision.alpha.kiln_gate we wait."))
        self.assertEqual(("literal", "/w/alpha/src/gate.sh"), used("Running /w/alpha/src/gate.sh now."))
        self.assertEqual(("literal", "gate --strict"), used("I ran `gate --strict` first."))
        self.assertEqual(("phrase", "before every firing schedule"), used("I do it before every firing schedule change."))
        self.assertIsNone(used("The gate is fine."))

    def test_a_longer_id_starting_with_the_record_s_names_another_record(self) -> None:
        from agent_memory import usage

        short = "federation.alpha.note-kiln-record-1"
        features = usage.UsageDetector(None).features(short, f"### {short}\n- value: The first kiln record.")

        def named(output: str) -> tuple[str, str] | None:
            return usage.match(features, usage.normalize(output), usage.Echo(), [None], spoken=usage.normalize(output))

        self.assertIsNone(named("依據 federation.alpha.note-kiln-record-10-shelf 改了。"))
        self.assertIsNone(named("See federation.alpha.note-kiln-record-1.b for that."))
        self.assertEqual(("id", short), named("依據 federation.alpha.note-kiln-record-1。"))
        self.assertEqual(("id", short), named("Per federation.alpha.note-kiln-record-1. Then fire."))

    def test_a_long_run_without_a_slash_is_read_in_linear_time(self) -> None:
        import time

        from agent_memory import usage

        started = time.monotonic()
        found = usage.literals("x" * 30_000 + " see /home/u/agent-memory/tools/x.py")
        self.assertLess(time.monotonic() - started, 1.0)  # 30,000 characters took about nine seconds
        self.assertIn("/home/u/agent-memory/tools/x.py", found)

    def test_what_the_rating_path_reads_of_one_line_is_capped_as_replay_s(self) -> None:
        import json

        from agent_memory import rating

        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Writing the export."},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/x.md", "content": "y" * 4_000_000}}]}})
        self.assertLessEqual(len(rating._output_text(line)), 20_000)

    def test_what_the_user_said_is_not_the_records(self) -> None:
        # Every message of the window counts, and each whole however long it is.
        long = "filler " * 4000 + "see /w/alpha/src/gate.sh for the gate"
        self.assertIsNone(used("Reading /w/alpha/src/gate.sh now.", long))
        self.assertIsNone(used("Reading /w/alpha/src/gate.sh now.", "the gate script is /w/alpha/src/gate.sh", "and check it"))
        # A path the user gave, cut into words, is not a phrase of the record either.
        self.assertIsNone(used("The file at /w/alpha/src/gate.sh is the one.", "see /w/alpha/src/gate.sh for the gate"))

    def test_what_reading_the_matches_showed_to_be_chance_is_not_use(self) -> None:
        from agent_memory import usage

        # Rule v2: patterns 30 matches of rule v1 showed to be chance.
        def matches(record: str, output: str) -> tuple[str, str] | None:
            features = usage.UsageDetector(None).features("fact.x.y", "### fact.x.y\n- value: " + record)
            return usage.match(features, usage.normalize(output), usage.Echo(), [None])

        self.assertIsNone(matches("Each report costs 11.6 dollars.", "Output at least 3411.6 in week 27."))  # inside a longer number
        self.assertIsNone(matches("The coefficient is 1.80 here.", "The kiln fell 1.80 yesterday."))  # a number without a unit
        self.assertIsNone(matches("Poll /api/health after a deploy.", "curl -s https://x.example/api/health"))  # two segments
        self.assertIsNone(matches("The type sits in `protocol.ts` now.", "grep -n cap protocol.ts"))  # one word in backticks
        self.assertIsNone(matches("Read the rest of the log first.", "I read the rest of the test."))  # small words
        self.assertIsNone(matches("Check origin main git diff output.", "git fetch origin main git diff --stat"))  # command words
        # Rule v3, after 30 matches of v2 were read.
        self.assertIsNone(matches("Coverage leaves 3.67% out.", "樣本少了 3.67%，不是整批。"))  # a percentage
        self.assertIsNone(matches("Clone into /home/u/desktop/kiln-game first.", "cd /home/u/desktop/kiln-game-worktrees/lead"))
        self.assertIsNone(matches("Output went from 13.402 to 58.731 58.70 1.36 4.25 then.", "58.731 58.70 1.36 4.25"))
        self.assertIsNone(matches("Never take the vendor's rated midpoint as is.",
                                  "memoryctl record --kind failure: never take the vendor's rated midpoint"))
        # Rule v4, after 30 matches of v3 were read.
        self.assertIsNone(matches("Redraw the pages of kiln v13 cone v12 first.", "kiln v11, kiln v13 cone v12 glaze v11"))
        self.assertIsNone(matches("Clone it to /home/u/desktop/kiln-game/client first.", "cd /home/u/desktop/kiln-game/client && git status"))
        # What stays use.
        self.assertEqual(("literal", "312 ms"), matches("Startup takes 312 ms at worst.", "It took 312 ms here too."))
        self.assertEqual(("literal", "pgrep -x loupe"), matches("Check with `pgrep -x loupe` first.", "sleep 3; pgrep -x loupe"))
        self.assertEqual(("phrase", "the vendor s rated"),
                         matches("Never take the vendor's rated midpoint as is.", "g is the vendor's rated midpoint times"))

    def test_a_sample_of_the_agents_words_holds_no_secret_and_no_address(self) -> None:
        from agent_memory import usage

        text = usage.normalize("Mail ops@example.com then run /w/alpha/src/gate.sh again.")
        self.assertEqual("mail [email] then run /w/alpha/src/gate.sh again.", usage.snippet(text, "/w/alpha/src/gate.sh"))
        self.assertIsNone(usage.snippet(usage.normalize("token sk-" + "a" * 24 + " /w/alpha/src/gate.sh"), "/w/alpha/src/gate.sh"))


if __name__ == "__main__":
    unittest.main()
