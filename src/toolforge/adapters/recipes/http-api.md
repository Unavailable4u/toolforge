# HTTP API recipe

Use this recipe when the agent needs to call HTTP APIs.

## Quick example

A tool that fetches weather data:

```json
{
  "name": "get_weather",
  "description": "Fetch current weather for a location",
  "kind": "http-api",
  "manifest": {
    "permissions": {
      "network": true,
      "hosts": ["api.openweathermap.org"]
    }
  },
  "tool": {
    "input_schema": {
      "type": "object",
      "properties": {
        "city": {
          "type": "string",
          "description": "City name"
        },
        "country_code": {
          "type": "string",
          "description": "Two-letter country code",
          "default": "US"
        }
      },
      "required": ["city"]
    },
    "request": {
      "method": "GET",
      "url": "https://api.openweathermap.org/data/2.5/weather",
      "query_params": {
        "q": "{{city}},{{country_code}}",
        "appid": "{{OWM_API_KEY}}",
        "units": "metric"
      }
    },
    "response": {
      "parse_as": "json",
      "extract": {
        "temperature": "$.main.temp",
        "humidity": "$.main.humidity",
        "description": "$.weather[0].description",
        "city": "$.name"
      }
    }
  },
  "test": {
    "mock": {
      "request": {
        "method": "GET",
        "url": "https://api.openweathermap.org/data/2.5/weather",
        "query_params": {
          "q": "London,UK",
          "appid": "{{OWM_API_KEY}}",
          "units": "metric"
        }
      },
      "response": {
        "status": 200,
        "body": {
          "main": {"temp": 15.5, "humidity": 65},
          "weather": [{"description": "light rain"}],
          "name": "London"
        }
      }
    },
    "live": {
      "smoke": true,
      "rate_limit": "1/second"
    }
  }
}
```

## Authentication

Never read API keys from the environment. Instead, use **brokered secrets**:

1. Register the secret name (e.g., `OWM_API_KEY`) with the ToolForge server.
2. Reference it as `{{SECRET_NAME}}` in the request.
3. The server injects the actual value at runtime.

## Request specification

The `request` object defines the HTTP call:

* `method`: GET, POST, PUT, DELETE, etc.
* `url`: Full URL with protocol.
* `headers`: Optional headers.
* `query_params`: URL query parameters.
* `body`: Request body (for POST/PUT).
* `timeout`: Request timeout in seconds (default: 30).

## Response handling

The `response` object defines how to process the HTTP response:

* `parse_as`: `"json"`, `"text"`, `"xml"`, or `"binary"`.
* `extract`: JSONPath expressions to pull fields from the response.
* `validate`: JSON Schema to validate the response structure.
* `error_on_status`: List of HTTP status codes that should raise an error.

## Host permissions

Every host the tool may contact must be listed in `manifest.permissions.hosts`. Wildcards (`*.example.com`) are allowed but limited.

## Constraints

* Maximum response size: 10 MB.
* Timeout: 30 seconds per request.
* Rate limiting: Tools are limited to 10 requests per minute.
* Only HTTPS is allowed (except for `localhost` in tests).

## Writing tests

Two kinds of tests:

1. **Mock tests**: Provide a canned request/response pair. These run offline and verify parsing logic.
2. **Live smoke tests**: Marked with `"live": {"smoke": true}`. These make one real request (with rate limiting) to verify the API still works.

## Error handling

HTTP errors produce a `ToolError` with code `"http_error"`. Network timeouts use code `"timeout"`. Invalid responses use `"parse_error"`.

## Security notes

* All requests go through a proxy that logs and audits traffic.
* Secrets are never exposed to the tool code.
* Hosts are whitelisted; unexpected domains are blocked.
* Request/response sizes are capped.
* The sandbox prevents direct network access.