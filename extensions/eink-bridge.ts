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
 * lives in ~/.pi/eink/token. Requests from IPs outside EINK_ALLOW_IPS are refused.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { appendFileSync, readFileSync, renameSync, writeFileSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const REGISTRY_FILE = join(homedir(), ".pi/eink/bridges.json");
const BASE_PORT = 8010;
const PORT_SPAN = 21;
const PORT = Number(process.env.EINK_BRIDGE_PORT ?? 0);
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
// Why a session stopped being the bridge owner; the TUI has no room to say it.
const BRIDGE_LOG = join(homedir(), ".pi/eink/bridge.log");
// Session control (new session, reload, compact, quit) is only reachable through
// ExtensionCommandContext, i.e. a command handler. Commands are namespaced
// "eink:" so they do not collide with the TUI built-ins of the same function.
const BRIDGE_COMMANDS: Record<string, string> = {
	new: "eink:new",
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
]);

const MAX_LISTEN_ATTEMPTS = 4;
// Binding the live bridge's port only works on the first try in practice; the
// retries exist for the old socket the previous runtime closed asynchronously.
// A pid in bridge.json proves nothing if the entry is ancient: pids get reused.
const STATE_MAX_AGE_MS = 5 * 60_000;
const DEFAULT_ALLOW = "192.168.3.25,192.168.3.45,127.0.0.1,::1";
// Kindle Voyage's browser reports "Kindle Build/4.0-rc2". Spoofable, so it only
// sits in front of the token check as a speed bump.
const DEFAULT_UA = "Kindle Build/";
const UA_FILE = join(homedir(), ".pi/eink/uas");

