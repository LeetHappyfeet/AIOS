# Inference workers: reasoning providers and completion budgets

In Gradio's **Inference Workers** tab, select a worker by name under **Worker controls**.
Enable, disable, drain, resume, remove and health-check act on that selection. **Load
selected worker into editor** loads its settings; expand **Add / update worker** to
edit them. Save with the same worker key to update it. A blank API key preserves
the saved key, and saved keys are never displayed in the editor. Saving settings
preserves an existing worker's enabled/drained state. Refresh retains the selection
if it still exists; removing a worker clears it.

## DeepSeek

Use the DeepSeek preset, your API model identifier and API key. Set **Model thinking /
reasoning** to **Enabled**, or **Provider default** to leave the switch to the service.
**Reasoning API format = auto** recognizes `api.deepseek.com`, including existing
worker records. It sends the native `thinking: {type: enabled/disabled}` field.
Optional effort is sent as `reasoning_effort`. Local `enable_thinking` and
`chat_template_kwargs` fields are not sent to the direct DeepSeek API.

The model identifier and supported effort values depend on the service/model.
See the [DeepSeek thinking documentation](https://api-docs.deepseek.com/guides/thinking_mode/).

## Other OpenAI-compatible services

| Reasoning API format | Behavior |
| --- | --- |
| auto | Detects direct DeepSeek and OpenAI hostnames; otherwise retains local-server behavior for compatibility. |
| deepseek | Native DeepSeek thinking fields, including when using a proxy. |
| openai | Optional `reasoning_effort`; recognized reasoning models use `max_completion_tokens` and omit temperature. The thinking switch is not sent. |
| local | Sends `enable_thinking` and `chat_template_kwargs.enable_thinking` when enabled or disabled is selected. |
| generic | Sends no reasoning-specific fields. Useful for proxies with their own defaults. |

**Completion token parameter** can explicitly select `max_tokens` or
`max_completion_tokens` when auto detection is unsuitable. **Supports response_format
json_object** controls JSON mode independently; AIOS still validates the returned
JSON when JSON mode is disabled. Bounded choice requests never enable JSON mode.

## Completion budget and retries

The context window and completion budget are separate settings. A provider may
count reasoning and final JSON against the same completion budget.

- **Completion budget = 0** uses the request budget, raised to at least 8192 for
  enabled reasoning, recognized reasoning model names, or direct DeepSeek's default
  thinking mode. Ordinary goal planning requests now allow 1200 tokens.
- A positive **Completion budget** overrides that automatic budget for this worker.
- **Completion budget ceiling = 0** allows at least 16384 tokens (or the initial
  budget if larger). A positive ceiling bounds both the initial call and retry.
- With **Retry truncated completion once** enabled, `finish_reason=length` causes
  one fresh request with twice the budget, capped by the ceiling. Both calls share
  the worker's inference deadline. Retries incur additional usage; disable them
  for a single-call budget. Respect the model's supported token/context limits.

AIOS never accepts a truncated completion as a plan, even if its text happens to
parse. Empty/null final content is also rejected. Incomplete completions do not
increment worker health or protocol-failure counters; the broker may try another
available worker. A final malformed JSON response still counts as a protocol error.

The existing `inference_request.response_json` stores diagnostic counts under
`_aios_completion`, including each attempt's budget, finish reason and token usage
when supplied by the provider. Partial final content is retained in `response_text`
on incomplete results. Reasoning traces are not stored. The returned plan payload
is unchanged. These settings use the existing capabilities JSON; no migration is
required. A health check tests endpoint reachability, not a planning completion.
