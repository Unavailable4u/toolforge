# Browser‑plan recipe

Use this recipe when the agent needs to interact with web pages.

## Quick example

A tool that extracts headlines from a news site:

```json
{
  "name": "browser_headlines",
  "description": "Extract today's headlines from a news website",
  "kind": "browser-plan",
  "manifest": {
    "permissions": {
      "browser": true,
      "hosts": ["news.example.com"]
    }
  },
  "tool": {
    "input_schema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "News category",
          "enum": ["politics", "business", "technology", "sports"],
          "default": "technology"
        }
      }
    },
    "plan": [
      {
        "step": "goto",
        "url": "https://news.example.com/{{category}}"
      },
      {
        "step": "wait_for",
        "selector": ".headline-list",
        "timeout": 10
      },
      {
        "step": "extract_text",
        "selector": ".headline-item .title",
        "as": "headlines",
        "limit": 10
      },
      {
        "step": "extract_attr",
        "selector": ".headline-item a",
        "attr": "href",
        "as": "links"
      }
    ]
  },
  "test": {
    "fixture": {
      "url": "https://news.example.com/technology",
      "html": "<div class='headline-list'><div class='headline-item'><a href='/article/1'><span class='title'>AI Breakthrough</span></a></div></div>"
    },
    "expected": {
      "headlines": ["AI Breakthrough"],
      "links": ["/article/1"]
    }
  }
}
```

## Plan vocabulary

A plan is a JSON array of steps. Each step has a `"step"` field identifying the action:

* **`goto`**: Navigate to a URL.
  * `url`: The URL to visit (supports `{{param}}` templating).
  * `wait_until`: Optional, `"load"` (default) or `"domcontentloaded"`.

* **`click`**: Click an element.
  * `selector`: CSS selector.
  * `wait_after`: Optional seconds to wait after clicking.

* **`type`**: Type text into an input.
  * `selector`: CSS selector of the input element.
  * `text`: Text to type (supports `{{param}}` templating).

* **`wait_for`**: Wait for an element to appear.
  * `selector`: CSS selector.
  * `timeout`: Maximum seconds to wait.

* **`extract_text`**: Extract text content from elements.
  * `selector`: CSS selector.
  * `as`: Output field name.
  * `limit`: Maximum number of elements to extract.

* **`extract_attr`**: Extract attribute values from elements.
  * `selector`: CSS selector.
  * `attr`: Attribute name (e.g., `"href"`, `"src"`).
  * `as`: Output field name.
  * `limit`: Maximum number of elements to extract.

* **`screenshot`**: Take a screenshot.
  * `as`: Output field name (image data as base64).
  * `selector`: Optional, screenshot only this element.
  * `full_page`: Optional boolean.

## Template parameters

Use `{{param}}` in `url` or `text` fields to insert tool input values. The parameter must be defined in the `input_schema`.

## Host permissions

Every host visited must be listed in `manifest.permissions.hosts`. Wildcards are not allowed for browser plans.

## Constraints

* Maximum plan length: 20 steps.
* Maximum execution time: 60 seconds.
* Maximum pages per plan: 5.
* Screenshots limited to 1 per plan.
* Extracted text limited to 10 KB per field.

## Writing tests

Browser‑plan tests use a **fixture server**:

1. Define the HTML that the fixture server will return.
2. Specify the expected extracted data.
3. The test runs the plan against the fixture (no real network calls).

## Error handling

Plan execution errors produce a `ToolError` with codes:

* `"browser_timeout"`: A `wait_for` timed out.
* `"selector_not_found"`: Element not found.
* `"navigation_error"`: Page failed to load.
* `"plan_error"`: Invalid plan step.

## Security notes

* Plans run in MiniLocker's `BrowserSandbox` (isolated browser instance).
* No JavaScript execution (except what's needed for page rendering).
* All extracted text passes through `textsafety` (same as MiniLocker).
* Screenshots are PNG images, base64‑encoded.
* Network access is limited to approved hosts only.