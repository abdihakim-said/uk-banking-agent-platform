// Demo chat client. Talks to the gateway through the same-origin proxy (/api) and the demo IdP (/auth).
// All server text is inserted with textContent, never innerHTML.

const ROUTE_LABELS = {
  accounts: "Accounts agent", transactions: "Transactions agent", service: "Service agent", general: "Assistant",
  out_of_scope: "Out of scope", injection: "Blocked", regulated_advice: "Regulated advice",
  human_handoff: "Human handoff", none: "System",
};
const PII_LABELS = {
  ACCOUNT_NUMBER: "account number", SORT_CODE: "sort code", CARD_NUMBER: "card number", IBAN: "IBAN",
  NI_NUMBER: "National Insurance number", EMAIL: "email", UK_PHONE: "phone number", POSTCODE: "postcode",
  ADDRESS: "address", PERSON_NAME: "name",
};
const GUARD_ROUTES = new Set(["injection", "regulated_advice", "out_of_scope"]);
const SUGGESTIONS = [
  "What's my balance?", "Show my recent Tesco payments", "How much did I spend last month?",
  "I don't recognise the Netflix payment, please dispute it", "My card was stolen",
  "Change my address to 22 King Street, London, SW1A 1AA", "Should I invest in crypto?", "I want to make a complaint",
];

const state = { token: null, persona: null, sessionId: null, busy: false };
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

async function loadPersonas() {
  const res = await fetch("/auth/personas");
  const personas = await res.json();
  const nav = $("personas");
  for (const p of personas) {
    const btn = el("button", { type: "button", "data-testid": `persona-${p.given_name.toLowerCase()}`,
                               title: p.description }, `${p.given_name} ${p.family_name}`);
    btn.addEventListener("click", () => signIn(p.customer_id, btn));
    nav.append(btn);
  }
}

async function signIn(customerId, button) {
  const res = await fetch("/auth/token", { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify({ customer_id: customerId }) });
  if (!res.ok) return addSystem("Sign-in failed.");
  const data = await res.json();
  Object.assign(state, { token: data.access_token, persona: data.persona, sessionId: null });
  document.querySelectorAll("#personas button").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
  $("signed-out").hidden = true;
  for (const id of ["messages", "suggestions", "composer"]) $(id).hidden = false;
  $("messages").replaceChildren();
  $("fact-user").textContent = `${data.persona.given_name} ${data.persona.family_name} (${customerId})`;
  $("fact-session").textContent = "new";
  $("fact-route").textContent = "—";
  addSystem(`Signed in as ${data.persona.given_name}. New conversation started.`);
  $("input").focus();
}

function addSystem(text) {
  $("messages").append(el("li", { class: "msg system" }, text));
}

function addUser(text) {
  const li = el("li", { class: "msg user" });
  li.append(el("p", {}, text));
  $("messages").append(li);
  return li;
}

function addBot(reply) {
  const li = el("li", { class: "msg bot", "data-testid": "bot-message" });
  const meta = el("div", { class: "meta" });
  meta.append(el("span", { class: "route" }, ROUTE_LABELS[reply.route] ?? reply.route));
  if (reply.handoff) meta.append(el("span", { class: "badge badge-human" }, "Human handoff"));
  if (GUARD_ROUTES.has(reply.route)) meta.append(el("span", { class: "badge badge-guard" }, "Guardrail"));
  if (reply.awaiting_confirmation) meta.append(el("span", { class: "badge badge-confirm" }, "Needs your OK"));
  li.append(meta, el("p", {}, reply.reply));

  if (reply.handoff) {
    li.append(el("div", { class: "banner", "data-testid": "handoff-banner" },
                 "A member of our team will pick this up. You don't need to repeat yourself."));
  }
  if (reply.awaiting_confirmation) {
    const pending = reply.pending_action;
    const needsCode = Boolean(pending && pending.sca_required);
    if (needsCode) meta.append(el("span", { class: "badge badge-sca" }, "Needs a one-time code"));
    const actions = el("div", { class: "actions" });
    const yes = el("button", { type: "button", class: "primary", "data-testid": "confirm-yes" },
                   needsCode ? "Approve with a one-time code" : "Yes, go ahead");
    const no = el("button", { type: "button", "data-testid": "confirm-no" }, "No, cancel");
    yes.addEventListener("click", () => {
      yes.disabled = no.disabled = true;
      if (needsCode) startSca(pending, li); else send("yes");
    });
    no.addEventListener("click", () => { yes.disabled = no.disabled = true; send("no"); });
    actions.append(yes, no);
    li.append(actions);
  }
  $("messages").append(li);
}

