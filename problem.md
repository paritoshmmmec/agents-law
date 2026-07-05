The Problem Statement
In autonomous, multi-agent workflows, the execution layer is highly vulnerable to the probabilistic nature of LLMs. When an agent is granted access to sensitive tools—like updating a system of record, issuing a refund, or triggering a marketing email sequence—standard authorization models (like RBAC) are insufficient.

Standard authorization verifies if the agent has permission to use a tool, but it completely ignores why the agent is using it. If an agent hallucinates a user's request, relies on stale context, or resolves conflicting data incorrectly, it will still generate a perfectly formatted, authorized API payload. The result is a high-confidence execution of a bad business decision.

The core problem is: We lack a verifiable, deterministic boundary that forces an agent to prove its reasoning against ground-truth evidence before a state-changing action is committed.

The Design Prompt (System Requirements)
To solve this, the architecture must fulfill the following design constraints:

1. Separation of Concerns (Reasoning vs. Enforcement)
The agent is responsible for orchestration, tool selection, and compiling the payload. It is not allowed to self-evaluate its own readiness to execute. The enforcement layer must be entirely decoupled from the LLM runtime and operate using strict, non-probabilistic logic.

2. Mandatory Context Lineage
The system must reject any tool execution attempt that lacks an "Evidence Manifest." The agent must be forced to provide a strict mapping of the facts it used—including source IDs, timestamps, and confidence signals—alongside its proposed action payload.

3. Deterministic Policy Evaluation
The rules governing whether an action is allowed must be explicitly defined (e.g., "A marketing workflow cannot be triggered unless the user's opt-in timestamp is verified and less than 12 months old"). The gate must evaluate the agent's Evidence Manifest against these rules deterministically.

4. Fail-Safe and Human-in-the-Loop Routing
If evidence is missing, stale, or conflicting, the system must not fail silently or drop the context. It must explicitly block the action or route the assembled context to a human reviewer (or a separate evaluation agent) without breaking the primary agent's execution loop.

5. Complete Telemetry and Observability
Every interaction at the gate—the proposed payload, the provided evidence, the specific policy rule triggered, and the final routing decision—must be recorded. This is critical for evaluating agentic loops and discovering edge cases in production.