function allowList(): Set<string> {
	return new Set(
		(process.env.EINK_ALLOW_IPS ?? DEFAULT_ALLOW)
			.split(",")
			.map((s) => s.trim())
			.filter(Boolean),
	);
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

export default function einkBridge(pi: ExtensionAPI) {
	let server: ReturnType<typeof createServer> | null = null;
	let ctx: ExtensionContext | null = null;
	let heartbeat: ReturnType<typeof setInterval> | null = null;
	let boundPort = 0;
	// e-ink taps double-fire easily and a page reload replays a POST. Side-effecting
	// actions are deduped by name within this window.
	const DEDUPE_MS = 8000;
	const lastFired = new Map<string, number>();

	const send = async (action: Exclude<Action, "stop" | "status">) => {
		const text = PROMPTS[action];
		if (!ctx) throw new Error("no session context");
		// Idle: a plain user message starts a turn. Busy: queue after the current run.
		if (!ctx.isIdle() && ctx.hasPendingMessages()) {
			throw new Error("已有排队消息，等当前任务结束再点");
		}
		if (ctx.isIdle()) {
			try {
				await pi.sendUserMessage(text);
				return "已发送（立即执行）";
			} catch (err) {
				// A run can start between the idle check and the send; sendUserMessage
				// rejects without deliverAs in that case, so retry as a steer.
				await pi.sendUserMessage(text, { deliverAs: "steer" });
				return "已插入当前运行（立即执行）";
			}
		}
		await pi.sendUserMessage(text, { deliverAs: "followUp" });
		return "已排队（当前任务结束后执行）";
	};

	const wantsJson = (req: IncomingMessage) => (req.headers.accept ?? "").includes("application/json");

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
		try {
			if (action === "approve" || action === "deny") {
				let pending: { id?: string; command?: string; pid?: number } = {};
				try {
					pending = JSON.parse(readFileSync(PENDING_FILE, "utf8"));
				} catch {
					// a second tap on a block that was just answered lands here; the old
					// wording ("nothing pending") read like a bug instead of "already done"
					reply(res, "approve", "该确认已处理，无需再点", req, 409);
					return;
				}
				// pending.json is shared by every pi process. Answering another session's
				// parking spot would approve a command the e-ink page never showed as this
				// session's, so refuse instead of guessing. 409, not 200: the viewer turns
				// any 2xx into "已允许" on the page.
				if (pending.pid !== process.pid) {
					reply(res, action, `该确认属于另一个会话（pid ${pending.pid ?? "unknown"}）`, req, 409);
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
				reply(res, action, action === "approve" ? "已允许" : "已拒绝", req);
				return;
			}
			if (action === "stop") {
				if (!ctx) throw new Error("no session context");
				const wasBusy = !ctx.isIdle();
				ctx.abort();
				reply(res, "stop", wasBusy ? "已中止当前运行" : "当前没有运行", req);
				return;
			}
			if (action === "status") {
				if (!ctx) throw new Error("no session context");
				const rows = [
					["状态", ctx.isIdle() ? "空闲" : "运行中"],
					["排队", ctx.hasPendingMessages() ? "有" : "无"],
					["模型", ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "未知"],
					["思考", ctx.thinkingLevel ?? "未知"],
				];
				const body = rows
					.map(([k, v]) => `<p>${k}：<b>${escAttr(v)}</b></p>`)
					.join("") + `<p><a href="${viewer}">返回</a></p>`;
				res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
				res.end(html("状态", body));
				return;
			}
			if (action === "say") {
				// Text from the LAN would be RCE through the agent's bash tool; the viewer
				// proxies from 127.0.0.1, so narrowing here costs nothing.
				if (!isLoopback(clientIp(req))) {
					reply(res, "say", "自由文本只接受本机转发（通过 eink 页面发送）", req, 403);
					return;
				}
				const text = new URLSearchParams(body).get("text")?.trim() ?? "";
				if (!text) {
					reply(res, "say", "没有输入内容", req, 400);
					return;
				}
				if (!ctx) throw new Error("no session context");
				if (!ctx.isIdle() && ctx.hasPendingMessages()) {
					throw new Error("已有排队消息，等当前任务结束再点");
				}
				const clipped = text.slice(0, 2000);
				// Built-in slash commands live in the TUI, not in sendUserMessage:
				// expandPromptTemplates only covers extension commands, /skill: and
				// prompt templates. Anything built in that this bridge cannot perform
				// is refused loudly instead of being sent to the model as chat.
				const cmd = clipped.match(/^\/([a-z][a-z-]*)(?:\s+([\s\S]*))?$/i);
				if (cmd) {
					const name = cmd[1].toLowerCase();
					const rest = (cmd[2] ?? "").trim();
					const mapped = BRIDGE_COMMANDS[name];
					if (mapped) {
						const label: Record<string, string> = {
							new: "\u5df2\u65b0\u5efa\u4f1a\u8bdd",
							reload: "\u6b63\u5728\u91cd\u8f7d\u6269\u5c55\u4e0e\u914d\u7f6e",
							compact: "\u5f00\u59cb\u538b\u7f29\u4e0a\u4e0b\u6587",
							quit: "\u6b63\u5728\u9000\u51fa\u8be5 pi \u4f1a\u8bdd",
							exit: "\u6b63\u5728\u9000\u51fa\u8be5 pi \u4f1a\u8bdd",
						};
						reply(res, "say", label[name] ?? "ok", req);
						await pi.sendUserMessage("/" + mapped + (rest ? " " + rest : ""),
							{ expandPromptTemplates: true });
						return;
					}
					if (TUI_ONLY_COMMANDS.has(name)) {
						reply(res, "say",
							"\u8be5\u547d\u4ee4\u53ea\u80fd\u5728\u7535\u8111\u7684 TUI \u91cc\u6267\u884c\uff08Kindle \u4e0a\u53ef\u7528\uff1a/new /reload /compact /quit /skill:xxx\uff09",
							req, 400);
						return;
					}
				}
				if (ctx.isIdle()) {
					await pi.sendUserMessage(clipped, { expandPromptTemplates: true });
				} else {
					await pi.sendUserMessage(clipped, { deliverAs: "followUp", expandPromptTemplates: true });
				}
				reply(res, "say", "已发送", req);
				return;
			}
			if (action === "continue" || action === "review" || action === "execute") {
				const now = Date.now();
				const prev = lastFired.get(action) ?? 0;
				if (now - prev < DEDUPE_MS) {
					reply(res, action, `${Math.round((DEDUPE_MS - (now - prev)) / 1000)} 秒内重复点击，已忽略`, req);
					return;
				}
				lastFired.set(action, now);
				try {
					const msg = await send(action);
					reply(res, action, msg, req);
				} catch (err) {
					lastFired.delete(action);
					throw err;
				}
				return;
			}
			res.writeHead(404, { "Content-Type": "text/html; charset=utf-8" });
			res.end(html("未知动作", "<p>未知动作</p>"));
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			reply(res, "error", `失败：${msg}`, req, 500);
		}
	};

	const requestHandler = (req: IncomingMessage, res: ServerResponse) => {
		const url = new URL(req.url ?? "/", "http://localhost");
		const allowed = allowList();
		const ip = clientIp(req);
		const path = url.pathname;
		const action = path.startsWith("/act/") ? path.slice(5) : "";

		if (!allowed.has(ip)) {
			res.writeHead(403, { "Content-Type": "text/html; charset=utf-8" });
			res.end(html("拒绝", `<p>拒绝：${escAttr(ip || "unknown")} 不在允许列表</p>`));
			return;
		}

		const uas = uaAllowList();
		const ua = (req.headers["user-agent"] ?? "").toLowerCase();
		if (uas.length > 0 && !isLoopback(ip) && !uas.some((a) => ua.includes(a))) {
			res.writeHead(403, { "Content-Type": "text/html; charset=utf-8" });
			res.end(html("拒绝", "<p>拒绝：浏览器标识不在白名单</p>"));
			return;
		}

		const want = token();
		if (!want) {
			res.writeHead(503, { "Content-Type": "text/html; charset=utf-8" });
			res.end(html("缺少 token", "<p>缺少 ~/.pi/eink/token</p>"));
			return;
		}

		const hasCookie = cookies(req)[COOKIE] === want;
		if (!hasCookie) {
			if (url.searchParams.get("k") !== want) {
				res.writeHead(403, { "Content-Type": "text/html; charset=utf-8" });
				res.end(html("未授权", "<p>未授权：需要 ?k=&lt;token&gt;</p>"));
				return;
			}
			res.setHeader("Set-Cookie", `${COOKIE}=${want}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax`);
		}

		if (path === "/state") {
			const usage = ctx?.getContextUsage();
			const body = JSON.stringify({
				idle: ctx ? ctx.isIdle() : true,
				pending: ctx ? ctx.hasPendingMessages() : false,
				model: ctx?.model ? `${ctx.model.provider}/${ctx.model.id}` : null,
				thinking: ctx?.thinkingLevel ?? null,
				context: usage ? { tokens: usage.tokens, window: usage.contextWindow, percent: usage.percent } : null,
			});
			res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
			res.end(body);
			return;
		}

		if (req.method === "GET" && path === "/") {
			const direct = (req.headers.host ?? "").replace(/:\d+$/, "");
			if (!HOST_OK.test(direct)) {
				res.writeHead(400, { "Content-Type": "text/html; charset=utf-8" });
				res.end(html("错误", "<p>bad host</p>"));
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
			res.end(html("完成", `<p>${escAttr(msg)}</p><p><a href="/">返回</a></p>`));
			return;
		}

		if (req.method !== "POST" && action) {
			res.writeHead(405, { "Content-Type": "text/html; charset=utf-8" });
			res.end(html("方法错误", "<p>只接受 POST</p>"));
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
			srv.listen(targetPort, "0.0.0.0", () => {
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

	pi.registerCommand("eink:new", {
		description: "Kindle: start a new pi session",
		handler: async (_args, cctx) => {
			await cctx.newSession();
		},
	});
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

	pi.on("session_start", async (_event, sessionCtx) => {
		ctx = sessionCtx;
		if (server) return;
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

	pi.on("session_shutdown", async () => {
		if (heartbeat) {
			clearInterval(heartbeat);
			heartbeat = null;
		}
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
