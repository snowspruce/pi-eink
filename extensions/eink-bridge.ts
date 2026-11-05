/**
 * E-ink action bridge.
 *
 * Serves a tiny HTTP endpoint that lets an e-ink Kindle on the same LAN push
 * whitelisted actions into a running session (not a new process).
 *
 * Each pi session owns its own port (base 8010, offset by a hash of the session
 * id, escalating on collision) and registers itself in ~/.pi/eink/bridges.json,
 * so the viewer can list every running session and drive each one separately.
 * bridge.json keeps the single most recent entry for callers that only need to
 * know "is any bridge alive" (safety-gate's approval handshake).
 *
 * Only fixed actions exist, plus `/act/say` — free text into this session, and
 * only accepted from loopback so the Kindle reaches it through the viewer's proxy
 * instead of posting to the LAN-exposed bridge port directly. The agent has bash
 * access, so a free-text endpoint reachable from the LAN would be remote code
 * execution.
 *
 * Auth: HttpOnly cookie `pieink`, seeded once via ?k=<token>, where the token
 * lives in ~/.pi/eink/token. Peers are gated by ~/.pi/eink/allow_ips (IP or
 * CIDR, loopback always allowed), the same file the viewer reads.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { BlockList, isIPv4, isIPv6 } from "node:net";
import { appendFileSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync, unlinkSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { execFile } from "node:child_process";
import { join, resolve, sep } from "node:path";

const REGISTRY_FILE = join(homedir(), ".pi/eink/bridges.json");
const BASE_PORT = 8010;
const PORT_SPAN = 21;
const PORT = Number(process.env.EINK_BRIDGE_PORT ?? 0);
// 0.0.0.0 exposes the bridge on every interface (VPN, docker bridges). Same name
// and meaning as the viewer's variable; tests bind loopback with it.
// Loopback by default, and a *separate* variable from the viewer's binding.
// It used to read EINK_BIND, which the viewer's launchd job sets to 0.0.0.0 - so every
// session's bridge ended up listening on the LAN, reachable directly and without the
// viewer's OTP layer (the viewer proxies to it, nothing else needs to).
export const BIND_ADDRESS = process.env.EINK_BRIDGE_BIND ?? "127.0.0.1";
const BIND = BIND_ADDRESS;
const VIEWER_PORT = Number(process.env.EINK_PORT ?? 8000);
const TOKEN_FILE = join(homedir(), ".pi/eink/token");
const COOKIE = "pieink";
// The viewer shows "the newest session file", but actions land in whichever pi
// process owns this port. Publishing the owner here lets the viewer follow it so
// the page and the buttons can never point at different sessions.
const STATE_FILE = join(homedir(), ".pi/eink/bridge.json");
// handshake with safety-gate: it parks a risky command here and waits for an answer
const PENDING_FILE = join(homedir(), ".pi/eink/pending.json");
const ANSWER_FILE = join(homedir(), ".pi/eink/answer.json");
// Messages the box queued while this session is busy.
//
// pi's own queue cannot be read or edited from an extension - ExtensionContext has
// `hasPendingMessages(): boolean` and nothing else; the steering/follow-up arrays and
// `clearQueue()` live on AgentSession and are only bound to RPC/JSON clients, and the
// `queue_update` event goes to those clients too. So the list, the delete/edit/reorder
// controls and the counter the page shows are backed by THIS queue instead: everything
// the e-ink box sends while the agent is busy lands here, and one message is delivered
// per settled run. The trade-off is stated in README: a message typed on the computer
// during a run is in pi's queue, which the box can neither see nor change.
const QUEUE_FILE = join(homedir(), ".pi/eink/queue.json");
const QUEUE_MAX = 20;
// A queue nobody came back for is dropped after a week: long enough to survive a
// weekend, short enough that the file does not grow one key per session for ever.
const QUEUE_KEEP_MS = 7 * 24 * 3600_000;
// Where a new session may be started from the page. Not a security boundary (a `full`
// credential can already drive a shell through the agent) - it stops a mistyped or
// hand-edited `dir` from putting pi in `/` or in a home directory with 10k files.
// Directories that already run pi are allowed too: the page offers them.
const PROJECT_ROOTS = (process.env.EINK_PROJECT_ROOTS || join(homedir(), "Documents", "Program"))
	.split(":")
	.map((s) => s.trim())
	.filter(Boolean);
// Why a session stopped being the bridge owner; the TUI has no room to say it.
const BRIDGE_LOG = join(homedir(), ".pi/eink/bridge.log");
// Session control (new session, reload, compact, quit) is only reachable through
// ExtensionCommandContext, i.e. a command handler. Commands are namespaced
// "eink:" so they do not collide with the TUI built-ins of the same function.
const BRIDGE_COMMANDS: Record<string, string> = {
	reload: "eink:reload",
	compact: "eink:compact",
	quit: "eink:quit",
	exit: "eink:quit",
};

// Built-in commands handled inside the TUI; an extension can never invoke them.
const TUI_ONLY_COMMANDS = new Set([
	"settings", "model", "tree", "thinking", "scoped-models", "export", "import",
	"share", "bug", "copy", "name", "session", "changelog", "hotkeys", "fork",
	"clone", "trust", "login", "logout", "resume",
	// no in-process implementation any more: a new session is a new window, and a
	// spawned process is not something an extension command can do
	"new",
]);

const MAX_LISTEN_ATTEMPTS = 4;
// Binding the live bridge's port only works on the first try in practice; the
// retries exist for the old socket the previous runtime closed asynchronously.
// A pid in bridge.json proves nothing if the entry is ancient: pids get reused.
const STATE_MAX_AGE_MS = 5 * 60_000;
// Kindle Voyage's browser reports "Kindle Build/4.0-rc2". Spoofable, so it only
// sits in front of the token check as a speed bump.
const DEFAULT_UA = "Kindle Build/";
const UA_FILE = join(homedir(), ".pi/eink/uas");
const ALLOW_FILE = join(homedir(), ".pi/eink/allow_ips");

/** Same precedence as the viewer: the file wins, then the env list. */
function allowEntries(): string[] {
	try {
		const items = readFileSync(ALLOW_FILE, "utf8")
			.split("\n")
			.map((line) => line.replace(/#.*$/, "").trim())
			.filter(Boolean);
		if (items.length > 0) return items;
	} catch {
		// fall through to the env list
	}
	return (process.env.EINK_ALLOW_IPS ?? "")
		.split(",")
		.map((s) => s.trim())
		.filter(Boolean);
}

function familyOf(ip: string): "ipv4" | "ipv6" | null {
	if (isIPv4(ip)) return "ipv4";
	if (isIPv6(ip)) return "ipv6";
	return null;
}

function sameV4Slash24(a: string, b: string): boolean {
	const pa = a.split(".");
	const pb = b.split(".");
	return pa.length === 4 && pb.length === 4 && pa[0] === pb[0] && pa[1] === pb[1] && pa[2] === pb[2];
}

/**
 * Mirrors viewer/server.py:ip_allowed() so the page and the buttons agree on who
 * may talk to us. Exported for tests; pi only uses the default export.
 */
export function ipAllowed(rawIp: string, localIp = ""): boolean {
	const ip = rawIp.startsWith("::ffff:") ? rawIp.slice(7) : rawIp;
	if (isLoopback(ip)) return true;
	const family = familyOf(ip);
	if (!family) return false;

	const block = new BlockList();
	for (const entry of allowEntries()) {
		const [addr, bits] = entry.split("/");
		const entryFamily = familyOf(addr ?? "");
		if (!entryFamily) continue;
		try {
			if (bits === undefined) block.addAddress(addr, entryFamily);
			else block.addSubnet(addr, Number(bits), entryFamily);
		} catch {
			// a malformed line must not take the whole gate down
		}
	}
	if (block.check(ip, family)) return true;

	// Opt-in convenience: any peer in the host's own /24. Off unless asked for.
	return (
		process.env.EINK_ALLOW_SAME_SUBNET !== "0" &&
		family === "ipv4" &&
		localIp !== "" &&
		sameV4Slash24(ip, localIp)
	);
}

/** Existing directory, inside a project root or already running pi.
 *  Exported for tests, like the other gates in this file. */
/** Single-quote for /bin/sh. A directory name is filesystem input: `x'; rm -rf ~; '`
 *  is a legal name, and this string is typed into a shell by iTerm. */
export function shellQuote(s: string): string {
	return "'" + s.replace(/'/g, `'\\''`) + "'";
}

/** Open a new terminal window at `dir` and run pi there.
 *
 * This is the only way to get a session without disturbing the current one: pi is a
 * full-screen TUI, its `--print`/`--mode` flags do not start an interactive session,
 * and its cwd comes from the process, not a flag. So: a fresh `pi` in a fresh shell.
 *
 * The command travels as an osascript *argument* (`on run argv`), not inside the
 * AppleScript source: one less quoting layer over an attacker-adjacent string.
 */
export const spawnRunner: { run: typeof execFile } = { run: execFile };

export function spawnPiWindow(dir: string, app = process.env.EINK_TERMINAL || "iTerm",
	run: typeof execFile = spawnRunner.run): void {
	const script = [
		"on run argv",
		"\tset cmd to item 1 of argv",
		'\ttell application "' + app.replace(/"/g, "") + '"',
		"\t\tactivate",
		"\t\tset w to (create window with default profile)",
		"\t\ttell current session of w to write text cmd",
		"\tend tell",
		"end run",
	].join("\n");
	// Strip inherited PI_* variables. A spawned shell inherits this process's environment,
	// and PI_SESSION_FILE points at the session that is already open here: a fresh pi
	// started with it attaches to that file (or gives up on it) and exits immediately -
	// the new window opened and closed, and the viewer waited 30s for a session that
	// never appeared.
	const strip = Object.keys(process.env)
		.filter((k) => k.startsWith("PI_"))
		.map((k) => " -u " + k)
		.join("");
	const command = "cd " + shellQuote(dir) + " && exec env" + strip + " pi";
	run("osascript", ["-e", script, "--", command], (err, _out, errOut) => {
		if (err) console.error("[eink] could not open a window in " + app + ":", (errOut || err.message).trim());
	});
}

export function mayStartSessionIn(dir: string, roots: string[] = [], known: string[] = []): boolean {
	let resolved: string;
	try {
		if (!statSync(dir).isDirectory()) return false;
		resolved = resolve(dir);
	} catch {
		return false;
	}
	if (known.some((k) => k && resolve(k) === resolved)) return true;
	return roots.some((root) => {
		const base = resolve(root);
		return resolved === base || resolved.startsWith(base.endsWith(sep) ? base : base + sep);
	});
}

function uaAllowList(): string[] {
	try {
		const items = readFileSync(UA_FILE, "utf8")
			.split("\n")
			.map((line) => line.replace(/#.*$/, "").trim().toLowerCase())
			.filter(Boolean);
		if (items.length > 0) return items;
	} catch {
		// fall through to the env default
	}
	return (process.env.EINK_ALLOW_UA ?? DEFAULT_UA)
		.split(",")
		.map((s) => s.trim().toLowerCase())
		.filter(Boolean);
}

function logBridge(message: string): void {
	try {
		appendFileSync(BRIDGE_LOG, `${new Date().toISOString()}\tpid=${process.pid}\t${message}\n`);
	} catch {
		// logging must never break the bridge
	}
}

function pidAlive(pid: number): boolean {
	try {
		process.kill(pid, 0);
		return true;
	} catch (err) {
		return (err as NodeJS.ErrnoException).code === "EPERM";
	}
}

/** Actions a non-loopback peer may run on the bridge port directly.
 *  Everything else - free text, approvals, prompts - is loopback-only: the viewer
 *  proxies from 127.0.0.1, so the Kindle keeps working while a LAN client cannot
 *  drive the agent even if it holds the token. Exported for tests. */
export function loopbackOnly(action: string): boolean {
	return action !== "" && action !== "stop" && action !== "status";
}

// A viewer running on another host proxies with its own LAN address, not loopback,
// so those actions would be refused there. EINK_VIEWER_IPS names that host; empty
// (the default) keeps this machine as the only trusted proxy.
const VIEWER_IPS = (process.env.EINK_VIEWER_IPS ?? "")
	.split(",")
	.map((s) => s.trim())
	.filter(Boolean);

/** True when this peer may run loopback-only actions. Exported for tests. */
export function mayProxy(ip: string, trusted: string[] = VIEWER_IPS): boolean {
	return isLoopback(ip) || trusted.includes(ip);
}

/** null = allowed; otherwise the reason to refuse. Kept as one function so the
 *  handler has a single call site and the rules themselves can be tested. */
export function gate(ip: string, action: string, path = "", localIp = "",
	trusted: string[] = VIEWER_IPS, lang: Lang = "zh"): string | null {
	// localIp matters: the same-subnet rule needs the address this socket answered on
	if (!ipAllowed(ip, localIp)) return tt(lang, "gate_ip");
	// /state exposes the same model/context/idle the status action returns
	if (!mayProxy(ip, trusted) && ((action !== "" && loopbackOnly(action)) || path === "/state" || path === "/queue")) {
		return tt(lang, "gate_loopback");
	}
	return null;
}

function isLoopback(ip: string): boolean {
	return ip.startsWith("127.") || ip === "::1" || ip === "localhost";
}

function token(): string {
	try {
		return readFileSync(TOKEN_FILE, "utf8").trim();
	} catch {
		return "";
	}
}

function clientIp(req: IncomingMessage): string {
	const ip = req.socket.remoteAddress ?? "";
	return ip.startsWith("::ffff:") ? ip.slice(7) : ip;
}

const HOST_OK = /^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$/;

function escAttr(value: string): string {
	return value
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;");
}

function cookies(req: IncomingMessage): Record<string, string> {
	const out: Record<string, string> = {};
	for (const part of (req.headers.cookie ?? "").split(";")) {
		const i = part.indexOf("=");
		if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
	}
	return out;
}

function html(title: string, body: string): string {
	return [
		"<!DOCTYPE html><html><head>",
		'<meta name="viewport" content="width=device-width, initial-scale=1">',
		"<title>", title, "</title>",
		"<style>",
		"body{font-family:-apple-system,Helvetica,sans-serif;font-size:26px;line-height:1.4;",
		"background:#fff;color:#000;padding:24px}",
		"a{color:#000}",
		"</style></head><body>",
		body,
		"</body></html>",
	].join("");
}

type Action = "stop" | "continue" | "review" | "execute" | "status";

const PROMPTS: Record<Exclude<Action, "stop" | "status">, string> = {
	continue: "继续",
	review: "对以上优化和修复做一个审查",
	execute: "根据建议执行",
};

type Lang = "zh" | "en";

// Strings the bridge hands back to the reader. The prompts sent to the agent
// (PROMPTS) stay Chinese on purpose: those are instructions to the model, not UI.
const TEXT: Record<string, [string, string]> = {
	gate_ip: ["不在允许列表", "not in the allow list"],
	gate_loopback: ["该动作只接受本机转发（通过 eink 页面操作）", "loopback only: drive it from the e-ink page"],
	queued_n: ["已排队（第 %s 条）", "Queued (#%s)"],
	queue_full: ["排队最多 %s 条", "The queue holds at most %s"],
	queue_started: ["已开始投递（一条一轮）", "Delivery started (one per run)"],
	queue_cleared: ["已清空排队（%s 条）", "Cleared %s queued"],
	queue_deleted: ["已删除第 %s 条", "Removed #%s"],
	queue_edited: ["已修改第 %s 条", "Updated #%s"],
	queue_moved: ["已移动", "Moved"],
	queue_missing: ["没有这条排队消息", "No such queued message"],
	queue_dropped: ["（丢弃排队 %s 条）", " (dropped %s queued)"],
	sent_run: ["已发送（立即执行）", "Sent (running now)"],
	sent_steer: ["已插入当前运行（立即执行）", "Sent into the current run"],
	sent: ["已发送", "Sent"],
	already_answered: ["该确认已处理，无需再点", "Already answered"],
	other_session: ["该确认属于另一个会话（pid %s）", "That approval belongs to another session (pid %s)"],
	allowed: ["已允许", "Allowed"],
	denied: ["已拒绝", "Denied"],
	aborted: ["已中止当前运行", "Stopped the current run"],
	nothing_running: ["当前没有运行", "Nothing is running"],
	empty_input: ["没有输入内容", "Nothing to send"],
	new_spawning: ["正在新窗口里开会话：%s", "Opening a new window in %s"],
	new_no_dir: ["没有指定目录", "No directory given"],
	new_dir_refused: ["不能在这个目录开会话：%s（要项目根目录下、或已经跑着 pi 的目录）",
		"Cannot start a session there: %s (must be under a project root or already running pi)"],
	reloading: ["正在重载扩展与配置", "Reloading extensions and config"],
	compacting: ["开始压缩上下文", "Compacting the context"],
	quitting: ["正在退出该 pi 会话", "Quitting this pi session"],
	tui_only: ["该命令只能在电脑的 TUI 里执行（Kindle 上可用：/new /reload /compact /quit /skill:xxx）",
		"That only works in the desktop TUI (on the Kindle: /new /reload /compact /quit /skill:xxx)"],
	dedupe: ["%s 秒内重复点击，已忽略", "Ignored: tapped again within %s s"],
	failed: ["失败：%s", "Failed: %s"],
	unknown_action: ["未知动作", "Unknown action"],
	refused: ["拒绝", "Refused"],
	missing_token: ["缺少 token", "Missing token"],
	unauthorized: ["未授权", "Unauthorized"],
	bad_host: ["错误", "Error"],
	method: ["方法错误", "Method not allowed"],
	post_only: ["只接受 POST", "POST only"],
	done: ["完成", "Done"],
	back: ["返回", "Back"],
	status_title: ["状态", "Status"],
	status_idle: ["空闲", "idle"],
	status_busy: ["运行中", "running"],
	status_queue: ["排队", "Queued"],
	status_yes: ["有", "yes"],
	status_no: ["无", "no"],
	status_model: ["模型", "Model"],
	status_thinking: ["思考", "Thinking"],
	unknown: ["未知", "unknown"],
};

function tt(lang: Lang, key: string, ...args: (string | number)[]): string {
	const pair = TEXT[key];
	let out = pair ? (lang === "en" ? pair[1] : pair[0]) : key;
	for (const a of args) out = out.replace("%s", String(a));
	return out;
}

function langOf(req: IncomingMessage): Lang {
	return String(req.headers["x-eink-lang"] ?? "").toLowerCase() === "en" ? "en" : "zh";
}


export default function einkBridge(pi: ExtensionAPI) {
	let server: ReturnType<typeof createServer> | null = null;
	let ctx: ExtensionContext | null = null;
	let heartbeat: ReturnType<typeof setInterval> | null = null;
	let boundPort = 0;
	// e-ink taps double-fire easily and a page reload replays a POST. Side-effecting
	// actions are deduped by name within this window.
	const DEDUPE_MS = 8000;
	const lastFired = new Map<string, number>();

	const send = async (action: Exclude<Action, "stop" | "status">, lang: Lang = "zh") => {
		const text = PROMPTS[action];
		if (!ctx) throw new Error("no session context");
		// Busy: the message joins the box's own queue, so it can be seen, edited and
		// dropped from the page. Idle: a plain user message starts a turn.
		if (!ctx.isIdle()) {
			enqueue(text, lang);
			return tt(lang, "queued_n", queue.length);
		}
		try {
			await pi.sendUserMessage(text);
			return tt(lang, "sent_run");
		} catch (err) {
			// A run can start between the idle check and the send; sendUserMessage
			// rejects without deliverAs in that case, so retry as a steer.
			await pi.sendUserMessage(text, { deliverAs: "steer" });
			return tt(lang, "sent_steer");
		}
	};

	const wantsJson = (req: IncomingMessage) => (req.headers.accept ?? "").includes("application/json");

	// ---- the queue the box owns (see QUEUE_FILE) -----------------------------
	// Two states are visible to the reader and they are not the same thing:
	// `queue` (here) is what the e-ink box typed while the agent was busy, fully
	// editable; `ctx.hasPendingMessages()` is pi's own queue (typed in the TUI, or a
	// steer), which no extension can read. The page says so instead of pretending.
	let queue: { i: number; text: string; at: number }[] = [];
	let delivering = false;
	// Did a run actually start? The retry below must not pipeline the next message
	// into the same run just because `isIdle()` had not flipped yet (a slow provider
	// preflight or a delaying hook is enough). `agent_start` is the fact; a timer is a
	// guess, so the retry asks this instead of the clock.
	let runStarted = false;
	// Whether this process is allowed to deliver the queue by itself. Set by anything
	// that queues here, and by a reload (so `/reload` is transparent). NOT set when a
	// session is merely resumed: three messages queued before a crash must not fire by
	// themselves a week later - they stay listed on the page until the reader taps
	// 开始投递 or queues something new.
	let armed = false;

	const sessionId = () => ctx?.sessionManager.getSessionId() ?? "";

	const readQueueFile = (): Record<string, unknown> => {
		try {
			return JSON.parse(readFileSync(QUEUE_FILE, "utf8")) as Record<string, unknown>;
		} catch {
			return {};
		}
	};

	const saveQueue = () => {
		const sid = sessionId();
		if (!sid) return;
		try {
			const all = readQueueFile();
			if (queue.length > 0) all[sid] = queue;
			else delete all[sid];
			// Drop queues nobody came back for: one key per session that ever queued
			// something would otherwise accumulate for ever.
			const stale = Date.now() - QUEUE_KEEP_MS;
			for (const [key, value] of Object.entries(all)) {
				if (key === sid || !Array.isArray(value) || value.length === 0) continue;
				const newest = value.reduce((max, item) => Math.max(max, Number(item?.at) || 0), 0);
				if (newest < stale) delete all[key];
			}
			// read-modify-write without a lock: two pi processes saving in the same instant
			// can lose one write, exactly like bridges.json. Losing an enqueue is a visible
			// loss (the page shows an empty queue), so the window stays short and small.
			const tmp = `${QUEUE_FILE}.${process.pid}.tmp`;
			writeFileSync(tmp, JSON.stringify(all, null, 2));
			renameSync(tmp, QUEUE_FILE);
		} catch {
			// best effort: a lost queue file must not break the page
		}
	};

	/** Reload this session's queue from disk (a `/reload` keeps the messages). */
	const loadQueue = (): number => {
		const sid = sessionId();
		queue = [];
		const raw = sid ? readQueueFile()[sid] : undefined;
		if (Array.isArray(raw)) {
			queue = raw
				.filter((r) => r && typeof r === "object" && String((r as { text?: unknown }).text ?? "").trim())
				.map((r, n) => {
					const item = r as { i?: unknown; text: unknown; at?: unknown };
					return {
						i: Number(item.i) || n + 1,
						text: String(item.text).slice(0, 2000),
						at: Number(item.at) || Date.now(),
					};
				})
				.slice(0, QUEUE_MAX);
		}
		return queue.length;
	};

	const nextQueueId = () => queue.reduce((max, item) => Math.max(max, item.i), 0) + 1;

	/** Index of the queued item named by the form's `i` field, or -1. */
	const findQueued = (body: string) => {
		const id = Number(new URLSearchParams(body).get("i") ?? "");
		if (!Number.isFinite(id)) return -1;
		return queue.findIndex((item) => item.i === id);
	};

	const enqueue = (text: string, lang: Lang) => {
		const clipped = text.trim().slice(0, 2000);
		if (!clipped) throw new Error(tt(lang, "empty_input"));
		if (queue.length >= QUEUE_MAX) throw new Error(tt(lang, "queue_full", QUEUE_MAX));
		const item = { i: nextQueueId(), text: clipped, at: Date.now() };
		queue.push(item);
		armed = true;
		saveQueue();
		return item;
	};

	/** Deliver the head of the queue once the session is idle.
	 *  One message per settled run: sending starts a turn, and the next item waits for
	 *  `agent_settled`. The delayed retry covers the case where a hook swallows the
	 *  message and no run starts (otherwise the queue would stall for ever).
	 *  `expandPromptTemplates` matches what an immediate send from the page does: a
	 *  queued `/skill:foo` or prompt template must not silently become chat text. */
	const deliverNext = async () => {
		if (!armed || delivering || !ctx || queue.length === 0 || !ctx.isIdle()) return;
		delivering = true;
		runStarted = false;
		const item = queue[0];
		try {
			try {
				await pi.sendUserMessage(item.text, { expandPromptTemplates: true });
			} catch {
				// a run started between the idle check and the send; a steered message is
				// delivered as-is (pi expands templates only for an idle send)
				await pi.sendUserMessage(item.text, { deliverAs: "steer" });
			}
			queue = queue.filter((q) => q.i !== item.i);
			saveQueue();
		} catch {
			// still queued: the next settle tries again
		} finally {
			delivering = false;
		}
		// Only when nothing started: a hook may swallow the message and no run would
		// ever settle to pump the rest, which would stall the queue for ever.
		if (queue.length > 0 && !runStarted && ctx?.isIdle()) {
			setTimeout(() => {
				if (!runStarted) void deliverNext();
			}, 400);
		}
	};

	const reply = (res: ServerResponse, key: string, msg: string, req: IncomingMessage, code = 200) => {
		if (wantsJson(req)) {
			res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
			res.end(JSON.stringify({ ok: code < 400, msg }));
			return;
		}
		done(res, key, msg);
	};

	const done = (res: ServerResponse, key: string, msg: string) => {
		// POST-Redirect-GET: the confirmation is fetched with GET, so refreshing or
		// re-entering the page can never replay the action.
		res.writeHead(303, { Location: `/done/${key}?m=${encodeURIComponent(msg)}`, "Cache-Control": "no-store" });
		res.end();
	};

	const handle = async (action: string, res: ServerResponse, req: IncomingMessage, body = "") => {
		const viewer = "/";
		const lang = langOf(req);
		try {
			if (action === "approve" || action === "deny") {
				let pending: { id?: string; command?: string; pid?: number } = {};
				try {
					pending = JSON.parse(readFileSync(PENDING_FILE, "utf8"));
				} catch {
					// a second tap on a block that was just answered lands here; the old
					// wording ("nothing pending") read like a bug instead of "already done"
					reply(res, "approve", tt(lang, "already_answered"), req, 409);
					return;
				}
				// pending.json is shared by every pi process. Answering another session's
				// parking spot would approve a command the e-ink page never showed as this
				// session's, so refuse instead of guessing. 409, not 200: the viewer turns
				// any 2xx into "已允许" on the page.
				if (pending.pid !== process.pid) {
					reply(res, action, tt(lang, "other_session", pending.pid ?? "unknown"), req, 409);
					return;
				}
				// tmp+rename: the pi that parked this reads it in a tight loop, and a
				// half-written file parses as "no answer yet" forever
				const answerTmp = `${ANSWER_FILE}.${process.pid}.tmp`;
				writeFileSync(answerTmp, JSON.stringify({
					id: pending.id,
					approved: action === "approve",
					at: Date.now(),
				}));
				renameSync(answerTmp, ANSWER_FILE);
				// drop the parking spot right away: the page keeps polling, and a block
				// that is already answered must not stay tappable
				try {
					const still = JSON.parse(readFileSync(PENDING_FILE, "utf8")) as { id?: string };
					if (still?.id === pending.id) unlinkSync(PENDING_FILE);
				} catch {
					// already gone
				}
				reply(res, action, tt(lang, action === "approve" ? "allowed" : "denied"), req);
				return;
			}
			if (action === "new") {
				// A new session is a NEW pi process in a new terminal window - not this one.
				// `ctx.newSession()` ends the conversation the reader is looking at, and
				// `switchSession` (with a header cwd) moves this very process to another
				// directory; both hijack what is open. Nothing below touches the current
				// session, so this also works while a run is in progress.
				const dir = (new URLSearchParams(body).get("dir") ?? "").trim();
				if (!dir) {
					reply(res, "new", tt(lang, "new_no_dir"), req, 400);
					return;
				}
				const fresh = Date.now() - STATE_MAX_AGE_MS;
				const running = Object.values(readRegistry())
					.filter((e) => {
						const at = Date.parse(String((e as { updatedAt?: string })?.updatedAt ?? ""));
						return Number.isFinite(at) && at >= fresh;
					})
					.map((e) => String((e as { cwd?: string })?.cwd ?? ""));
				// the same gate the page uses: only directories the reader was offered
				if (!mayStartSessionIn(dir, PROJECT_ROOTS, running)) {
					reply(res, "new", tt(lang, "new_dir_refused", dir), req, 400);
					return;
				}
				// Answer first: the window takes seconds to come up, and the viewer waits
				// for the new session to register in bridges.json, not for this reply.
				reply(res, "new", tt(lang, "new_spawning", dir), req);
				try {
					spawnPiWindow(dir);
				} catch (err) {
					console.error("[eink] spawn failed:", (err as Error).message);
				}
				return;
			}
			if (action === "stop") {
				if (!ctx) throw new Error("no session context");
				const wasBusy = !ctx.isIdle();
				// Same meaning as Escape in the TUI: the queued messages go too, otherwise
				// "stop" would immediately start the next run. Reported, never silent.
				const dropped = queue.length;
				queue = [];
				saveQueue();
				ctx.abort();
				reply(res, "stop", tt(lang, wasBusy ? "aborted" : "nothing_running")
					+ (dropped > 0 ? tt(lang, "queue_dropped", dropped) : ""), req);
				return;
			}
			if (action === "qadd") {
				const clipped = (new URLSearchParams(body).get("text") ?? "").trim().slice(0, 2000);
				if (!clipped) {
					reply(res, "qadd", tt(lang, "empty_input"), req, 400);
					return;
				}
				if (!ctx) throw new Error("no session context");
				if (ctx.isIdle()) {
					// nothing to wait for: send it now and say so, instead of claiming it is
					// queued and then delivering it a millisecond later
					await pi.sendUserMessage(clipped, { expandPromptTemplates: true });
					reply(res, "qadd", tt(lang, "sent"), req);
					return;
				}
				enqueue(clipped, lang);
				reply(res, "qadd", tt(lang, "queued_n", queue.length), req);
				return;
			}
			if (action === "qrun") {
				// The reader's way out of "loaded from disk but not armed" (a resumed
				// session): 开始/恢复投递 is an explicit act, never a side effect.
				armed = true;
				reply(res, "qrun", tt(lang, "queue_started"), req);
				void deliverNext();
				return;
			}
			if (action === "qedit") {
				const clipped = (new URLSearchParams(body).get("text") ?? "").trim().slice(0, 2000);
				const idx = findQueued(body);
				if (idx < 0) {
					reply(res, "qedit", tt(lang, "queue_missing"), req, 404);
					return;
				}
				if (!clipped) {
					reply(res, "qedit", tt(lang, "empty_input"), req, 400);
					return;
				}
				queue[idx].text = clipped;
				saveQueue();
				reply(res, "qedit", tt(lang, "queue_edited", idx + 1), req);
				return;
			}
			if (action === "qdel") {
				const idx = findQueued(body);
				if (idx < 0) {
					reply(res, "qdel", tt(lang, "queue_missing"), req, 404);
					return;
				}
				queue.splice(idx, 1);
				saveQueue();
				reply(res, "qdel", tt(lang, "queue_deleted", idx + 1), req);
				return;
			}
			if (action === "qmove") {
				const idx = findQueued(body);
				if (idx < 0) {
					reply(res, "qmove", tt(lang, "queue_missing"), req, 404);
					return;
				}
				const dir = new URLSearchParams(body).get("d") === "-1" ? -1 : 1;
				const to = Math.min(queue.length - 1, Math.max(0, idx + dir));
				const [moved] = queue.splice(idx, 1);
				queue.splice(to, 0, moved);
				saveQueue();
				reply(res, "qmove", tt(lang, "queue_moved"), req);
				return;
			}
			if (action === "qclear") {
				const dropped = queue.length;
				queue = [];
				saveQueue();
				reply(res, "qclear", tt(lang, "queue_cleared", dropped), req);
				return;
			}
			if (action === "status") {
				if (!ctx) throw new Error("no session context");
				const rows = [
					[tt(lang, "status_title"), tt(lang, ctx.isIdle() ? "status_idle" : "status_busy")],
					[tt(lang, "status_queue"), tt(lang, ctx.hasPendingMessages() || queue.length > 0 ? "status_yes" : "status_no")],
					[tt(lang, "status_model"), ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : tt(lang, "unknown")],
					[tt(lang, "status_thinking"), ctx.thinkingLevel ?? tt(lang, "unknown")],
				];
				const body = rows
					.map(([k, v]) => `<p>${k}：<b>${escAttr(v)}</b></p>`)
					.join("") + `<p><a href="${viewer}">${tt(lang, "back")}</a></p>`;
				res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
				res.end(html(tt(lang, "status_title"), body));
				return;
			}
			if (action === "say") {
				// also gated below for every loopback-only action; kept here so a future
				// refactor of the dispatcher cannot quietly reopen the free-text path
				const text = new URLSearchParams(body).get("text")?.trim() ?? "";
				if (!text) {
					reply(res, "say", tt(lang, "empty_input"), req, 400);
					return;
				}
				if (!ctx) throw new Error("no session context");
				const clipped = text.slice(0, 2000);
				// Built-in slash commands live in the TUI, not in sendUserMessage:
				// expandPromptTemplates only covers extension commands, /skill: and
				// prompt templates. Anything built in that this bridge cannot perform
				// is refused loudly instead of being sent to the model as chat.
				const cmd = clipped.match(/^\/([a-z][a-z-]*)(?:\s+([\s\S]*))?$/i);
				if (cmd && ctx.isIdle()) {
					const name = cmd[1].toLowerCase();
					const rest = (cmd[2] ?? "").trim();
					const mapped = BRIDGE_COMMANDS[name];
					if (mapped) {
						const label: Record<string, string> = {
							reload: tt(lang, "reloading"),
							compact: tt(lang, "compacting"),
							quit: tt(lang, "quitting"),
							exit: tt(lang, "quitting"),
						};
						reply(res, "say", label[name] ?? "ok", req);
						await pi.sendUserMessage("/" + mapped + (rest ? " " + rest : ""),
							{ expandPromptTemplates: true });
						return;
					}
					if (TUI_ONLY_COMMANDS.has(name)) {
						reply(res, "say", tt(lang, "tui_only"), req, 400);
						return;
					}
				}
				if (ctx.isIdle()) {
					await pi.sendUserMessage(clipped, { expandPromptTemplates: true });
					reply(res, "say", tt(lang, "sent"), req);
					return;
				}
				// Busy: join the box's queue instead of pi's unreadable one, so the
				// message can be seen, edited, reordered and dropped from the page.
				enqueue(clipped, lang);
				reply(res, "say", tt(lang, "queued_n", queue.length), req);
				return;
			}
			if (action === "continue" || action === "review" || action === "execute") {
				const now = Date.now();
				const prev = lastFired.get(action) ?? 0;
				if (now - prev < DEDUPE_MS) {
					reply(res, action,
						tt(lang, "dedupe", Math.round((DEDUPE_MS - (now - prev)) / 1000)), req);
					return;
				}
				lastFired.set(action, now);
				try {
					const msg = await send(action, lang);
					reply(res, action, msg, req);
				} catch (err) {
					lastFired.delete(action);
					throw err;
				}
				return;
			}
			res.writeHead(404, { "Content-Type": "text/html; charset=utf-8" });
			res.end(html(tt(lang, "unknown_action"), `<p>${tt(lang, "unknown_action")}</p>`));
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			reply(res, "error", tt(lang, "failed", msg), req, 500);
		}
	};

	const requestHandler = (req: IncomingMessage, res: ServerResponse) => {
		const url = new URL(req.url ?? "/", "http://localhost");
		const ip = clientIp(req);
		const lang = langOf(req);
		const path = url.pathname;
		const action = path.startsWith("/act/") ? path.slice(5) : "";

		const refused = gate(ip, action, path, req.socket.localAddress ?? "", undefined, lang);
		if (refused) {
			if (wantsJson(req) || path === "/state") {
				reply(res, action || "state", refused, req, 403);
			} else {
				res.writeHead(403, { "Content-Type": "text/html; charset=utf-8" });
				res.end(html(tt(lang, "refused"),
					`<p>${tt(lang, "refused")}: ${escAttr(refused)} (${escAttr(ip || "unknown")})</p>`));
			}
			return;
		}

		const uas = uaAllowList();
		const ua = (req.headers["user-agent"] ?? "").toLowerCase();
		if (uas.length > 0 && !isLoopback(ip) && !uas.some((a) => ua.includes(a))) {
			res.writeHead(403, { "Content-Type": "text/html; charset=utf-8" });
			res.end(html(tt(lang, "refused"),
				`<p>${tt(lang, "refused")}: user agent not in the allow list</p>`));
			return;
		}

		const want = token();
		if (!want) {
			res.writeHead(503, { "Content-Type": "text/html; charset=utf-8" });
			res.end(html(tt(lang, "missing_token"),
				`<p>${tt(lang, "missing_token")}: ~/.pi/eink/token</p>`));
			return;
		}

		const hasCookie = cookies(req)[COOKIE] === want;
		if (!hasCookie) {
			if (url.searchParams.get("k") !== want) {
				res.writeHead(403, { "Content-Type": "text/html; charset=utf-8" });
				res.end(html(tt(lang, "unauthorized"),
					`<p>${tt(lang, "unauthorized")}: ?k=&lt;token&gt;</p>`));
				return;
			}
			res.setHeader("Set-Cookie", `${COOKIE}=${want}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax`);
		}

		if (path === "/state") {
			const usage = ctx?.getContextUsage();
			const body = JSON.stringify({
				idle: ctx ? ctx.isIdle() : true,
				// pending = pi's own queue (TUI typing, steers): not readable here
				pending: ctx ? ctx.hasPendingMessages() : false,
				// queued = the box's queue, which the page renders and edits
				queued: queue.length,
				// spawn = "/act/new" opens a window elsewhere instead of moving this
				// process: a bridge without it must not be offered the button
				spawn: true,
				armed,
				model: ctx?.model ? `${ctx.model.provider}/${ctx.model.id}` : null,
				thinking: ctx?.thinkingLevel ?? null,
				context: usage ? { tokens: usage.tokens, window: usage.contextWindow, percent: usage.percent } : null,
			});
			res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
			res.end(body);
			return;
		}

		if (req.method === "GET" && path === "/queue") {
			// Read by the viewer, which renders the page. Kept out of /state: that one is
			// polled every 1.2 s and only needs the count.
			const body = JSON.stringify({
				items: queue.map((item) => ({ i: item.i, text: item.text, at: item.at })),
				pending: ctx ? ctx.hasPendingMessages() : false,
				armed,
			});
			res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
			res.end(body);
			return;
		}

		if (req.method === "GET" && path === "/") {
			const direct = (req.headers.host ?? "").replace(/:\d+$/, "");
			if (!HOST_OK.test(direct)) {
				res.writeHead(400, { "Content-Type": "text/html; charset=utf-8" });
				res.end(html(tt(lang, "bad_host"), "<p>bad host</p>"));
				return;
			}
			res.writeHead(302, { Location: `http://${direct}:${VIEWER_PORT}/` });
			res.end();
			return;
		}

		if (path.startsWith("/done/")) {
			const key = path.slice(6);
			const msg = url.searchParams.get("m") ?? "";
			res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
			res.end(html(tt(lang, "done"),
			`<p>${escAttr(msg)}</p><p><a href="/">${tt(lang, "back")}</a></p>`));
			return;
		}

		if (req.method !== "POST" && action) {
			res.writeHead(405, { "Content-Type": "text/html; charset=utf-8" });
			res.end(html(tt(lang, "method"), `<p>${tt(lang, "post_only")}</p>`));
			return;
		}

		if (req.method === "POST") {
			const chunks: Buffer[] = [];
			let size = 0;
			req.on("data", (chunk: Buffer) => {
				size += chunk.length;
				if (size <= 8192) chunks.push(chunk);
			});
			req.on("end", () => {
				void handle(action, res, req, Buffer.concat(chunks).toString("utf8"));
			});
			return;
		}
		void handle(action, res, req);
	};

	// Resolves false when this process must run without a bridge, so the caller can
	// tell the user instead of leaving them wondering why the Kindle page is dead.
	const listen = (attempt = 0, offset = 0): Promise<boolean> =>
		new Promise((resolve, reject) => {
			const targetPort = PORT || preferredPort() + offset;
			const srv = createServer(requestHandler);
			srv.on("error", (err: NodeJS.ErrnoException) => {
				if (err.code !== "EADDRINUSE") {
					reject(err);
					return;
				}
				// Reload closes the old socket asynchronously, so a fresh runtime can briefly race it.
				try {
					srv.close();
				} catch {
					// never listened
				}
				// Someone else holds this port: another session (hash collision) or our own
				// stale socket from before /reload. Walk forward a few ports first, then
				// retry the same one a bounded number of times — never unbounded, because
				// session_start AWAITS this promise and an endless retry hangs /reload.
				if (offset < 12) {
					setTimeout(() => {
						void listen(attempt, offset + 1).then(resolve, reject);
					}, 120);
					return;
				}
				if (attempt >= MAX_LISTEN_ATTEMPTS) {
					logBridge(`no free bridge port in ${BASE_PORT}-${BASE_PORT + PORT_SPAN + 12}; bridge disabled here`);
					resolve(false);
					return;
				}
				setTimeout(() => {
					void listen(attempt + 1, 0).then(resolve, reject);
				}, Math.min(250 * (attempt + 1), 3000));
			});
			srv.listen(targetPort, BIND, () => {
				server = srv;
				boundPort = targetPort;
				publishState();
				resolve(true);
			});
		});

	const readRegistry = (): Record<string, unknown> => {
		try {
			return JSON.parse(readFileSync(REGISTRY_FILE, "utf8")) as Record<string, unknown>;
		} catch {
			return {};
		}
	};

	const writeRegistry = (mutate: (reg: Record<string, unknown>) => void) => {
		const reg = readRegistry();
		mutate(reg);
		try {
			// write + rename: concurrent heartbeats from several sessions would
			// otherwise overwrite each other's entries
			const tmp = REGISTRY_FILE + ".tmp";
			writeFileSync(tmp, JSON.stringify(reg, null, 2));
			renameSync(tmp, REGISTRY_FILE);
		} catch {
			// registry is best effort; the viewer falls back to no bridges
		}
	};

	const sessionKey = () => ctx?.sessionManager.getSessionId() ?? "unknown";

	const preferredPort = () => {
		if (PORT) return PORT;
		const id = sessionKey();
		let h = 0;
		for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 100000;
		return BASE_PORT + (h % PORT_SPAN);
	};

	const publishState = () => {
		if (!ctx) return;
		const payload = {
			sessionId: sessionKey(),
			sessionFile: ctx.sessionManager.getSessionFile() ?? null,
			port: boundPort || PORT,
			pid: process.pid,
			cwd: ctx.cwd ?? null,
			updatedAt: new Date().toISOString(),
		};
		try {
			writeFileSync(STATE_FILE, JSON.stringify(payload, null, 2));
		} catch {
			// non-fatal
		}
		writeRegistry((reg) => {
			reg[payload.sessionId] = payload;
		});
	};

	pi.registerCommand("eink:reload", {
		description: "Kindle: reload extensions, skills, prompts, themes",
		// the reply has to flush before the runtime (and this HTTP server) is replaced
		handler: async (_args, cctx) => {
			setTimeout(() => void cctx.reload(), 600);
		},
	});
	pi.registerCommand("eink:compact", {
		description: "Kindle: compact the session context",
		handler: async (args, cctx) => {
			const custom = args.trim();
			cctx.compact(custom ? { customInstructions: custom } : {});
		},
	});
	pi.registerCommand("eink:quit", {
		description: "Kindle: quit this pi session",
		handler: async (_args, cctx) => {
			setTimeout(() => cctx.shutdown(), 600);
		},
	});

	pi.on("session_start", async (event, sessionCtx) => {
		ctx = sessionCtx;
		// The queue is on disk, so it outlives the extension instance: load it, and arm
		// delivery only when this is a reload (the reader was mid-flight and expects the
		// messages to keep flowing). A resumed session shows the leftovers on the queue
		// page instead of running them: three messages queued before a crash must not
		// execute by themselves a week later.
		armed = event?.reason === "reload";
		const restored = loadQueue();
		if (restored > 0 && armed) void deliverNext();
		if (server) {
			// a new session in this process: the registry must name the new id and drop
			// the old one, or the picker offers a session whose buttons act on this one
			if (restored > 0) saveQueue();
			publishState();
			return;
		}
		const bound = await listen();
		if (!bound) {
			if (sessionCtx.hasUI) {
				sessionCtx.ui.notify(
					`eink bridge 没抢到端口 ${BASE_PORT}-${BASE_PORT + PORT_SPAN + 12}，本会话没有 Kindle 按钮`,
					"warning",
				);
			}
			return;
		}
		if (!heartbeat) {
			// keep bridge.json fresh: safety-gate treats a stale entry as "no bridge"
			heartbeat = setInterval(publishState, 60_000);
			heartbeat.unref?.();
		}
	});

	// The queue's delivery point: one message per settled run. `aborted` is ignored on
	// purpose - an abort clears the queue in the stop handler, and pi's own abort rule
	// ("continue queued messages") is not what a panic button should do here.
	pi.on("agent_start", async () => {
		runStarted = true;
	});

	pi.on("agent_settled", async () => {
		runStarted = false;
		await deliverNext();
	});

	pi.on("session_shutdown", async () => {
		if (heartbeat) {
			clearInterval(heartbeat);
			heartbeat = null;
		}
		// the queue file is written on every mutation; this only flushes the last state
		saveQueue();
		ctx = null;
		try {
			const state = JSON.parse(readFileSync(STATE_FILE, "utf8"));
			if (state.pid === process.pid) unlinkSync(STATE_FILE);
		} catch {
			// already gone
		}
		writeRegistry((reg) => {
			for (const [key, value] of Object.entries(reg)) {
				const entry = value as { pid?: number };
				if (entry?.pid === process.pid) delete reg[key];
			}
		});
		if (server) {
			server.close();
			server = null;
		}
	});
}
