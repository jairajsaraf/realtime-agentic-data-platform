# 3. LangChain `create_agent` with deterministic provenance and budgets

Status: Accepted. Supersedes the earlier "no model SDK" decision for the agent loop.

Date: 2026-10-27

## Context

The Stage D agent started as a hand-written tool-calling loop over `httpx`, with its own
OpenAI-compatible client, message types, and fake model. A header in the old client said no model
SDK was used or required.

That loop reimplemented what agent frameworks already provide: model and tool-call limits, hooks
around each model and tool call, and tool schemas. The change moves the agent onto a widely used
framework while keeping the project's own guarantees: read-only tools, citations that cannot be
lost, and explicit budgets.

## Decision

- Run the agent on `langchain.agents.create_agent`, with `ChatOpenAI` against any OpenAI-compatible
  endpoint, configured by the unchanged `RTDP_AGENT_*` settings.
- Keep the guarantees in the project's own code, not in the model's text:
  - The `Sources:` line is built from the `ToolResult` artifacts attached to the tool messages.
  - `max_turns` is enforced with `ModelCallLimitMiddleware` and `max_tool_calls` with
    `ToolCallLimitMiddleware`, plus a small hook that stops the run once a call has been blocked.
    The built-in middleware alone does not reproduce the old behaviour: in `"end"` mode it runs none
    of an over-budget batch, and in `"continue"` mode it gives the model another turn.
  - Malformed tool-call JSON degrades to `{}` so the executor's own validation answers, unknown
    tool names go to the executor so they are recorded, and tool calls run one at a time in the
    order the model gave them.
- Pin the behaviour before changing it. Golden transcripts were recorded against the hand-written
  loop, and they must pass unchanged on the new one. Disabling any one of the hooks fails at least
  one of them.
- Ship LangChain as the optional `agent` extra, with `rtdp agent` printing an install hint without
  it. The default install set is unchanged. A one-week cooldown in `pyproject.toml` holds the
  fast-moving LangChain and OpenAI packages to versions that have been public for a while.
- Hand each tool call's `extra_content` back unchanged on the next request, through a small
  `ChatOpenAI` subclass. Gemini 3 models return a thought signature there and reject replayed tool
  calls without it. The response shape comes from community reports, not from the vendor's
  documentation, so the recorded eval runs are what show whether it works.

## Consequences

- The hand-written client, message types, and fake model are gone, and the framework supplies the
  tool-calling loop and the limit middleware. The code did not get smaller overall: the loop file
  is longer than before, because the hooks that keep the old behaviour are project code. The costs
  are the LangChain and OpenAI package tree in an optional extra, and reliance on two private
  `ChatOpenAI` methods in the subclass, covered by tests and held by the lockfile.
- A few behaviours changed in ways callers can see (exception types from the model endpoint, the
  shape of the `on_event` payload, a malformed call now running after the well-formed calls of the
  same response). They are listed in the pull request that made the change.
- The framework adds failure modes the old client did not have, and the first two live eval runs
  hit integration problems, not agent behaviour: a missing thought signature, then a model no
  longer available to new users. Both are recorded, with the changes made in response, as
  addenda to the eval preregistration.
