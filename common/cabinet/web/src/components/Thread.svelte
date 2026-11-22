<script module lang="ts">
  import { onDraftsCleared } from "../lib/drafts";
  import { isOpen } from "../lib/council";
  import type { CouncilView, DecisionItem, Message as Msg } from "../lib/types";

  // The field's text and its message key per draft key, shared by every mount of the thread: a send
  // that ends after the page moved on and back (A → B → A) clears the field shown now, and the same text sent
  // again keeps its key (the server takes it once). A sign-out empties both.
  const texts = $state<Record<string, string>>({});
  const pending = new Map<string, { text: string; id: string }>();
  // the files chosen for a draft live as long as the page: a browser keeps no File in sessionStorage
  const chosenFiles = $state<Record<string, File[]>>({});
  // a voice message's key from its first try: sent again alone, it is taken once
  const voiceKeys = new WeakMap<File, string>();
  onDraftsCleared(() => {
    for (const key of Object.keys(texts)) delete texts[key];
    for (const key of Object.keys(chosenFiles)) delete chosenFiles[key];
    pending.clear();
  });

  export type Block =
    | { type: "message"; key: string; message: Msg }
    | { type: "tools"; key: string; messages: Msg[] }
    | { type: "decision"; key: string; item: DecisionItem }
    | { type: "council"; key: string; council: CouncilView; seq: number };

  const TOOLS = new Set(["tool_call", "tool_result"]);
  const REVIEWS = new Set(["review", "review_reply"]); // a council's messages: its block shows them

  /** The message a decision was opened with; a question on it leaves it open too, but is only a line. */
  export const opens = (message: Msg) =>
    message.kind === "decision" && message.payload?.state === "open" && message.payload?.key !== "sys.decision.asked";

  /** Messages as the thread shows them: a decision where it was opened — one still open under the newest message,
   *  next to the field it is answered from — tool calls folded together. A council is one
   *  block, its critics' reviews and its writer's replies no lines of their own: an open one stands under the
   *  newest message (above the open decisions), an ended one where its first message is. A review of a council
   *  the page has not read stays a line: nothing is hidden by a list that did not load. */
  export function blocks(messages: Msg[], decisions: Record<string, DecisionItem>, councils: CouncilView[] = []): Block[] {
    const out: Block[] = [];
    const open: Block[] = [];
    const gathered = new Map(councils.map((council) => [council.id, council]));
    const placed = new Set<string>();
    const reading: Block[] = [];
    const place = (council: CouncilView, seq: number) => {
      if (placed.has(council.id)) return;
      placed.add(council.id);
      (isOpen(council) ? reading : out).push({ type: "council", key: `c${council.id}`, council, seq });
    };
    for (const message of messages) {
      const council = REVIEWS.has(message.kind) ? gathered.get(message.payload?.council) : undefined;
      if (council) {
        place(council, message.seq);
        continue;
      }
      const item = opens(message) ? decisions[message.payload.decision] : undefined;
      if (item) {
        (item.decision.state === "open" ? open : out).push({ type: "decision", key: `d${message.seq}`, item });
      } else if (TOOLS.has(message.kind)) {
        const last = out.at(-1);
        if (last?.type === "tools") last.messages.push(message);
        else out.push({ type: "tools", key: `t${message.seq}`, messages: [message] });
      } else {
        out.push({ type: "message", key: `m${message.seq}`, message });
      }
    }
    // an open council no critic has answered yet has no message; oldest first, as they were gathered
    for (const council of [...councils].reverse()) if (isOpen(council)) place(council, Number.MAX_SAFE_INTEGER);
    return out.concat(reading, open);
  }

  export type ToolStep = { key: string; call?: Msg; result?: Msg };

  /** A tool call and its result as one step, paired by the call's id; a result with no call stands alone. */
  export function toolSteps(messages: Msg[]): ToolStep[] {
    const calls = new Map<string, ToolStep>();
    const out: ToolStep[] = [];
    for (const m of messages) {
      const id = typeof m.payload?.id === "string" ? m.payload.id : "";
      if (m.kind === "tool_call") {
        const step: ToolStep = { key: `s${m.seq}`, call: m };
        if (id) calls.set(id, step);
        out.push(step);
      } else {
        const step = id ? calls.get(id) : undefined;
        if (step && !step.result) step.result = m;
        else out.push({ key: `s${m.seq}`, result: m });
      }
    }
    return out;
  }
</script>

