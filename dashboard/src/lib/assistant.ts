/**
 * The assistant client — one implementation, two surfaces.
 *
 * The floating bubble and the /assistant page are the *same conversation*. That is
 * only true if there is one source of truth, so this module owns the session and both
 * surfaces are thin renderers of it: every mutation writes to `localStorage` and
 * notifies every mount, which re-renders. Two copies of the logic would drift the
 * first time one was edited, and a user who asked something in the bubble then opened
 * the page would find their question missing.
 *
 * Turns are stored as **data, never as HTML**. Rendering happens through `textContent`
 * on every path — answers quote job descriptions and channel messages written by
 * strangers, and `innerHTML` on that would be a cross-site scripting hole with extra
 * steps.
 */

export type Pending = {
  nonce: string;
  tool: string;
  card: string;
  state: "open" | "done" | "failed" | "dismissed";
  result?: string;
};

export type Turn =
  | { kind: "you"; text: string }
  | { kind: "it"; text: string; meta: string; warnings: string[]; pending: Pending[] }
  | { kind: "error"; text: string };

export type Session = { id: string; turns: Turn[]; updatedAt: number };

const KEY = "jobagent_assistant_session";
const UI_KEY = "jobagent_assistant_ui";
export const MAX_TURNS = 100; // a session is a conversation, not an archive

let listeners = new Set<() => void>();
let busy = false;

