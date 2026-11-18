import os
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessageChunk, HumanMessage

from shop.agents import Agents, LocalLLM, handoff
from shop.servers import CONFIGS
from shop.upstage import get_upstage_api_key, require_upstage_api_key


class UpstageKeyTests(unittest.TestCase):
    def test_missing_or_blank_key(self):
        for environ in ({}, {"UPSTAGE_API_KEY": ""}, {"UPSTAGE_API_KEY": " \t\n"}):
            with self.subTest(environ=environ):
                self.assertIsNone(get_upstage_api_key(environ))
                with self.assertRaisesRegex(RuntimeError, "UPSTAGE_API_KEY is missing/empty"):
                    require_upstage_api_key(environ)

    def test_nonempty_key_is_stripped(self):
        environ = {"UPSTAGE_API_KEY": " test-key \n"}
        self.assertEqual(get_upstage_api_key(environ), "test-key")
        self.assertEqual(require_upstage_api_key(environ), "test-key")

    def test_default_environment(self):
        with patch.dict(os.environ, {"UPSTAGE_API_KEY": " test-key "}, clear=True):
            self.assertEqual(get_upstage_api_key(), "test-key")
            self.assertEqual(require_upstage_api_key(), "test-key")
            self.assertIsNone(get_upstage_api_key({}))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "UPSTAGE_API_KEY is missing/empty"):
                require_upstage_api_key()


class ChatClientTests(unittest.TestCase):
    def test_remote_client_options(self):
        with patch.dict(os.environ, {"UPSTAGE_API_KEY": "test-key"}, clear=True), \
                patch("shop.agents.ChatOpenAI") as chat, patch("shop.agents.httpx.Client") as http:
            llm = LocalLLM(timeout=77, config=CONFIGS["solar-pro4"])
            http.assert_called_once_with()
            chat.assert_called_once_with(base_url="https://api.upstage.ai/v1", api_key="test-key",
                                         model="solar-pro4", temperature=0, max_tokens=1024,
                                         timeout=77, max_retries=0, stream_usage=True,
                                         http_client=http.return_value)
            self.assertNotIn("extra_body", chat.call_args.kwargs)
            llm.close()
            chat.return_value.http_client.close.assert_called_once_with()

    def test_remote_client_refuses_key_before_creating_clients(self):
        for key in (None, "", " \t"):
            environ = {} if key is None else {"UPSTAGE_API_KEY": key}
            with self.subTest(key=key), patch.dict(os.environ, environ, clear=True), \
                    patch("shop.agents.ChatOpenAI") as chat, patch("shop.agents.httpx.Client") as http:
                with self.assertRaisesRegex(RuntimeError, "UPSTAGE_API_KEY is missing/empty"):
                    LocalLLM(config=CONFIGS["solar-pro4"])
                chat.assert_not_called()
                http.assert_not_called()

    def test_local_client_options_unchanged(self):
        for config in (None, *(value for value in CONFIGS.values() if value["backend"] != "remote_api")):
            with self.subTest(config=config), patch("shop.agents.ChatOpenAI") as chat, \
                    patch("shop.agents.httpx.Client") as http:
                LocalLLM(18380, timeout=77, config=config)
                http.assert_called_once_with(trust_env=False)
                chat.assert_called_once_with(base_url="http://127.0.0.1:18380/v1", api_key="local",
                                             model="local", temperature=0, max_tokens=1024,
                                             timeout=77, max_retries=0, stream_usage=True,
                                             http_client=http.return_value, extra_body={"cache_prompt": False})

    def test_remote_stream_records_usage_and_model_for_all_nodes(self):
        with patch.dict(os.environ, {"UPSTAGE_API_KEY": "test-key"}, clear=True), \
                patch("shop.agents.ChatOpenAI") as chat:
            chat.return_value.model_name = "solar-pro4"
            chat.return_value.bind_tools.return_value = chat.return_value
            chunk = AIMessageChunk(content="안녕하세요.", usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
                                   response_metadata={"finish_reason": "stop", "model_name": "solar-pro4"})
            chat.return_value.stream.side_effect = lambda messages: iter([chunk])
            llm = LocalLLM(config=CONFIGS["solar-pro4"], http_client=Mock())
            agents = Agents(llm, "C01")
            for node in ("supervisor", "product", "order_delivery", "refund_exchange", "payment", "qna", "human",
                         "consent_judge", "simulated_customer"):
                with self.subTest(node=node):
                    answer, timing = agents.call([HumanMessage(content="인사")], [handoff("transfer_to_product")], node)
                    self.assertEqual(answer.content, "안녕하세요.")
                    self.assertEqual(timing["model"], "solar-pro4")
                    self.assertEqual(timing["tokens"], {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13})
                    self.assertEqual(timing["request"]["model"], "solar-pro4")
                    self.assertNotIn("cache_prompt", timing["request"])
                    self.assertNotIn("test-key", str(timing))
                    options = chat.return_value.bind_tools.call_args.kwargs
                    self.assertEqual(options["tool_choice"], "required" if node in ("supervisor", "consent_judge", "simulated_customer") else "auto")
                    self.assertFalse(options["parallel_tool_calls"])
