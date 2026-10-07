# UK regulatory mapping

This maps the platform's controls to UK obligations that a bank deploying an AI assistant would have to meet. It
shows what the code supports and what the firm would still own. It is not legal advice, and using this code does
not make a firm compliant with anything.

## Summary

| Area | Obligation | What the platform does | What the firm still needs |
|---|---|---|---|
| Data protection | UK GDPR Art. 5(1)(c) data minimisation; Art. 25 data protection by design | PII is pseudonymised at the gateway before any model call. Tool results are redacted again before the model reads them. Logs and traces carry ids and sizes, not text. | A DPIA (Art. 35). Records of processing. Retention policy for history and vault (TTL here is 30 minutes). |
| Processors and transfers | UK GDPR Art. 28; Chapter V international transfers | Model calls are isolated in one provider module, and a self-hosted model can be first in the route for data that must stay in-house. | Data processing agreement with each model provider. Transfer mechanism (UK data bridge or IDTA) and a transfer risk assessment. Confirm the provider's retention settings. |
| Consumer Duty | PRIN 2A: avoid foreseeable harm, support customer understanding, act in good faith | Figures must trace to tool results. State-changing actions are described from the server's validated preview and need explicit confirmation. Plain-English, short replies. | Outcome testing and monitoring (MI on journeys, complaints, handoffs). Board-level Consumer Duty reporting that covers the assistant. |
| Vulnerable customers | FCA FG21/1 | Signs of bereavement, financial difficulty, scams, self-harm or a request for a person route straight to a human, without a model call. | Trained staff behind the handoff, a vulnerability flag workflow, and review of the trigger list with the vulnerable-customers team. |
| Financial promotions and advice | FSMA s.19/s.21; COBS (regulated advice) | Requests for investment, pension, mortgage or insurance advice are declined and signposted to MoneyHelper. An output guard blocks recommendation phrasing. | Compliance sign-off of the wording, and a periodic sample review of conversations. |
| Complaints | DISP 1 | "Complaint" triggers a human handoff. | Logging the expression of dissatisfaction as a complaint, with DISP time limits, in the complaints system. |
| Payment disputes | Payment Services Regulations 2017 (reg. 74: 13-month notification limit for unauthorised transactions) | Disputes on debits older than 13 months are refused at the server. Disputes need confirmation and get a reference. | Chargeback and refund process, including the D+1 refund rule for unauthorised transactions (reg. 76). |
| Strong customer authentication | PSRs 2017 reg. 100; SCA-RTS | Not implemented. Confirmation is a typed "yes" inside an authenticated session. | Step-up SCA (for example in-app approval) for card reissue, contact detail changes and anything payment-related. |
| Operational resilience | FCA PS21/3 / PRA SS1/21 (important business services, impact tolerances) | Provider fallback with circuit breakers. Graceful "unavailable" replies. Budget limits. Alerts on fallback, tool errors and spend. | Mapping the assistant to important business services, setting impact tolerances, and scenario testing (for example a model provider outage). |
| Outsourcing and third parties | SYSC 8; PRA SS2/21 | The model provider is behind one interface and can be swapped or run self-hosted. | Material outsourcing assessment, contract terms (audit, access, exit), and an exit plan. |
| Model risk | PRA SS1/23 model risk management principles | Evaluation suite with thresholds in CI. Live-model eval on demand. Every turn's route, tools and provider are recorded. | Model inventory entry, independent validation, ongoing performance monitoring against the eval set, and a change process for prompts, models and effort settings. |
| Security | SYSC 3/4 systems and controls; NCSC guidance | OIDC with issuer, audience, expiry and scope checks. Per-customer and per-IP rate limits. Session ownership and locking. Least-privilege tools per agent. Signed customer context on every tool call. **Mutual TLS on every internal hop; Redis with TLS, client certificates and a password.** Non-root, read-only containers. | Penetration test, threat model, HSM-backed secrets, WAF/DDoS at the edge, egress filtering by hostname. |
| Record keeping | SYSC 9 (records); DISP; UK GDPR Art. 5(2) accountability | **Tamper-evident audit log**: every sign-in failure, refused session, guardrail block and customer-confirmed action, HMAC-chained so edits and deletions are detectable. No message text or personal data values. | Shipping to write-once storage for the required retention period, and access controls on who can read it. |

## Open Banking

The MCP servers are where back-end integrations plug in. An Open Banking adapter (UK Open Banking Read/Write API,
with FAPI security profile for the client) could sit behind the same tool interfaces. That adapter isn't included,
so the platform is not "Open Banking ready" in any certified sense.

## What leaves the bank's boundary

With Claude as the first provider, these go to the model provider per call:

- the system prompt for that agent
- redacted conversation history (placeholders, not values)
- redacted tool results (balances, merchants, dates and amounts are not personal identifiers on their own, but they
  are personal data in context, so this still needs the Art. 28 and transfer controls above)

Placeholders never leave, and neither do the vault mapping, context tokens or customer ids. If a bank decides that no
account data may leave at all, put `private` first in `BANK_LLM_ROUTE` and remove `anthropic`. The eval suite should
then be run against that model before going live, because small self-hosted models are much weaker at tool use.