// Strong customer authentication: the IdP "texts" a one-time code to the registered phone; a correct code is exchanged
// for a short-lived step-up token bound to this exact action, sent with the "yes".
async function startSca(pending, li) {
  const res = await fetch("/auth/sca/challenge", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.token}` },
    body: JSON.stringify({ txn: pending.binding }),
  });
  if (!res.ok) return addSystem("Couldn't start the security check. Please try again.");
  const challenge = await res.json();
  showPhone(challenge.delivered_to, challenge.demo_code);

  const form = el("form", { class: "sca", "data-testid": "sca-form" });
  const id = `otp-${challenge.challenge_id.slice(0, 8)}`;
  const input = el("input", { id, inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6",
                              pattern: "\\d{6}", required: "", "data-testid": "sca-code" });
  const submit = el("button", { type: "submit", class: "primary", "data-testid": "sca-submit" }, "Approve");
  const message = el("p", { class: "sca-msg", "data-testid": "sca-msg" });
  form.append(el("label", { for: id }, `Enter the 6-digit code sent to your ${challenge.delivered_to}`),
              el("div", { class: "sca-row" }), message);
  form.querySelector(".sca-row").append(input, submit);
  li.append(form);
  input.focus();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const verify = await fetch("/auth/sca/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ challenge_id: challenge.challenge_id, code: input.value.trim() }),
    });
    const data = await verify.json().catch(() => ({}));
    if (!verify.ok) {
      message.textContent = data.detail || "That code didn't work.";
      if (/start again/.test(message.textContent)) input.disabled = submit.disabled = true;
      return;
    }
    input.disabled = submit.disabled = true;
    message.textContent = "Approved.";
    hidePhone();
    send("yes", data.step_up_token);
  });
}

function showPhone(deliveredTo, code) {
  hidePhone();
  const phone = el("aside", { class: "phone", "data-testid": "demo-phone", "aria-live": "polite" });
  phone.append(el("p", { class: "phone-label" }, `Demo · SMS to your ${deliveredTo}`),
               el("p", {}, "Your Bank code to approve this change is"),
               el("p", { class: "phone-code", "data-testid": "demo-otp" }, code),
               el("p", { class: "phone-small" }, "We'll never call you to ask for this code."));
  document.body.append(phone);
}

function hidePhone() {
  document.querySelector(".phone")?.remove();
}

function markRedactions(userLi, kinds) {
  if (!kinds.length) return;
  const names = kinds.map((k) => PII_LABELS[k] ?? k.toLowerCase());
  userLi.append(el("span", { class: "badge badge-pii", "data-testid": "pii-badge",
                             title: "Replaced with a placeholder before any AI model saw this message" },
                   `Hidden from AI: ${names.join(", ")}`));
}

async function send(text, scaToken) {
  if (state.busy || !state.token) return;
  state.busy = true;
  $("send").disabled = true;
  document.querySelectorAll(".actions button").forEach((b) => (b.disabled = true));
  const userLi = addUser(text);
  const typing = el("li", { class: "msg bot typing", "aria-label": "Assistant is replying" }, "…");
  $("messages").append(typing);
  typing.scrollIntoView({ block: "end" });
  try {
    const res = await fetch("/api/v1/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.token}`,
                 ...(scaToken ? { "X-SCA-Token": scaToken } : {}) },
      body: JSON.stringify(state.sessionId ? { message: text, session_id: state.sessionId } : { message: text }),
    });
    typing.remove();
    if (res.status === 429) return addSystem("You're sending messages too quickly. Please wait a moment.");
    if (res.status === 401) return addSystem("Your session expired. Please sign in again.");
    if (!res.ok) return addSystem(`Something went wrong (HTTP ${res.status}). Please try again.`);
    const reply = await res.json();
    state.sessionId = reply.session_id;
    $("fact-session").textContent = `${reply.session_id.slice(0, 8)}…`;
    $("fact-route").textContent = ROUTE_LABELS[reply.route] ?? reply.route;
    markRedactions(userLi, reply.redacted ?? []);
    addBot(reply);
  } catch {
    typing.remove();
    addSystem("Couldn't reach the assistant. Is the stack running?");
  } finally {
    state.busy = false;
    $("send").disabled = false;
    $("messages").lastElementChild?.scrollIntoView({ block: "end" });
  }
}

$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  $("input").value = "";
  send(text);
});

for (const s of SUGGESTIONS) {
  const chip = el("button", { type: "button", class: "chip" }, s);
  chip.addEventListener("click", () => send(s));
  $("suggestions").append(chip);
}

loadPersonas().catch(() => addSystem("Couldn't load test customers. Is the demo IdP running?"));