<script lang="ts">
  // A card's thread: messages, decisions in place, tool calls folded; the composer (markdown, @agent hints,
  // attachments). It keeps to the bottom while the reader is there. The field is
  // multi-line (Enter breaks the line, Ctrl/Cmd+Enter sends), keeps a draft per person, card and target,
  // stays editable while sending and says whom the text goes to: a message (the agents it names, else the
  // card's agent) or the answer to an agent's open question (one open question is the default target).
  // With `chatAgent` it is that agent's chat: messages go to the agent.
  // Files: chosen (several at once), pasted (a screenshot with Ctrl+V) or dropped, they go with the text
  // as one message in one request; a refused send keeps the text and the files in the form and says why.
  // A voice message: the microphone button records up to five minutes (it stops itself there); stopped,
  // the recording goes at once on its own — a file without text, it wakes nobody; a refused one stays in the form.
  // A long thread opens on its newest window: «Show earlier» reads an older page and keeps the
  // reader's place, messages that come while the reader is above are counted on a chip that goes down, and a
  // linked decision older than the window is looked for page by page.
  import { tick } from "svelte";
  import { apiGet, apiPost, apiUpload } from "../lib/api";
  import { AUDIO_TYPES, carried, problem, refusal } from "../lib/attachments";
  import { bytes } from "../lib/bytes";
  import { chats, reportRead } from "../lib/chats.svelte";
  import { holdPresence } from "../lib/presence";
  import { parseSpace, spaceBase } from "../lib/spaces";
  import { clock, record, recordable, VOICE_MAX_MS, type Recording } from "../lib/recorder";
  import { inReadingArea, isReply, Reading, readUpTo } from "../lib/unread";
  import { afterSend, draftKey, isSendKey, readDraft, writeDraft } from "../lib/drafts";
  import { errorText, t } from "../lib/i18n.svelte";
  import { session } from "../lib/session.svelte";
  import { thread } from "../lib/stores/thread.svelte";
  import { toast } from "../lib/toast.svelte";
  import type { ToolOutput } from "../lib/types";
  import CouncilBlock from "./CouncilBlock.svelte";
  import DecisionCard from "./DecisionCard.svelte";
  import Message from "./Message.svelte";

  let {
    cardId = "",
    chatAgent = "",
    decisions,
    canComment,
    agents,
    assignee = "",
    highlight = "",
    working = "",
    canOutput = false,
    quiet = false,
    councils = [],
    canManageCouncil = false,
    councilBusy = false,
    ondecision,
    oncouncilcancel,
    oncounciledit,
  }: {
    cardId?: string;
    /** The agent thread this is, instead of a card's: its key (lib/spaces — the agent's chat, or `<agent>/monitor`). */
    chatAgent?: string;
    decisions: Record<string, DecisionItem>;
    canComment: boolean;
    agents: string[];
    /** The card's agent while the card is live: a plain message is for it. */
    assignee?: string;
    highlight?: string;
    /** The agent at work on this thread now: a line at the bottom, its running step spins. */
    working?: string;
    /** The person controls the agent: a tool's kept output opens in place. */
    canOutput?: boolean;
    /** Tool steps stay out of the conversation (the coordinator's chat). */
    quiet?: boolean;
    /** The card's councils, newest first: each is one block here. */
    councils?: CouncilView[];
    /** The person may cancel a council and change its composition. */
    canManageCouncil?: boolean;
    councilBusy?: boolean;
    ondecision: () => void;
    oncouncilcancel?: (council: string) => void;
    oncounciledit?: () => void;
  } = $props();

  const COORDINATOR = "@boss";
  const HUNT_MAX = 20; // older pages read to find a linked decision
  // quiet (the coordinator's chat): tool steps stay out of the conversation — the working line says it
  // works, "Diagnostics" shows the steps
  const shown = $derived(blocks(thread.messages, decisions, councils).filter((block) => !(quiet && block.type === "tools")));
  let scroller = $state<HTMLDivElement>();
  let field = $state<HTMLTextAreaElement>();
  let sending = $state(false);
  let outputs = $state<Record<number, ToolOutput | "loading">>({});
  const lastTools = $derived([...shown].reverse().find((b) => b.type === "tools")?.key ?? "");
  let fileInput = $state<HTMLInputElement>();
  let atBottom = $state(true);
  let jumpedTo = ""; // the linked decision the thread last scrolled to: another link scrolls again
  let huntFor = ""; // the linked decision the hunt's pages are counted for
  let hunted = 0;
  let hunting = $state(false); // a hunt's page is on its way: it ends (or fails) before the next one
  // the last message the reader has seen at the bottom: later ones are «new» while they read above
  let seen = $state(0);
  const lastSeq = () => thread.messages.at(-1)?.seq ?? 0;
  // in an agent's chat «new» is the server's count of replies the person was not shown
  const unseen = $derived(
    chatAgent ? chats.unread(chatAgent) : thread.messages.filter((m) => m.seq > seen && !TOOLS.has(m.kind)).length,
  );

  // An agent's chat: the first reply the person had not been shown when the chat opened gets a divider
  // "New messages · N" and the chat opens there (older pages are read up to it — nothing skipped counts as
  // read); the cursor moves only by messages shown in a visible, focused window (Reading, readUpTo).
  let divider = $state<{ seq: number; count: number } | null>(null);
  let dividerFor = -1; // the thread generation the divider was taken for
  let dividerShown = -1; // … and scrolled to
  let dividerPages = 0;
  const reading = new Reading();
  const firstSeq = (block: Block) =>
    block.type === "message" ? block.message.seq
      : block.type === "tools" ? block.messages[0].seq
      : block.type === "council" ? block.seq
      : Number(block.key.slice(1));
  const dividerKey = $derived(divider ? (shown.find((block) => firstSeq(block) >= divider!.seq)?.key ?? "") : "");

  $effect(() => {
    const gen = thread.generation;
    const state = chatAgent ? chats.of(chatAgent) : undefined;
    if (!chatAgent || thread.loading || !state || dividerFor === gen) return;
    dividerFor = gen;
    dividerPages = 0;
    reading.reset();
    divider = state.first !== null && state.unread > 0 ? { seq: state.first, count: state.unread } : null;
  });

  // the first unread is older than the window: older pages until it is there
  $effect(() => {
    if (!divider || hunting || thread.loadingOlder || !thread.older || dividerPages >= HUNT_MAX) return;
    if ((thread.messages[0]?.seq ?? 0) <= divider.seq) return;
    dividerPages += 1;
    void hunt();
  });

  // …then the chat opens on it, not at the bottom
  $effect(() => {
    const key = dividerKey;
    if (!key || dividerShown === thread.generation || hunting || thread.loadingOlder) return;
    void tick().then(() => {
      if (hunting || thread.loadingOlder) return; // a page is still on its way: the effect runs again after it
      const target = document.getElementById(`new-${key}`);
      if (!target) return;
      dividerShown = thread.generation;
      target.scrollIntoView?.({ block: "start" });
      atBottom = false;
      onScroll();
    });
  });

  // what the reading area shows: half of a message (or of the area) is in it
  $effect(() => {
    void shown.length;
    if (!chatAgent || !scroller || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const seq = Number((entry.target as HTMLElement).dataset.seq);
          if (entry.isIntersecting && inReadingArea(entry)) reading.enter(seq);
          else reading.leave(seq);
        }
      },
      { root: scroller, threshold: [0, 0.25, 0.5, 0.75, 1] },
    );
    void tick().then(() => scroller?.querySelectorAll<HTMLElement>("[data-seq]").forEach((el) => observer.observe(el)));
    return () => observer.disconnect();
  });

  // While this chat is in front of the person, its replies need no push to them
  $effect(() => {
    const agent = chatAgent;
    if (!agent || typeof document === "undefined") return;
    return holdPresence(agent);
  });

  // four looks a second: a visible, focused window moves the cursor over what it has shown
  $effect(() => {
    const agent = chatAgent;
    if (!agent) return;
    const timer = setInterval(() => {
      const active = document.visibilityState === "visible" && document.hasFocus();
      const shownSeqs = reading.tick(active);
      const state = chats.of(agent);
      if (!active || !state || thread.agent !== agent || thread.loading) return;
      // replies between the cursor and the window are not on the page yet: nothing past them is read
      if (thread.older && state.first !== null && (thread.messages[0]?.seq ?? 0) > state.first) return;
      if (divider && dividerKey && dividerShown !== thread.generation) return; // still opening on the first unread
      const replies = thread.messages.filter((m) => isReply(m, parseSpace(agent).agent)).map((m) => m.seq);
      const to = readUpTo(state.read, replies, shownSeqs);
      if (to > state.read) reportRead(agent, to);
    }, 250);
    return () => clearInterval(timer);
  });

  // the open questions this person may answer: the field can answer one of them, except an agent's form,
  // which is answered in the form itself
  const answerable = $derived(
    Object.values(decisions).filter(
      ({ decision: d }) => d.type === "question" && d.state === "open" && session.can(d.decide_permission, d.project || undefined),
    ),
  );
  const questions = $derived(answerable.filter((item) => item.view.origin !== "agent").map((item) => item.decision));
  const forms = $derived(answerable.filter((item) => item.view.origin === "agent").map((item) => item.decision));
  let chosen = $state<string | null | undefined>(undefined); // undefined: the default; null: a message
  const replyTo = $derived(
    chosen === undefined
      ? questions.length === 1 ? questions[0].id : null
      : chosen && questions.some((q) => q.id === chosen) ? chosen : null,
  );

  // the draft of this person, card and target
  const key = $derived(
    draftKey(session.me?.id ?? "", chatAgent ? `agent:${chatAgent}` : cardId, replyTo ? `reply:${replyTo}` : "thread"),
  );
  const textOf = (k: string) => texts[k] ?? readDraft(k);
  const draft = $derived(textOf(key));
  function setDraft(value: string, k = key) {
    texts[k] = value;
    writeDraft(k, value);
  }

  // the draft's files: an answer to a question carries them too, with the question's expectation
  const files = $derived(chosenFiles[key] ?? []);
  const expects = $derived(replyTo ? (decisions[replyTo]?.view?.expect ?? "text") : "");
  let failure = $state<{ key: string; text: string } | null>(null); // why a send of this draft did not go
  const failed = $derived(failure?.key === key ? failure.text : "");
  let dragging = $state(false);

  /** Why this answer does not answer its question yet. Recordings alone answer a text question once transcribed
   *; with the transcription off a recording is not a text answer. */
  function unanswered(body: string, attached: File[]): string {
    if (expects === "file" && !attached.length) return t("decision.need_file");
    if (expects === "text" && !body) {
      const audio = (f: File) => AUDIO_TYPES.has((f.type || "").split(";")[0].trim().toLowerCase());
      if (session.me?.voice && attached.length && attached.every(audio)) return "";
      return attached.some((f) => (f.type || "").startsWith("audio/")) ? t("decision.voice_no_transcript") : t("decision.need_text");
    }
    return "";
  }

  function addFiles(list: File[]) {
    if (!list.length) return;
    chosenFiles[key] = [...files, ...list];
    failure = null;
  }

  function removeFile(index: number) {
    chosenFiles[key] = files.filter((_, i) => i !== index);
    failure = null;
  }

  /** A screenshot from the clipboard is attached; a paste that carries text stays a paste of text. */
  function onPaste(event: ClipboardEvent) {
    const data = event.clipboardData;
    if (!data || Array.from(data.types ?? []).includes("text/plain")) return;
    const list = carried(data);
    if (!list.length) return;
    event.preventDefault();
    addFiles(list);
  }

  const dragsFiles = (event: DragEvent) => Array.from(event.dataTransfer?.types ?? []).includes("Files");

  function onDragOver(event: DragEvent) {
    if (!canComment || !dragsFiles(event)) return;
    event.preventDefault();
    dragging = true;
  }

  function onDrop(event: DragEvent) {
    dragging = false;
    if (!canComment || !dragsFiles(event)) return;
    event.preventDefault();
    addFiles(carried(event.dataTransfer));
  }

  // typing pins the target: a question that arrives meanwhile does not take the field away
  function onInput(value: string) {
    if (chosen === undefined) chosen = replyTo ?? null;
    setDraft(value);
  }
  // a message's idempotency key lives with the text it was made for until that text is sent (`pending`, above)

  // whom a message goes to, as the server decides it (api._addressees)
  const known = $derived(new Set([...agents, COORDINATOR]));
  const named = $derived(
    [...new Set([...draft.matchAll(/(?<![\w@])@([a-z][a-z0-9-]*)/g)].map((m) => `@${m[1]}`))].filter((a) => known.has(a)),
  );
  const addressee = $derived(
    chatAgent ? `@${parseSpace(chatAgent).agent}` : named.length ? named.join(", ") : known.has(assignee) ? assignee : "",
  );
  const asker = (actor: string) => actor.replace(/^(user|agent):/, "@");

  const mention = $derived(/(?:^|\s)@([\w-]*)$/.exec(draft)?.[1]);
  const hints = $derived(
    mention === undefined ? [] : agents.filter((a) => a.slice(1).startsWith(mention) && a.slice(1) !== mention),
  );

  function onScroll() {
    if (!scroller) return;
    atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    if (atBottom || !seen) seen = lastSeq(); // the window the thread opened on is seen
  }

  function toBottom() {
    if (!scroller) return;
    scroller.scrollTop = scroller.scrollHeight;
    atBottom = true;
    seen = lastSeq();
  }

  $effect(() => {
    void shown.length;
    const wanted = highlight; // read here, so a new link on the open card runs this again
    void tick().then(() => {
      if (!scroller) return;
      if (wanted && jumpedTo !== wanted) {
        const target = document.getElementById(`decision-${wanted}`);
        if (target) {
          target.scrollIntoView?.({ block: "center" });
          jumpedTo = wanted;
          seen = lastSeq();
          return;
        }
      }
      if (atBottom) toBottom();
    });
  });

  /** An older page above, the reader's place kept: what they saw stays where it was. */
  async function older() {
    const before = scroller?.scrollHeight ?? 0;
    const top = scroller?.scrollTop ?? 0;
    try {
      await thread.loadOlder();
    } catch (exc) {
      toast(errorText(exc), "error");
      return;
    }
    await tick();
    if (scroller) scroller.scrollTop = top + (scroller.scrollHeight - before);
  }

  // a linked decision whose message is older than the window: older pages until the one where it was opened is
  // shown (the jump above then centres it — the reader's place is not kept here); a page that fails ends the hunt
  $effect(() => {
    const wanted = highlight;
    if (!wanted || jumpedTo === wanted || hunting || thread.loading || thread.loadingOlder || !thread.older) return;
    if (wanted !== huntFor) {
      huntFor = wanted;
      hunted = 0;
    }
    if (thread.messages.some((m) => opens(m) && m.payload?.decision === wanted)) return;
    if (hunted >= HUNT_MAX) return;
    hunted += 1;
    void hunt();
  });

  async function hunt() {
    hunting = true;
    try {
      await thread.loadOlder();
    } catch (exc) {
      hunted = HUNT_MAX;
      dividerPages = HUNT_MAX; // a failed page ends the way to the first unread too: one toast, not twenty
      toast(errorText(exc), "error");
    } finally {
      hunting = false;
    }
  }

  function useHint(agent: string) {
    setDraft(draft.replace(/@([\w-]*)$/, `${agent} `));
  }

  function newKey(): string {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
  }

  function clientKey(k: string, text: string): string {
    let entry = pending.get(k);
    if (!entry || entry.text !== text) {
      entry = { text, id: newKey() };
      pending.set(k, entry);
    }
    return entry.id;
  }

  async function send() {
    const k = key;
    const sent = draft;
    const body = sent.trim();
    const target = replyTo;
    const attached = files;
    const generation = thread.generation; // the reply goes into this thread only, not one opened meanwhile
    if ((!body && !attached.length) || sending) return;
    const why = problem(attached);
    if (why) {
      failure = { key: k, text: why };
      return;
    }
    sending = true;
    failure = null;
    try {
      // the field is still this send's: no newer text of it went out meanwhile (from the thread opened again,
      // A → B → A), whose key and text that send keeps and cleans up itself
      let own = true;
      if (target) {
        const unmet = unanswered(body, attached);
        if (unmet) {
          failure = { key: k, text: unmet };
          return;
        }
        const id = clientKey(k, [body, ...attached.map((f) => `${f.name}\u0000${f.size}\u0000${f.lastModified}`)].join("\u0001"));
        if (attached.length) await apiUpload(`/api/decisions/${target}/files`, attached, { text: body, client_key: id });
        else await apiPost(`/api/decisions/${target}/reply`, { text: body });
        if (pending.get(k)?.id === id) pending.delete(k);
        chosenFiles[k] = (chosenFiles[k] ?? []).filter((f) => !attached.includes(f));
        ondecision();
      } else {
        // the same text with the same files keeps its key: a send repeated after a lost answer is taken once
        // …and a voice message that did not go, sent again as it was, keeps the key of its first try
        const voiceKey = !body && attached.length === 1 ? voiceKeys.get(attached[0]) : undefined;
        const id = voiceKey ?? clientKey(k, [body, ...attached.map((f) => `${f.name}\u0000${f.size}\u0000${f.lastModified}`)].join("\u0001"));
        const base = chatAgent ? spaceBase(chatAgent) : `/api/cards/${encodeURIComponent(cardId)}`;
        const made = attached.length
          ? await apiUpload(`${base}/files`, attached, { text: body, client_key: id })
          : await apiPost(`${base}/messages`, { body, client_key: id });
        thread.add(made, generation);
        own = voiceKey !== undefined || pending.get(k)?.id === id;
        if (own) {
          if (voiceKey === undefined) pending.delete(k);
          chosenFiles[k] = (chosenFiles[k] ?? []).filter((f) => !attached.includes(f));
        }
      }
      // what was typed while it was sending stays
      if (own) setDraft(afterSend(textOf(k), sent), k);
      if (target) chosen = undefined;
      atBottom = true;
    } catch (exc) {
      // all or nothing: the text and the files stay in the form, with the reason — an answer's too
      if (attached.length || target) failure = { key: k, text: refusal(exc) };
      else toast(errorText(exc), "error");
    } finally {
      sending = false;
    }
  }

  function onKey(event: KeyboardEvent) {
    if (!isSendKey(event)) return; // Enter breaks the line
    event.preventDefault();
    void send();
  }

  function answer(id: string) {
    chosen = id;
    void tick().then(() => field?.focus());
  }

  async function output(result: Msg) {
    const seq = result.seq;
    if (outputs[seq]) {
      delete outputs[seq];
      return;
    }
    outputs[seq] = "loading";
    try {
      outputs[seq] = await apiGet<ToolOutput>(`/api/sessions/${encodeURIComponent(result.payload.session)}/output/${seq}`);
    } catch (exc) {
      delete outputs[seq];
      toast(errorText(exc), "error");
    }
  }

  function attach() {
    addFiles(Array.from(fileInput?.files ?? []));
    if (fileInput) fileInput.value = "";
  }

  const canRecord = recordable();
  let voice = $state<{ handle: Recording | null; ms: number } | null>(null);
  let voiceSending = $state(false);
  let alive = true;
  // a thread closed mid-recording throws the recording away and frees the microphone
  $effect(() => () => {
    alive = false;
    voice?.handle?.cancel();
  });

  async function startVoice() {
    if (voice || voiceSending) return;
    const answering = !!replyTo; // an answer's recording waits in the form, with the answer
    const k = key;
    const path = chatAgent ? spaceBase(chatAgent) : `/api/cards/${encodeURIComponent(cardId)}`;
    const generation = thread.generation;
    voice = { handle: null, ms: 0 };
    failure = null;
    try {
      const handle = await record({
        tick: (ms) => {
          if (voice) voice.ms = ms;
        },
        done: (file, broken) => {
          voice = null;
          if (!alive) return;
          if (!file) {
            if (broken) toast(t("voice.failed", { reason: t("voice.broken") }), "error");
            return;
          }
          if (!broken && !answering) {
            void sendVoice(file, k, path, generation);
            return;
          }
          // the recorder failed midway (the microphone went away), or it answers a question: it waits in the form
          chosenFiles[k] = [...(chosenFiles[k] ?? []), file];
          if (broken) failure = { key: k, text: t("voice.broken") };
        },
      });
      if (!alive) handle.cancel();
      else if (voice) voice.handle = handle;
    } catch (exc) {
      voice = null;
      const denied = exc instanceof DOMException && (exc.name === "NotAllowedError" || exc.name === "SecurityError");
      toast(denied ? t("voice.denied") : t("voice.failed", { reason: errorText(exc) }), "error");
    }
  }

  async function sendVoice(file: File, k: string, path: string, generation: number) {
    let why = problem([file]);
    if (!why) {
      voiceSending = true;
      const id = newKey();
      voiceKeys.set(file, id);
      try {
        thread.add(await apiUpload(`${path}/files`, [file], { client_key: id }), generation);
        atBottom = true;
        return;
      } catch (exc) {
        why = refusal(exc);
      } finally {
        voiceSending = false;
      }
    }
    // not sent: the recording waits in the form with the reason, «Send» tries again
    chosenFiles[k] = [...(chosenFiles[k] ?? []), file];
    failure = { key: k, text: why };
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions (a drop of files is a shortcut; the attach button is the accessible way) -->
<div class="thread" class:dragging bind:this={scroller} onscroll={onScroll} ondragover={onDragOver} ondragleave={() => (dragging = false)} ondrop={onDrop}>
  {#if thread.older && !thread.error}
    <button type="button" class="btn small ghost earlier" disabled={thread.loadingOlder} onclick={() => void older()}>
      {t("thread.older")}
    </button>
  {/if}
  {#if thread.error}
    <p class="empty" role="alert">
      {t("card.thread_failed")}
      <button type="button" class="btn small ghost" onclick={() => void thread.retry()}>{t("app.retry")}</button>
    </p>
  {:else if !thread.loading && shown.length === 0}<p class="empty">{t("card.no_messages")}</p>{/if}
  {#each shown as block (block.key)}
    {#if divider && block.key === dividerKey}
      <div class="divider" id="new-{block.key}" role="separator">{t("thread.new_divider", { count: divider.count })}</div>
    {/if}
    {#if block.type === "decision"}
      <DecisionCard
        item={block.item}
        onchanged={ondecision}
        highlight={block.item.decision.id === highlight}
        onreply={canComment ? answer : undefined}
      />
    {:else if block.type === "council"}
      <CouncilBlock
        council={block.council}
        canManage={canManageCouncil}
        busy={councilBusy}
        {assignee}
        oncancel={() => oncouncilcancel?.(block.council.id)}
        onedit={oncounciledit}
      />
    {:else if block.type === "tools"}
      {@const steps = toolSteps(block.messages)}
      <details class="tools">
        <summary>{t("card.tools", { count: steps.length })}</summary>
        {#each steps as step (step.key)}
          <div class="tool">
            <span class="name">{step.call?.payload?.tool ?? t("card.tool_result")}</span>
            {#if step.result}
              <span class={step.result.payload?.ok === false ? "fail" : "ok"}>{step.result.payload?.ok === false ? "✗" : "✓"}</span>
            {:else if working && block.key === lastTools}
              <span class="spin" aria-label={t("thread.step_running")}></span>
            {:else}
              <span></span>
            {/if}
            {#if step.result?.payload?.output && step.result.payload.session && canOutput}
              {@const result = step.result}
              <button type="button" class="link" aria-expanded={!!outputs[result.seq]} onclick={() => void output(result)}>
                {t("card.tool_output")}
              </button>
            {/if}
            {#if step.call}<pre>{step.call.body}</pre>{/if}
            {#if step.result && outputs[step.result.seq] && outputs[step.result.seq] !== "loading"}
              {@const out = outputs[step.result.seq] as ToolOutput}
              <pre class="output">{out.text}</pre>
              {#if out.truncated}<span class="note">{t("sessions.turns.truncated")}</span>{/if}
            {/if}
          </div>
        {/each}
      </details>
    {:else}
      <div data-seq={block.message.seq}>
        <Message message={block.message} decision={block.message.kind === "decision" ? decisions[block.message.payload?.decision] : undefined} />
      </div>
    {/if}
  {/each}
  {#if working}
    <div class="working" role="status"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>{t("thread.working", { agent: working })}</div>
  {/if}
</div>
{#if unseen > 0 && !atBottom}
  <div class="below"><button type="button" class="chip" onclick={toBottom}>{t("thread.new_below", { count: unseen })} <span aria-hidden="true">↓</span></button></div>
{/if}

{#if canComment}
  <div class="targets" role="group" aria-label={t("composer.target")}>
    <button type="button" class="chip" class:on={!replyTo} aria-pressed={!replyTo} onclick={() => (chosen = null)}>
      {addressee ? t("composer.to", { who: addressee }) : t("composer.to_thread")}
    </button>
    {#each questions as q (q.id)}
      <span class="question">
        <button type="button" class="chip" class:on={replyTo === q.id} aria-pressed={replyTo === q.id} onclick={() => (chosen = q.id)}>
          {t("composer.answer", { who: asker(q.requested_by) })}
        </button>
        <a href="#decision-{q.id}" aria-label={t("composer.to_question")}>↗</a>
      </span>
    {/each}
    {#each forms as q (q.id)}
      <a class="formlink" href="#decision-{q.id}">{t("composer.form_hint")} ↗</a>
    {/each}
  </div>
  {#if expects}
    <p class="expects muted">
      {t(expects === "file" ? "decision.need_file" : "decision.need_text")}{#if expects === "text" && session.me?.voice}
        · {t("decision.voice_answer")}{/if}
    </p>
  {/if}
  {#if files.length || failed}
    <div class="chosen">
      {#each files as file, i (i)}
        <span class="chip file">
          {file.name} <span class="muted">{bytes(file.size)}</span>
          <button type="button" class="x" disabled={sending} aria-label={t("files.remove", { name: file.name })} onclick={() => removeFile(i)}>×</button>
        </span>
      {/each}
      {#if failed}<p class="failed" role="alert">{t("files.not_sent", { reason: failed })}</p>{/if}
    </div>
  {/if}
  {#if voice || voiceSending}
    <div class="recording" role="status">
      {#if voice?.handle}<span class="rec" aria-hidden="true"></span>{/if}
      <span>
        {!voice ? t("voice.sending") : voice.handle ? t("voice.recording", { time: clock(voice.ms), max: clock(VOICE_MAX_MS) }) : t("voice.asking")}
      </span>
      {#if voice}
        <button type="button" class="btn small ghost" disabled={!voice.handle} onclick={() => voice?.handle?.cancel()}>{t("voice.cancel")}</button>
        <button type="button" class="btn small" disabled={!voice.handle} onclick={() => voice?.handle?.stop()}>{t("voice.send")}</button>
      {/if}
    </div>
  {/if}
  <!-- svelte-ignore a11y_no_static_element_interactions (a drop of files is a shortcut; the attach button is the accessible way) -->
  <div class="composer" class:dragging ondragover={onDragOver} ondragleave={() => (dragging = false)} ondrop={onDrop}>
    {#if dragging}<div class="drop" aria-hidden="true">{t("files.drop")}</div>{/if}
    {#if hints.length}
      <div class="hints" role="listbox">
        {#each hints as agent (agent)}
          <button type="button" role="option" aria-selected="false" onclick={() => useHint(agent)}>{agent}</button>
        {/each}
      </div>
    {/if}
    <textarea
      rows="3"
      bind:this={field}
      placeholder={t(replyTo ? "decision.reply_placeholder" : "card.message.placeholder")}
      aria-label={t(replyTo ? "decision.reply_placeholder" : "card.message.placeholder")}
      value={draft}
      oninput={(event) => onInput(event.currentTarget.value)}
      onkeydown={onKey}
      onpaste={onPaste}
    ></textarea>
    <!-- the attach and voice buttons stand in a column above send, so the field keeps the width -->
    <div class="actions">
      <button class="iconbtn" type="button" title={t("action.attach")} aria-label={t("action.attach")} disabled={sending} onclick={() => fileInput?.click()}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 11l-8.5 8.5a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L15 7" /></svg>
      </button>
      <input class="visually-hidden" type="file" multiple bind:this={fileInput} onchange={attach} tabindex="-1" />
      {#if canRecord}
        <button
          class="iconbtn"
          class:on={!!voice}
          type="button"
          title={t(voice ? "voice.send" : "voice.record")}
          aria-label={t(voice ? "voice.send" : "voice.record")}
          aria-pressed={!!voice}
          disabled={voiceSending || (!!voice && !voice.handle)}
          onclick={() => (voice ? voice.handle?.stop() : void startVoice())}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5 11a7 7 0 0 0 14 0M12 18v3" /></svg>
        </button>
      {/if}
      <button class="iconbtn send" type="button" title={t("action.send")} aria-label={t("action.send")} disabled={sending || (!draft.trim() && !files.length)} onclick={send}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12l16-8-6 16-2-7z" /></svg>
      </button>
    </div>
  </div>
{/if}

<style>
  .thread { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; padding: 14px 2px 10px; overscroll-behavior: contain; }
  /* a long thread scrolls; its blocks keep their height (a decision card clips with overflow: hidden, so
     as a flex item it would shrink to nothing) */
  .thread > :global(*) { flex: none; }
  .tools { border: 1px solid var(--line); border-radius: 8px; background: var(--bg); font-size: 13px; margin-left: 40px; }
  .tools summary { padding: 7px 10px; color: var(--muted); cursor: pointer; }
  .tool { display: grid; grid-template-columns: auto auto auto 1fr; gap: 8px; padding: 6px 10px; border-top: 1px solid var(--line); align-items: center; }
  .tool .name { color: var(--muted); }
  .tool pre { grid-column: 1 / -1; margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12.5px; }
  .ok { color: var(--good); } .fail { color: var(--crit); }
  .tool .output { margin-top: 2px; padding: 8px; max-height: 320px; overflow: auto; background: var(--surface-2); border: 1px solid var(--line-soft); border-radius: 8px; font-size: 12px; white-space: pre; }
  .tool .note { grid-column: 1 / -1; font-size: 12px; color: var(--muted); }
  .link { background: none; border: none; padding: 0; color: var(--accent); cursor: pointer; font-size: 12px; text-decoration: underline; justify-self: start; }
  .spin { width: 11px; height: 11px; border: 2px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; }
  .working { display: flex; gap: 8px; align-items: center; color: var(--muted); font-size: 13px; padding-left: 40px; }
  .dots { display: inline-flex; gap: 3px; }
  .dots i { width: 5px; height: 5px; border-radius: 50%; background: var(--accent); animation: blink 1.2s infinite ease-in-out; }
  .dots i:nth-child(2) { animation-delay: 0.2s; }
  .dots i:nth-child(3) { animation-delay: 0.4s; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes blink { 0%, 80%, 100% { opacity: 0.25; } 40% { opacity: 1; } }
  @media (prefers-reduced-motion: reduce) { .spin, .dots i { animation: none; } }
  .earlier { align-self: center; }
  .divider { display: flex; align-items: center; gap: 10px; color: var(--accent); font-size: 12px; font-weight: 600; }
  .divider::before, .divider::after { content: ""; flex: 1; border-top: 1px solid var(--accent); opacity: 0.5; }
  .below { flex: none; display: flex; justify-content: center; margin-top: -40px; height: 40px; align-items: center; position: relative; pointer-events: none; }
  .below .chip { pointer-events: auto; cursor: pointer; background: var(--surface); border-color: var(--accent); box-shadow: 0 2px 8px rgb(0 0 0 / 0.25); }
  .composer { position: relative; flex: none; margin-top: 8px; display: flex; gap: 8px; align-items: flex-end; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 6px; }
  .composer.dragging, .thread.dragging { outline: 2px dashed var(--accent); outline-offset: -2px; }
  .drop { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; background: var(--surface); border-radius: 12px; color: var(--accent); font-size: 14px; pointer-events: none; z-index: 1; }
  .chosen { flex: none; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: 8px; }
  .chosen .file { display: inline-flex; gap: 6px; align-items: center; max-width: 100%; overflow-wrap: anywhere; }
  .chosen .x { background: none; border: 0; padding: 0 2px; color: var(--muted); cursor: pointer; font-size: 15px; line-height: 1; }
  .failed { flex-basis: 100%; margin: 0; color: var(--crit); font-size: 13px; }
  .composer textarea { flex: 1; min-width: 0; align-self: stretch; min-height: 76px; max-height: 200px; resize: none; background: none; border: 0; padding: 8px 4px; }
  .composer .actions { flex: none; display: flex; flex-direction: column; gap: 4px; }
  .composer textarea:focus { outline: none; }
  .composer .iconbtn { border: 0; }
  .composer .send { background: var(--accent); color: var(--accent-ink); }
  .composer .iconbtn.on { color: var(--crit); }
  .recording { flex: none; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 8px; font-size: 13px; }
  .recording .rec { width: 10px; height: 10px; border-radius: 50%; background: var(--crit); animation: blink 1.2s infinite ease-in-out; }
  @media (prefers-reduced-motion: reduce) { .recording .rec { animation: none; } }
  .targets { flex: none; display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
  .targets .chip { cursor: pointer; }
  .targets .chip.on { color: var(--text); border-color: var(--accent); }
  .question { display: inline-flex; align-items: center; gap: 4px; }
  .question a, .formlink { color: var(--accent); text-decoration: none; }
  .formlink { font-size: 13px; align-self: center; }
  .hints { position: absolute; bottom: calc(100% + 6px); left: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
  .hints button { background: var(--surface-2); border: 1px solid var(--line); border-radius: 999px; padding: 4px 10px; font-size: 13px; }
</style>
