"""Exercise the production HTTP client constructor without upstream traffic."""

import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity.bump_responses_adapter import NamespaceResponsesAdapter


class DefaultHttpClientCompatibilityTests(unittest.TestCase):
    def test_default_client_constructs_and_closes_without_network_requests(self):
        agent = SimpleNamespace(
            backend="codex", model="glm-5.3", provider="openai",
            base_url="https://freeinference.org/v1", api_key="fixture-not-a-real-key",
        )
        # The adapter may bind a loopback listener, but no outgoing connection,
        # DNS lookup, model request or service request is permitted by this test.
        with patch.object(socket.socket, "connect", side_effect=AssertionError("outgoing connection")), \
                patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS lookup")):
            with NamespaceResponsesAdapter(agent) as adapter:
                client = adapter._client
                self.assertEqual(client.timeout.connect, 20)
                self.assertEqual(client.timeout.read, 600)
                self.assertEqual(client.timeout.write, 60)
                self.assertEqual(client.timeout.pool, 30)
                self.assertFalse(client.follow_redirects)
                self.assertFalse(client.trust_env)
                self.assertEqual(adapter.request_count, 0)
                self.assertFalse(client.is_closed)
            self.assertTrue(client.is_closed)


if __name__ == "__main__":
    unittest.main()
