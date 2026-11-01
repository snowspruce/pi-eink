/**
 * E-ink action bridge.
 *
 * Serves a tiny HTTP endpoint that lets an e-ink Kindle on the same LAN push
 * whitelisted actions into THIS running session (not a new process).
 *
 * Only fixed actions exist; there is no free-text channel on purpose. The agent
 * has bash access, so a free-text endpoint on an unauthenticated LAN port would
 * be remote code execution.
 *
 * Auth: HttpOnly cookie `pieink`, seeded once via ?k=<token>, where the token
 * lives in ~/.pi/eink/token. Requests from IPs outside EINK_ALLOW_IPS are refused.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.EINK_BRIDGE_PORT ?? 8001);
const VIEWER_PORT = Number(process.env.EINK_PORT ?? 8000);
const TOKEN_FILE = join(homedir(), ".pi/eink/token");
const COOKIE = "pieink";
const DEFAULT_ALLOW = "192.168.3.25,192.168.3.45,127.0.0.1,::1";
// Kindle Voyage's browser reports "Kindle Build/4.0-rc2". Spoofable, so it only
// sits in front of the token check as a speed bump.
const DEFAULT_UA = "Kindle Build/";

function allowList(): Set<string> {
	return new Set(
		(process.env.EINK_ALLOW_IPS ?? DEFAULT_ALLOW)
			.split(",")
			.map((s) => s.trim())
			.filter(Boolean),
	);
}

function uaAllowList(): string[] {
	return (process.env.EINK_ALLOW_UA ?? DEFAULT_UA)
		.split(",")
		.map((s) => s.trim().toLowerCase())
		.filter(Boolean);
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

/** Host to advertise in links: the local address the client reached, not the Host header. */
function safeHost(req: IncomingMessage): string {
	const local = req.socket.localAddress ?? "";
	if (HOST_OK.test(local)) return local;
	const fallback = (req.headers.host ?? "").replace(/:\d+$/, "");
	return HOST_OK.test(fallback) ? fallback : "127.0.0.1";
}

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

	const handle = async (action: string, res: ServerResponse, viewerHost: string) => {
		const viewer = `http://${escAttr(viewerHost)}:${VIEWER_PORT}/`;
		try {
			if (action === "stop") {
				if (!ctx) throw new Error("no session context");
				const wasBusy = !ctx.isIdle();
				ctx.abort();
				res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
				res.end(html("停下", `<p>${wasBusy ? "已中止当前运行" : "当前没有运行"}</p>` +
					`<p><a href="${viewer}">返回</a></p>`));
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
			if (action === "continue" || action === "review" || action === "execute") {
				const msg = await send(action);
				res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
				res.end(html("完成", `<p>${msg}</p><p><a href="${viewer}">返回</a></p>`));
				return;
			}
			res.writeHead(404, { "Content-Type": "text/html; charset=utf-8" });
			res.end(html("未知动作", "<p>未知动作</p>"));
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			res.writeHead(500, { "Content-Type": "text/html; charset=utf-8" });
			res.end(html("失败", `<p>失败：${escAttr(msg)}</p>`));
		}
	};

	const requestHandler = (req: IncomingMessage, res: ServerResponse) => {
		const url = new URL(req.url ?? "/", "http://localhost");
		const allowed = allowList();
		const ip = clientIp(req);
		const host = safeHost(req);
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

		if (req.method !== "POST" && action) {
			res.writeHead(405, { "Content-Type": "text/html; charset=utf-8" });
			res.end(html("方法错误", "<p>只接受 POST</p>"));
			return;
		}

		void handle(action, res, host);
	};

	const listen = (attempt = 0): Promise<void> =>
		new Promise<void>((resolve, reject) => {
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
				// Retry indefinitely: a stale socket from the previous runtime can outlive a
				// reload. Giving up silently leaves old code serving the page.
				setTimeout(() => {
					void listen(attempt + 1).then(resolve, reject);
				}, Math.min(250 * (attempt + 1), 3000));
			});
			srv.listen(PORT, "0.0.0.0", () => {
				server = srv;
				resolve();
			});
		});

	pi.on("session_start", async (_event, sessionCtx) => {
		ctx = sessionCtx;
		if (server) return;
		await listen();
	});

	pi.on("session_shutdown", async () => {
		ctx = null;
		if (server) {
			server.close();
			server = null;
		}
	});
}
