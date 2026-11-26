from unittest.mock import patch, MagicMock
from tool import ToolError, run


def test_fetch_json_success():
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"status": "ok", "items": [1, 2, 3]}'
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = run({"url": "https://api.example.com/data"})
        assert result == {"status": "ok", "data": {"status": "ok", "items": [1, 2, 3]}}


def test_fetch_json_with_custom_method_and_headers():
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"result": "success"}'
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = mock_response
        result = run({
            "url": "https://api.example.com/data",
            "method": "POST",
            "headers": {"Authorization": "Bearer token"}
        })
        assert result == {"status": "ok", "data": {"result": "success"}}

        # Verify the request was made with correct parameters
        call_args = mock_urlopen.call_args[0][0]
        assert call_args.get_method() == "POST"
        assert call_args.headers.get("Authorization") == "Bearer token"


def test_http_error_raises_tool_error():
    from urllib.error import HTTPError

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = HTTPError(
            url="https://api.example.com/data",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None
        )
        try:
            run({"url": "https://api.example.com/data"})
            assert False, "Expected ToolError"
        except ToolError as e:
            assert "HTTP error 404" in str(e)


def test_network_error_raises_tool_error():
    from urllib.error import URLError

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = URLError("Connection refused")
        try:
            run({"url": "https://api.example.com/data"})
            assert False, "Expected ToolError"
        except ToolError as e:
            assert "Network error" in str(e)


def test_invalid_json_raises_tool_error():
    import json
    from io import BytesIO

    mock_response = MagicMock()
    mock_response.read.return_value = b'not valid json'
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)

    with patch("urllib.request.urlopen", return_value=mock_response):
        try:
            run({"url": "https://api.example.com/data"})
            assert False, "Expected ToolError"
        except ToolError as e:
            assert "Invalid JSON" in str(e)