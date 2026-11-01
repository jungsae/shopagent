import json
import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shop.upstage import assert_upstage_remote_allowed, build_decide_body, dump_decide_payloads, get_upstage_api_key, post_systemone, require_upstage_api_key
from shop.upstage_dryrun import main


FIXTURES = Path(__file__).parent / "fixtures" / "upstage"
ROOT = Path(__file__).resolve().parent.parent


class DecideDryRunTests(unittest.TestCase):
    def test_smoke_payloads_without_http_even_with_live_flags(self):
        with patch.dict(os.environ, {"UPSTAGE_API_KEY": "mock-only-key", "SHOPAGENT_UPSTAGE_ALLOW_HTTP": "1"}, clear=True), \
                patch("shop.upstage.post_systemone", side_effect=AssertionError("No post")) as post, \
                patch("socket.socket", side_effect=AssertionError("No network")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("No urllib")), \
                patch("httpx.Client.send", side_effect=AssertionError("No httpx")):
            records = dump_decide_payloads()
            post.assert_not_called()
        self.assertEqual({record["scenario_id"] for record in records}, {"single-01", "single-07", "single-18"})
        first_turns = [record for record in records if record["turn_index"] == 0]
        self.assertEqual(len(first_turns), 3)
        prompts = json.loads((ROOT / "data/prompts.json").read_text(encoding="utf-8"))
        for record in records:
            body = record["body"]
            self.assertEqual(body["model"], "solar-decide")
            self.assertTrue(body["state"].startswith("human: "))
            self.assertEqual(record["item"]["id"], "route_" + str(record["turn_index"]))
            self.assertEqual(body["state"], record["item"]["text"])
            self.assertEqual(body["questions"]["route"], {
                "type": "choice", "instructions": prompts["supervisor"], "criteria": prompts["routes"],
            })
            self.assertEqual(set(body["questions"]["route"]["criteria"]), {"product", "order_delivery", "refund_exchange", "payment", "qna", "human"})

    def test_multiturn_user_history(self):
        dataset = json.loads((ROOT / "data/scenarios.json").read_text(encoding="utf-8"))
        scenario = next(s for s in dataset["scenarios"] if s["id"] == "chain-01")
        records = dump_decide_payloads(["chain-01"])
        self.assertEqual(len(records), len(scenario["turns"]))
        for index, record in enumerate(records):
            expected = "\n".join("human: " + turn["user"] for turn in scenario["turns"][:index + 1][-10:])
            self.assertEqual(record["body"]["state"], expected)

    def test_cli_stdout_and_new_file_refuses_overwrite(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--ids", "single-01,single-07"]), 0)
        records = json.loads(output.getvalue())
        self.assertEqual({r["scenario_id"] for r in records}, {"single-01", "single-07"})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "payloads.json"
            self.assertEqual(main(["--ids", "single-01,single-07", "--out", str(path)]), 0)
            before = path.read_bytes()
            self.assertEqual(json.loads(before), records)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["--out", str(path)])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(path.read_bytes(), before)

    def test_cli_refuses_runs_and_unknown_ids(self):
        for args in (["--out", str(ROOT / "runs/dryrun.json")], ["--ids", "unknown"], ["--ids", "single-01,"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(args)
            self.assertEqual(error.exception.code, 2)


class DecideBodyTests(unittest.TestCase):
    def setUp(self):
        self.item = json.loads((FIXTURES / "decide_item.json").read_text(encoding="utf-8"))

    def test_fixture_snapshot(self):
        expected = json.loads((FIXTURES / "decide_request.json").read_text(encoding="utf-8"))
        self.assertEqual(build_decide_body(self.item), expected)

    def test_request_keys(self):
        body = build_decide_body(self.item)
        self.assertEqual(set(body), {"model", "state", "questions"})
        route = body["questions"]["route"]
        self.assertEqual(set(route), {"type", "instructions", "criteria"})
        self.assertEqual(set(route["criteria"]), set(self.item["questions"]["route"]["options"]))
        self.assertEqual(set(route["criteria"]), {"product", "order_delivery", "refund_exchange", "payment", "qna", "human"})

    def test_model_override_and_default(self):
        self.assertEqual(build_decide_body(self.item, "mock-model")["model"], "mock-model")
        self.assertEqual(build_decide_body(self.item, "")["model"], "solar-decide")

    def test_score_and_noul(self):
        item = {"text": "Review this request.", "questions": {
            "quality": {"type": "score", "instructions": "Rate quality.", "levels": {"0": "Poor", "1": "Good"}},
            "valid": {"type": "noul", "instructions": "Is this valid?"},
        }}
        self.assertEqual(build_decide_body(item)["questions"], {
            "quality": {"type": "score", "instructions": "Rate quality.", "criteria": ["Poor", "Good"]},
            "valid": {"type": "noul", "instructions": "Is this valid?"},
        })

    def test_missing_text_or_questions(self):
        for item in ({}, {"questions": self.item["questions"]}, {"text": "Hello"}, {"text": None, "questions": self.item["questions"]}, {"text": "Hello", "questions": None}, {"text": "Hello", "questions": {}}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                build_decide_body(item)

    def test_unsupported_question_type(self):
        for kind in ("unknown", None):
            self.item["questions"]["route"]["type"] = kind
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "Unsupported"):
                build_decide_body(self.item)


class UpstageGuardTests(unittest.TestCase):
    def test_missing_or_blank_key(self):
        for environ in ({}, {"UPSTAGE_API_KEY": ""}, {"UPSTAGE_API_KEY": " \t\n"}):
            with self.subTest(environ=environ):
                self.assertIsNone(get_upstage_api_key(environ))
                with self.assertRaisesRegex(RuntimeError, "UPSTAGE_API_KEY is missing/empty"):
                    require_upstage_api_key(environ)

    def test_nonempty_key_is_stripped(self):
        environ = {"UPSTAGE_API_KEY": " mock-only-key \n"}
        self.assertEqual(get_upstage_api_key(environ), "mock-only-key")
        self.assertEqual(require_upstage_api_key(environ), "mock-only-key")

    def test_default_environment(self):
        with patch.dict(os.environ, {"UPSTAGE_API_KEY": " mock-only-key "}, clear=True):
            self.assertEqual(get_upstage_api_key(), "mock-only-key")
            self.assertEqual(require_upstage_api_key(), "mock-only-key")
            self.assertIsNone(get_upstage_api_key({}))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "UPSTAGE_API_KEY is missing/empty"):
                post_systemone({})

    def test_remote_refuses_missing_key_first(self):
        for allow in (None, "1"):
            environ = {} if allow is None else {"SHOPAGENT_UPSTAGE_ALLOW_HTTP": allow}
            for call in (assert_upstage_remote_allowed, lambda env: post_systemone({}, env)):
                with self.subTest(allow=allow, call=call), self.assertRaisesRegex(RuntimeError, "UPSTAGE_API_KEY is missing/empty"):
                    call(environ)

    def test_key_without_explicit_allow_refuses(self):
        for allow in (None, "0", "true", " 1 "):
            environ = {"UPSTAGE_API_KEY": "mock-only-key"}
            if allow is not None:
                environ["SHOPAGENT_UPSTAGE_ALLOW_HTTP"] = allow
            for call in (assert_upstage_remote_allowed, lambda env: post_systemone({}, env)):
                with self.subTest(allow=allow, call=call), self.assertRaisesRegex(RuntimeError, "Live Upstage HTTP is not enabled"):
                    call(environ)

    def test_explicit_allow_still_has_no_http_client(self):
        environ = {"UPSTAGE_API_KEY": "mock-only-key", "SHOPAGENT_UPSTAGE_ALLOW_HTTP": "1"}
        self.assertIsNone(assert_upstage_remote_allowed(environ))
        with self.assertRaisesRegex(RuntimeError, "Upstage HTTP client not implemented"):
            post_systemone({}, environ)