const newId = () =>
  `s_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;

function blank(): Session {
  return { id: newId(), turns: [], updatedAt: Date.now() };
}

export function loadSession(): Session {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return blank();
    const parsed = JSON.parse(raw);
    if (!parsed || !Array.isArray(parsed.turns)) return blank();
    return parsed as Session;
  } catch {
    return blank(); // corrupt storage must not brick the widget
  }
}

function save(session: Session) {
  session.updatedAt = Date.now();
  if (session.turns.length > MAX_TURNS) {
    session.turns = session.turns.slice(-MAX_TURNS);
  }
  try {
    localStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    /* quota or private mode — the session stays in memory for this page */
  }
  notify();
}

export function clearSession() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
  notify();
}

export function isBusy() {
  return busy;
}

function notify() {
  for (const fn of listeners) fn();
}

export function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// Another tab edited the session. Without this, two open tabs silently diverge and the
// last one to write wins — which looks like losing your conversation.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (e) => {
    if (e.key === KEY || e.key === null) notify();
  });
}

/* --- UI preferences (open state, panel size) -------------------------------------- */

export type UiState = { open: boolean; w: number; h: number };
const DEFAULT_UI: UiState = { open: false, w: 400, h: 560 };

export function loadUi(): UiState {
  try {
    return { ...DEFAULT_UI, ...JSON.parse(localStorage.getItem(UI_KEY) || "{}") };
  } catch {
    return { ...DEFAULT_UI };
  }
}

export function saveUi(patch: Partial<UiState>) {
  try {
    localStorage.setItem(UI_KEY, JSON.stringify({ ...loadUi(), ...patch }));
  } catch {
    /* ignore */
  }
}

/* --- talking to the API ----------------------------------------------------------- */

declare global {
  interface Window {
    JA: { token(): string | null; headers(): Record<string, string>; explain(s: number): string };
  }
}

/**
 * On 401/403 the actionable message must win over the server's terse detail:
 * "Unauthorized." tells you nothing; "open Settings, sign in" tells you what to do.
 * Every other status keeps the server's wording, which is more specific than anything
 * the client could invent.
 */
function problem(status: number, body: any): string {
  return status === 401 || status === 403
    ? window.JA.explain(status)
    : (body && body.detail) || window.JA.explain(status);
}

export async function ask(base: string, question: string, opts: { reindex?: boolean } = {}) {
  const q = question.trim();
  if (!q || busy) return;

  const session = loadSession();
  session.turns.push({ kind: "you", text: q });
  busy = true;
  save(session); // renders your message immediately; the wait is visible, not blank

  try {
    const r = await fetch(`${base}/assistant/ask`, {
      method: "POST",
      headers: window.JA.headers(),
      body: JSON.stringify({ question: q, reindex: !!opts.reindex }),
    });
    const body = await r.json().catch(() => ({}));
    const next = loadSession();

    if (!r.ok) {
      next.turns.push({ kind: "error", text: problem(r.status, body) });
    } else {
      next.turns.push({
        kind: "it",
        text: body.answer ?? "",
        meta: [`${body.provider}/${body.model}`, body.strategy, `${body.elapsed_ms}ms`,
               `run ${body.run_id}`].filter(Boolean).join(" · "),
        // Degradation is surfaced, never hidden: an answer from a weaker model must
        // not look identical to one from the best available.
        warnings: body.warnings || [],
        pending: (body.pending || []).map((p: any) => ({ ...p, state: "open" })),
      });
    }
    busy = false;
    save(next);
  } catch (e) {
    const next = loadSession();
    next.turns.push({ kind: "error", text: `Could not reach the API: ${e}` });
    busy = false;
    save(next);
  }
}

/**
 * Approve one waiting action. The client holds **only the nonce** — the arguments stay
 * server-side, so there is no field here in which they could be swapped between the
 * card being read and the approval being sent.
 */
export async function confirm(base: string, nonce: string) {
  const mark = (state: Pending["state"], result?: string) => {
    const s = loadSession();
    for (const t of s.turns) {
      if (t.kind !== "it") continue;
      for (const p of t.pending) if (p.nonce === nonce) { p.state = state; p.result = result; }
    }
    save(s);
  };

  try {
    const r = await fetch(`${base}/assistant/confirm/${encodeURIComponent(nonce)}`, {
      method: "POST",
      headers: window.JA.headers(),
    });
    const body = await r.json().catch(() => ({}));
    mark(r.ok ? "done" : "failed", r.ok ? body.result : problem(r.status, body));
  } catch (e) {
    mark("failed", `Could not reach the API: ${e}`);
  }
}

export function dismiss(nonce: string) {
  const s = loadSession();
  for (const t of s.turns) {
    if (t.kind !== "it") continue;
    for (const p of t.pending) if (p.nonce === nonce) p.state = "dismissed";
  }
  save(s);
}

/* --- rendering -------------------------------------------------------------------- */

function el(tag: string, cls?: string | null, text?: string) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text; // textContent, never innerHTML
  return n;
}

export const SUGGESTIONS = [
  "Is the pipeline healthy?",
  "What did the last run do?",
  "How much of my catch am I ignoring?",
  "Are any sources stale?",
];

/**
 * Render the whole thread into `root`. Re-rendering everything on each change keeps
 * the two surfaces provably identical — there is no incremental path that one could
 * take and the other miss. A session is tens of turns, so the cost is irrelevant.
 */
export function renderThread(root: HTMLElement, base: string, onAsk: (q: string) => void) {
  const session = loadSession();
  root.replaceChildren();

  if (session.turns.length === 0) {
    const empty = el("div", "ja-empty");
    empty.appendChild(el("p", null, "Ask about your pipeline, queue, or settings."));
    const chips = el("div", "ja-chips");
    for (const s of SUGGESTIONS) {
      const c = el("button", "ja-chip", s);
      c.type = "button";
      c.onclick = () => onAsk(s);
      chips.appendChild(c);
    }
    empty.appendChild(chips);
    root.appendChild(empty);
    return;
  }

  for (const turn of session.turns) {
    const wrap = el("div", "ja-turn");
    if (turn.kind === "you") {
      wrap.appendChild(el("div", "ja-msg ja-you", turn.text));
    } else if (turn.kind === "error") {
      wrap.appendChild(el("div", "ja-msg ja-err", turn.text));
    } else {
      wrap.appendChild(el("div", "ja-msg ja-it", turn.text));
      if (turn.meta) wrap.appendChild(el("div", "ja-meta", turn.meta));
      for (const w of turn.warnings) wrap.appendChild(el("div", "ja-meta ja-warn", `! ${w}`));
      for (const p of turn.pending) wrap.appendChild(confirmCard(p, base));
    }
    root.appendChild(wrap);
  }

  if (busy) {
    const t = el("div", "ja-turn");
    const think = el("div", "ja-thinking");
    think.append(el("span", "ja-dot"), el("span", "ja-dot"), el("span", "ja-dot"));
    t.appendChild(think);
    root.appendChild(t);
  }
}

/** One approval. Card text is server-computed — no model output reaches this markup. */
function confirmCard(p: Pending, base: string) {
  const box = el("div", "ja-confirm");

  if (p.state === "done") return el("div", "ja-done", p.result || "Applied.");
  if (p.state === "failed") return el("div", "ja-meta ja-warn", p.result || "Failed.");
  if (p.state === "dismissed") return el("div", "ja-meta", "Dismissed.");

  box.appendChild(el("h4", null, `${p.tool} needs your approval`));
  box.appendChild(el("pre", null, p.card));
  const row = el("div", "ja-row");
  const yes = el("button", "ja-ok", "Approve") as HTMLButtonElement;
  const no = el("button", null, "Dismiss") as HTMLButtonElement;
  yes.type = no.type = "button";
  yes.onclick = () => {
    yes.disabled = no.disabled = true;
    yes.textContent = "Applying…";
    confirm(base, p.nonce);
  };
  no.onclick = () => dismiss(p.nonce);
  row.append(yes, no);
  box.appendChild(row);
  return box;
}

/**
 * Wire a thread container + composer into a live surface. Returns a teardown function.
 * Both the bubble and the page call exactly this, which is what makes them the same
 * feature rather than two that resemble each other.
 */
export function mount(opts: {
  root: HTMLElement;
  form: HTMLFormElement;
  input: HTMLInputElement;
  send: HTMLButtonElement;
  base: string;
  scroll?: HTMLElement;
}) {
  const { root, form, input, send, base } = opts;
  const scroller = opts.scroll || root;

  const submit = (q: string) => {
    if (!q.trim() || busy) return;
    input.value = "";
    ask(base, q);
  };

  const draw = (stick = true) => {
    const atBottom =
      scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    renderThread(root, base, submit);
    send.disabled = input.disabled = busy;
    // Only auto-scroll if the reader was already at the bottom — yanking someone away
    // from what they were re-reading is the classic chat-widget annoyance.
    if (stick && atBottom) scroller.scrollTop = scroller.scrollHeight;
  };

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    submit(input.value);
    scroller.scrollTop = scroller.scrollHeight;
  });

  const unsubscribe = subscribe(() => draw());
  draw(false);
  return () => unsubscribe();
}
