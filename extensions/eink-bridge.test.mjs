/**
 * Tests for the eink bridge, run with `npm test`.
 *
 * The extension is loaded for real: a stubbed pi API collects what it would send,
 * and every request goes through its HTTP handler. HOME points at a temp dir, so
 * tokens, the allowlist and the bridge registry never touch ~/.pi/eink and two
 * test runs cannot collide.
 */
import { after, before, describe, it } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";

const HOME = mkdtempSync(join(tmpdir(), "pi-eink-test-"));
const EINK = join(HOME, ".pi/eink");
// random per run: a fixed weak token plus a LAN-bound port made the bridge
// reachable by anyone on the same network while the tests ran
const TOKEN = randomUUID();
// no fixed port on purpose: the suite exercises the per-session hash path, and the
// collision test below needs `preferredPort() + n` to be reachable
const TEST_PORT = 0;

const PENDING = join(EINK, "pending.json");
const ANSWER = join(EINK, "answer.json");

let handlers = {};
let commands = {};
let aborted = 0;
let sent = [];
let port = 0;
let mod;

/** Absolute paths are resolved from homedir() at module load, so HOME comes first. */
async function load() {
	mkdirSync(EINK, { recursive: true });
	writeFileSync(join(EINK, "token"), TOKEN);
	writeFileSync(join(EINK, "allow_ips"), "127.0.0.1\n# a CIDR the old env-only gate could not express\n192.168.0.0/16\n");
	writeFileSync(join(EINK, "uas"), "kindle build/\n");
	process.env.HOME = HOME;
	// 0.0.0.0: a request from this machine's LAN address is not loopback, which is
	// the only way to exercise the UA gate without a second host
	process.env.EINK_BIND = "0.0.0.0";
	mod = await import("../extensions/eink-bridge.ts");
}

const pi = {
	on: (name, fn) => {
		handlers[name] = fn;
	},
	registerCommand: (name, options) => { commands[name] = options; },
	sendUserMessage: async (text, opts) => {
		sent.push({ text, opts });
	},
};

const ctx = {
	hasUI: false,
	cwd: HOME,
	sessionManager: {
		getSessionId: () => "test-session",
		getSessionFile: () => join(HOME, "test-session.jsonl"),
	},
	isIdle: () => true,
	abort: () => { aborted += 1; },
	hasPendingMessages: () => false,
	model: { provider: "test", id: "model" },
	thinkingLevel: "high",
	getContextUsage: () => ({ tokens: 10, contextWindow: 1000, percent: 1 }),
};

const post = async (path, body = "") => {
	const res = await fetch(`http://127.0.0.1:${port}${path}`, {
		method: "POST",
		// the viewer always asks for JSON: status + msg is the contract the page reads
		headers: {
			Cookie: `pieink=${TOKEN}`,
			Accept: "application/json",
			"Content-Type": "application/x-www-form-urlencoded",
		},
		body,
	});
	return { status: res.status, json: await res.json().catch(() => null) };
};

/** The queue the bridge owns, as the viewer reads it to render /queue. */
const queued = async () => {
	const res = await fetch(`http://127.0.0.1:${port}/queue`, {
		headers: { Cookie: `pieink=${TOKEN}`, Accept: "application/json" },
	});
	return (await res.json()).items;
};

const park = (id, pid) => {
	writeFileSync(PENDING, JSON.stringify({
		id, pid, reason: "rm -rf on a protected path", command: "rm -rf /tmp/x",
		at: Date.now(), timeoutMs: 120_000,
	}));
};

describe("eink bridge", () => {
	before(async () => {
		await load();
		mod.default(pi);
		await handlers.session_start({}, ctx);
		const reg = JSON.parse(readFileSync(join(EINK, "bridges.json"), "utf8"));
		port = Object.values(reg)[0].port;
	});

	after(async () => {
		// stop the server, but keep HOME: the allowlist suites below read allow_ips
		await handlers.session_shutdown({});
	});

	it("registers this session in the bridge registry", () => {
		const reg = JSON.parse(readFileSync(join(EINK, "bridges.json"), "utf8"));
		assert.deepEqual(Object.keys(reg), ["test-session"]);
		// hashed off the session id, inside the 8010 + PORT_SPAN window
		assert.ok(reg["test-session"].port >= 8010 && reg["test-session"].port < 8010 + 40,
			`unexpected bridge port ${reg["test-session"].port}`);
		assert.equal(reg["test-session"].pid, process.pid);
	});

	it("reports state", async () => {
		const res = await fetch(`http://127.0.0.1:${port}/state`, {
			headers: { Cookie: `pieink=${TOKEN}` },
		});
		assert.equal(res.status, 200);
		const body = await res.json();
		assert.equal(body.idle, true);
		assert.equal(body.model, "test/model");
		assert.equal(body.context.window, 1000);
	});

	it("approves a parked command and clears the parking spot", async () => {
		park("p1", process.pid);
		assert.deepEqual(await post("/act/approve"), { status: 200, json: { ok: true, msg: "已允许" } });
		assert.deepEqual(JSON.parse(readFileSync(ANSWER, "utf8")).id, "p1");
		assert.equal(JSON.parse(readFileSync(ANSWER, "utf8")).approved, true);
		assert.equal(existsSync(PENDING), false);
	});

	it("answers 409 for a second tap instead of pretending it worked", async () => {
		const res = await post("/act/approve");
		assert.equal(res.status, 409);
		assert.equal(res.json.ok, false);
	});

	it("refuses a parking spot owned by another pi process", async () => {
		rmSync(ANSWER, { force: true });
		park("p2", 999_999);
		const res = await post("/act/deny");
		assert.equal(res.status, 409);
		assert.equal(existsSync(ANSWER), false);
	});

	it("sends free text into the session", async () => {
		rmSync(PENDING, { force: true });
		sent = [];
		assert.equal((await post("/act/say", "text=hello")).status, 200);
		assert.deepEqual(sent, [{ text: "hello", opts: { expandPromptTemplates: true } }]);
	});

	it("maps /compact to the extension command and refuses TUI-only built-ins", async () => {
		sent = [];
		assert.equal((await post("/act/say", "text=%2Fcompact")).status, 200);
		assert.equal(sent[0].text, "/eink:compact");
		const refused = await post("/act/say", "text=%2Fmodel");
		assert.equal(refused.status, 400);
		assert.equal(sent.length, 1);
	});

	it("rejects an empty message without bothering the agent", async () => {
		sent = [];
		const res = await post("/act/say", "text=");
		assert.equal(res.status, 400);
		assert.equal(sent.length, 0);
	});

	it("starts a new session through the extension command", async () => {
		sent = [];
		const res = await post("/act/new");
		assert.equal(res.status, 200);
		assert.equal(res.json.msg, "已新建会话");
		// the command goes out after the reply: creating a session replaces this
		// extension instance, and the reply is written by the server it closes
		await new Promise((r) => setTimeout(r, 300));
		assert.deepEqual(sent, [{ text: "/eink:new", opts: { expandPromptTemplates: true } }]);
	});

	it("refuses a directory outside the project roots", async () => {
		const res = await post("/act/new", "dir=" + encodeURIComponent("/etc"));
		assert.equal(res.status, 400);
		assert.match(res.json.msg, /不能在这个目录|Cannot start/);
	});

	it("refuses a new session while this one is running", async () => {
		// a stray tap on a 511px button must not throw away a turn in progress
		const idle = ctx.isIdle;
		ctx.isIdle = () => false;
		try {
			sent = [];
			const res = await post("/act/new");
			assert.equal(res.status, 409);
			await new Promise((r) => setTimeout(r, 200));
			assert.equal(sent.length, 0);
		} finally {
			ctx.isIdle = idle;
		}
	});

	it("redirects the bare bridge port to the viewer", async () => {
		const res = await fetch(`http://127.0.0.1:${port}/`, {
			redirect: "manual",
			headers: { Cookie: `pieink=${TOKEN}` },
		});
		assert.ok([301, 302].includes(res.status));
		assert.match(res.headers.get("location") ?? "", /^http:\/\//);
	});

	it("refuses a request without a cookie or a k parameter", async () => {
		const res = await fetch(`http://127.0.0.1:${port}/state`);
		assert.equal(res.status, 403);
	});

	it("accepts ?k=<token> once and seeds the cookie", async () => {
		const res = await fetch(`http://127.0.0.1:${port}/state?k=${TOKEN}`, { redirect: "manual" });
		assert.ok([200, 303].includes(res.status));
		assert.match(res.headers.get("set-cookie") ?? "", new RegExp(`pieink=${TOKEN}`));
	});

	it("answers a plain browser with html, not json", async () => {
		const res = await fetch(`http://127.0.0.1:${port}/done/say?m=ok`, {
			headers: { Cookie: `pieink=${TOKEN}` },
		});
		assert.equal(res.status, 200);
		const body = await res.text();
		assert.match(body, /<html/);
		assert.match(body, /ok/);
	});

	it("renders the status page for a browser", async () => {
		// status answers with its own HTML page, so it is a POST like every action
		const res = await fetch(`http://127.0.0.1:${port}/act/status`, {
			method: "POST",
			headers: { Cookie: `pieink=${TOKEN}`, Accept: "text/html" },
		});
		assert.equal(res.status, 200);
		assert.match(await res.text(), /Status|状态/);
	});

	it("answers 404 for an unknown action", async () => {
		const res = await fetch(`http://127.0.0.1:${port}/act/nope`, {
			method: "POST",
			headers: { Cookie: `pieink=${TOKEN}`, Accept: "application/json" },
		});
		assert.equal(res.status, 404);
	});

	it("keeps the language header for the answers", async () => {
		const res = await fetch(`http://127.0.0.1:${port}/act/status`, {
			method: "POST",
			headers: { Cookie: `pieink=${TOKEN}`, Accept: "text/html", "X-Eink-Lang": "en" },
		});
		assert.match(await res.text(), /Status/);
	});

	it("maps the slash commands it can perform", async () => {
		for (const cmd of ["/compact", "/reload", "/quit"]) {
			sent = [];
			const res = await post("/act/say", "text=" + encodeURIComponent(cmd));
			assert.equal(res.status, 200, cmd);
			assert.equal(sent.length, 1, cmd);
		}
	});

	it("passes an unknown slash command through as text", async () => {
		sent = [];
		const res = await post("/act/say", "text=" + encodeURIComponent("/foo bar"));
		assert.equal(res.status, 200);
		assert.equal(sent[0].text, "/foo bar");
	});

	it("ignores a second tap inside the dedupe window", async () => {
		const first = await post("/act/continue");
		const second = await post("/act/continue");
		assert.equal(first.status, 200);
		assert.match(second.json.msg, /忽略|Ignored/);
	});

	it("retries as a steer when a run starts between the idle check and the send", async () => {
		const original = pi.sendUserMessage;
		let calls = 0;
		pi.sendUserMessage = async (_text, opts) => {
			calls += 1;
			if (!opts) throw new Error("a run already started");
		};
		try {
			const res = await post("/act/execute");
			assert.equal(res.status, 200);
			assert.equal(calls, 2);
		} finally {
			pi.sendUserMessage = original;
		}
	});

	it("answers 405 to a GET on an action", async () => {
		const res = await fetch(`http://127.0.0.1:${port}/act/say`, {
			headers: { Cookie: `pieink=${TOKEN}` },
		});
		assert.equal(res.status, 405);
	});

	it("keeps ipv6 loopback and unknown addresses straight", () => {
		assert.equal(mod.ipAllowed("::1"), true);
		assert.equal(mod.ipAllowed("fe80::1"), false);
		assert.equal(mod.ipAllowed(""), false);
	});

	it("rejects a Host header that is not an address", async () => {
		const http = await import("node:http");
		const status = await new Promise((resolve, reject) => {
			const req = http.request({
				host: "127.0.0.1",
				port,
				path: "/",
				// a bare hostname is fine (the regex allows it); a colon is not
				headers: { Host: "evil:example", Cookie: `pieink=${TOKEN}` },
			}, (res) => {
				res.resume();
				resolve(res.statusCode);
			});
			req.on("error", reject);
			req.end();
		});
		assert.equal(status, 400);
	});

	it("stops a LAN peer whose user agent is not allowed", async () => {
		const os = await import("node:os");
		const lan = Object.values(os.networkInterfaces()).flat()
			.find((i) => i && i.family === "IPv4" && !i.internal)?.address;
		if (!lan) {
			return; // no LAN address on this machine: nothing to test
		}
		const res = await fetch(`http://${lan}:${port}/state`, {
			headers: { Cookie: `pieink=${TOKEN}`, "User-Agent": "curl/8" },
		});
		assert.equal(res.status, 403);
	});

	it("lets a LAN peer with the Kindle user agent read state", async () => {
		const os = await import("node:os");
		const lan = Object.values(os.networkInterfaces()).flat()
			.find((i) => i && i.family === "IPv4" && !i.internal)?.address;
		if (!lan) {
			return;
		}
		const res = await fetch(`http://${lan}:${port}/state`, {
			headers: { Cookie: `pieink=${TOKEN}`, "User-Agent": "Kindle Build/4.0-rc2" },
		});
		assert.equal(res.status, 200);
	});

	it("stops the current run", async () => {
		const res = await post("/act/stop");
		assert.equal(res.status, 200);
		assert.match(res.json.msg, /中止|Nothing|没有/);
	});

	it("queues text behind a running turn", async () => {
		const wasIdle = ctx.isIdle;
		ctx.isIdle = () => false;
		try {
			const res = await post("/act/say", "text=" + encodeURIComponent("later"));
			assert.equal(res.status, 200);
			assert.match(res.json.msg, /第 1 条|#1/);
		} finally {
			ctx.isIdle = wasIdle;
		}
		assert.deepEqual((await queued()).map((x) => x.text), ["later"]);
		await post("/act/qclear");
	});

	it("lists, reorders, edits and drops what was queued", async () => {
		await post("/act/qclear");
		const wasIdle = ctx.isIdle;
		ctx.isIdle = () => false;
		try {
			await post("/act/qadd", "text=" + encodeURIComponent("first"));
			const second = await post("/act/qadd", "text=" + encodeURIComponent("second"));
			assert.match(second.json.msg, /第 2 条|#2/);
			let items = await queued();
			assert.deepEqual(items.map((x) => x.text), ["first", "second"]);

			assert.equal((await post("/act/qmove", "i=" + items[1].i + "&d=-1")).status, 200);
			items = await queued();
			assert.deepEqual(items.map((x) => x.text), ["second", "first"]);

			const edit = await post("/act/qedit", "i=" + items[0].i + "&text=edited");
			assert.equal(edit.status, 200);
			assert.match(edit.json.msg, /已修改第|Updated/);
			items = await queued();
			assert.equal(items[0].text, "edited");

			const dropped = await post("/act/qdel", "i=" + items[0].i);
			assert.equal(dropped.status, 200);
			assert.match(dropped.json.msg, /第 1 条|#1/, "the page numbers rows by position");
			assert.deepEqual((await queued()).map((x) => x.text), ["first"]);

			// an id that is not in the queue is refused, not "deleted something else"
			assert.equal((await post("/act/qdel", "i=9999")).status, 404);
			assert.equal((await post("/act/qedit", "i=9999&text=x")).status, 404);

			const cleared = await post("/act/qclear");
			assert.match(cleared.json.msg, /1/);
			assert.deepEqual(await queued(), []);
		} finally {
			ctx.isIdle = wasIdle;
			await post("/act/qclear");
		}
	});

	it("delivers one queued message per settled run", async () => {
		await post("/act/qclear");
		const wasIdle = ctx.isIdle;
		ctx.isIdle = () => false;
		try {
			await post("/act/qadd", "text=" + encodeURIComponent("one"));
			await post("/act/qadd", "text=" + encodeURIComponent("two"));
		} finally {
			ctx.isIdle = wasIdle;
		}
		sent = [];
		await handlers.agent_settled({ aborted: false }, ctx);
		assert.deepEqual(sent.map((s) => s.text), ["one"]);
		// a queued message must be expanded like an immediate send, or a queued
		// /skill:foo would reach the model as chat text
		assert.deepEqual(sent[0].opts, { expandPromptTemplates: true });
		assert.deepEqual((await queued()).map((x) => x.text), ["two"], "the rest waits for the next run");
		await handlers.agent_settled({ aborted: false }, ctx);
		assert.deepEqual(sent.map((s) => s.text), ["one", "two"]);
		assert.deepEqual(await queued(), []);
	});

	it("does not pipeline a second message into a run that already started", async () => {
		// the retry that keeps a swallowed message from stalling the queue must not turn
		// "one per run" into "two at once" when a run starts slowly
		await post("/act/qclear");
		const wasIdle = ctx.isIdle;
		ctx.isIdle = () => false;
		try {
			await post("/act/qadd", "text=" + encodeURIComponent("one"));
			await post("/act/qadd", "text=" + encodeURIComponent("two"));
		} finally {
			ctx.isIdle = wasIdle;
		}
		sent = [];
		await handlers.agent_settled({ aborted: false }, ctx);   // delivers "one"
		await handlers.agent_start({}, ctx);                     // the run is now real
		await new Promise((r) => setTimeout(r, 700));
		assert.deepEqual(sent.map((s) => s.text), ["one"], "the rest waits for the next settle");
		assert.deepEqual((await queued()).map((x) => x.text), ["two"]);
		await post("/act/qclear");
	});

	it("keeps going when a hook swallows the message and no run starts", async () => {
		await post("/act/qclear");
		const wasIdle = ctx.isIdle;
		ctx.isIdle = () => false;
		try {
			await post("/act/qadd", "text=" + encodeURIComponent("one"));
			await post("/act/qadd", "text=" + encodeURIComponent("two"));
		} finally {
			ctx.isIdle = wasIdle;
		}
		sent = [];
		await handlers.agent_settled({ aborted: false }, ctx);
		await new Promise((r) => setTimeout(r, 700));   // no agent_start: nothing ran
		assert.deepEqual(sent.map((s) => s.text), ["one", "two"]);
		assert.deepEqual(await queued(), []);
	});

	it("drops the queue when the run is stopped, and says how many", async () => {
		await post("/act/qclear");
		const wasIdle = ctx.isIdle;
		ctx.isIdle = () => false;
		try {
			await post("/act/qadd", "text=a");
			await post("/act/qadd", "text=b");
		} finally {
			ctx.isIdle = wasIdle;
		}
		sent = [];
		const res = await post("/act/stop");
		assert.match(res.json.msg, /丢弃|dropped/);
		assert.deepEqual(await queued(), []);
		assert.deepEqual(sent, [], "stop must not immediately start the next run");
	});

	it("reloads the queue from the file on session_start", async () => {
		await post("/act/qclear");
		const wasIdle = ctx.isIdle;
		ctx.isIdle = () => false;
		try {
			await post("/act/qadd", "text=" + encodeURIComponent("in memory"));
			// what a /reload (or a resumed session) actually faces: the file is the state
			writeFileSync(join(EINK, "queue.json"), JSON.stringify({
				"test-session": [{ i: 99, text: "from the file", at: Date.now() }],
			}));
			await handlers.session_start({ reason: "new" }, ctx);
			assert.deepEqual((await queued()).map((x) => x.text), ["from the file"]);
		} finally {
			ctx.isIdle = wasIdle;
			await post("/act/qclear");
		}
		const file = JSON.parse(readFileSync(join(EINK, "queue.json"), "utf8"));
		assert.equal(file["test-session"], undefined, "an emptied queue leaves no dead entry");
	});

	it("does not run a resumed session's leftovers by itself", async () => {
		// three messages queued before a crash must not execute a week later
		await post("/act/qclear");
		writeFileSync(join(EINK, "queue.json"), JSON.stringify({
			"test-session": [{ i: 3, text: "left over", at: Date.now() - 5 * 864e5 }],
		}));
		sent = [];
		await handlers.session_start({ reason: "resume" }, ctx);
		await new Promise((r) => setTimeout(r, 200));
		assert.deepEqual(sent, [], "a resume shows the leftovers, it does not run them");
		assert.deepEqual((await queued()).map((x) => x.text), ["left over"]);
		const state = await (await fetch(`http://127.0.0.1:${port}/queue`, {
			headers: { Cookie: `pieink=${TOKEN}` },
		})).json();
		assert.equal(state.armed, false);
		// and a run settling in that session must not flush it either
		await handlers.agent_settled({ aborted: false }, ctx);
		await new Promise((r) => setTimeout(r, 150));
		assert.deepEqual(sent, [], "not even a settle may start a restored queue");

		// 开始投递 is the explicit way out
		assert.equal((await post("/act/qrun")).status, 200);
		await new Promise((r) => setTimeout(r, 150));
		assert.deepEqual(sent.map((s) => s.text), ["left over"]);
		assert.deepEqual(await queued(), []);
	});

	it("keeps delivering across a reload", async () => {
		// the reader was mid-flight: a reload must not strand what they queued
		await post("/act/qclear");
		const wasIdle = ctx.isIdle;
		ctx.isIdle = () => false;
		try {
			await post("/act/qadd", "text=" + encodeURIComponent("across"));
		} finally {
			ctx.isIdle = wasIdle;
		}
		sent = [];
		await handlers.session_start({ reason: "reload" }, ctx);
		await new Promise((r) => setTimeout(r, 150));
		assert.deepEqual(sent.map((s) => s.text), ["across"]);
	});

	it("sends an added message right away when the session is idle", async () => {
		await post("/act/qclear");
		sent = [];
		const res = await post("/act/qadd", "text=" + encodeURIComponent("now"));
		// the reply has to match what happens: it is sent, not queued
		assert.equal(res.json.msg, "已发送");
		assert.deepEqual(sent.map((s) => s.text), ["now"]);
		assert.deepEqual(await queued(), []);
	});

	it("refuses an empty message without a 500", async () => {
		const res = await post("/act/qadd", "text=");
		assert.equal(res.status, 400);
		assert.match(res.json.msg, /没有输入内容|Nothing to send/);
	});

	it("reports the queue count in /state", async () => {
		await post("/act/qclear");
		const wasIdle = ctx.isIdle;
		ctx.isIdle = () => false;
		try {
			await post("/act/qadd", "text=x");
		} finally {
			ctx.isIdle = wasIdle;
		}
		const res = await fetch(`http://127.0.0.1:${port}/state`, { headers: { Cookie: `pieink=${TOKEN}` } });
		assert.equal((await res.json()).queued, 1);
		await post("/act/qclear");
	});

	it("maps /new and refuses a TUI-only built-in", async () => {
		const created = await post("/act/say", "text=" + encodeURIComponent("/new"));
		assert.equal(created.status, 200);
		assert.match(created.json.msg, /新建|New session/);
		const refused = await post("/act/say", "text=" + encodeURIComponent("/settings"));
		assert.equal(refused.status, 400);
		assert.match(refused.json.msg, /TUI/);
	});

	it("reviews when asked", async () => {
		sent = [];
		const res = await post("/act/review");
		assert.equal(res.status, 200);
		assert.equal(sent.length, 1);
	});

	it("reports a missing token instead of crashing", async () => {
		const tokenPath = join(EINK, "token");
		const saved = readFileSync(tokenPath, "utf8");
		rmSync(tokenPath);
		try {
			const res = await fetch(`http://127.0.0.1:${port}/state`, {
				headers: { Cookie: `pieink=${TOKEN}` },
			});
			assert.equal(res.status, 503);
		} finally {
			writeFileSync(tokenPath, saved);
		}
	});

	it("refuses a POST to a path that is not an action", async () => {
		const res = await fetch(`http://127.0.0.1:${port}/nope`, {
			method: "POST",
			headers: { Cookie: `pieink=${TOKEN}`, Accept: "application/json" },
		});
		assert.equal(res.status, 404);
	});
});

describe("port collision", () => {
	let blocker;
	let firstPort;

	before(async () => {
		firstPort = port;                        // the port the suite above used
		const net = await import("node:net");
		blocker = net.createServer();
		await new Promise((resolve) => blocker.listen(firstPort, "0.0.0.0", resolve));
		mod.default(pi);
		await handlers.session_start({}, ctx);
	});

	after(async () => {
		await handlers.session_shutdown({});
		await new Promise((resolve) => blocker.close(resolve));
	});

	it("walks forward instead of dying when its port is taken", () => {
		const reg = JSON.parse(readFileSync(join(EINK, "bridges.json"), "utf8"));
		const now = reg["test-session"].port;
		assert.notEqual(now, firstPort);
		assert.ok(now >= 8010 && now < 8010 + 40, `unexpected bridge port ${now}`);
	});
});

describe("new session in a directory", () => {
	// pi has no cwd option on newSession(); the session HEADER carries the cwd and
	// switchSession() rebuilds the runtime around it. So the box writes a header-only
	// session file for the chosen directory and switches to it.
	const run = async (args) => {
		let switched = "";
		await commands["eink:new-in"].handler(args, {
			switchSession: async (path) => { switched = path; return { cancelled: false }; },
		});
		return switched;
	};

	it("writes an empty session whose header points at the directory, then switches", async () => {
		const dir = mkdtempSync(join(tmpdir(), "eink-project-"));
		const file = await run(dir);
		assert.ok(file.endsWith(".jsonl"), file);
		assert.ok(file.includes("--" + dir.replace(/^\//, "").replace(/[/:]/g, "-") + "--"), file);
		const header = JSON.parse(readFileSync(file, "utf8").trim());
		assert.equal(header.type, "session");
		assert.equal(header.cwd, dir);
		assert.equal(header.version, 3);
		assert.ok(header.id && header.timestamp);
	});

	it("ignores an empty argument", async () => {
		assert.equal(await run("  "), "");
	});
});

describe("mayStartSessionIn", () => {
	it("allows a project root, rejects the rest of the filesystem", async () => {
		const root = mkdtempSync(join(tmpdir(), "eink-root-"));
		const inside = join(root, "some-project");
		mkdirSync(inside);
		assert.equal(mod.mayStartSessionIn(inside, [root]), true);
		assert.equal(mod.mayStartSessionIn(root, [root]), true);
		assert.equal(mod.mayStartSessionIn(tmpdir(), [root]), false);
		assert.equal(mod.mayStartSessionIn(join(root, "missing"), [root]), false);
		// a file is not a directory
		const file = join(root, "a.txt");
		writeFileSync(file, "x");
		assert.equal(mod.mayStartSessionIn(file, [root]), false);
	});
});

describe("loopbackOnly", () => {
	it("keeps only read/stop reachable from the LAN", () => {
		assert.equal(mod.loopbackOnly("stop"), false);
		assert.equal(mod.loopbackOnly("status"), false);
		for (const action of ["say", "approve", "deny", "continue", "review", "execute", "new"]) {
			assert.equal(mod.loopbackOnly(action), true, action);
		}
		// no action at all is not an action: the handler gates on `action &&`
		assert.equal(mod.loopbackOnly(""), false);
	});
});

describe("mayProxy", () => {
	it("trusts loopback and nothing else by default", () => {
		assert.equal(mod.mayProxy("127.0.0.1", []), true);
		assert.equal(mod.mayProxy("192.168.3.9", []), false);
	});

	it("trusts an explicitly listed viewer host", () => {
		// a viewer on another machine proxies with its LAN address, not loopback
		assert.equal(mod.mayProxy("192.168.3.9", ["192.168.3.9"]), true);
		assert.equal(mod.mayProxy("192.168.3.10", ["192.168.3.9"]), false);
	});
});

describe("gate", () => {
	it("refuses a peer outside the allowlist", () => {
		assert.equal(mod.gate("8.8.8.8", "stop"), "不在允许列表");
	});

	it("lets a LAN peer stop and read status, but nothing else", () => {
		assert.equal(mod.gate("192.168.3.9", "stop", "", "192.168.3.45"), null);
		assert.equal(mod.gate("192.168.3.9", "status", "", "192.168.3.45"), null);
		for (const action of ["say", "approve", "deny", "continue", "review", "execute", "new"]) {
			assert.notEqual(mod.gate("192.168.3.9", action, "", "192.168.3.45"), null, action);
		}
	});

	it("treats /state like an action, not like a static page", () => {
		assert.notEqual(mod.gate("192.168.3.9", "", "/state", "192.168.3.45"), null);
		assert.equal(mod.gate("127.0.0.1", "", "/state", "192.168.3.45"), null);
	});

	it("keeps the queue readable only from this machine", () => {
		// it quotes what the reader typed: same rule as /state and free text
		assert.notEqual(mod.gate("192.168.3.9", "", "/queue", "192.168.3.45"), null);
		assert.equal(mod.gate("127.0.0.1", "", "/queue", "192.168.3.45"), null);
	});

	it("trusts a listed viewer host as a proxy", () => {
		const trusted = ["192.168.3.9"];
		assert.equal(mod.gate("192.168.3.9", "say", "", "192.168.3.45", trusted), null);
		assert.notEqual(mod.gate("192.168.3.10", "say", "", "192.168.3.45", trusted), null);
	});
});

describe("ipAllowed", () => {
	after(() => rmSync(HOME, { recursive: true, force: true }));

	it("always allows loopback", () => {
		assert.equal(mod.ipAllowed("127.0.0.1"), true);
		assert.equal(mod.ipAllowed("::1"), true);
	});

	it("honours CIDR entries from allow_ips, which the old env-only gate could not", () => {
		assert.equal(mod.ipAllowed("192.168.3.25"), true);
		assert.equal(mod.ipAllowed("192.168.139.3"), true);
		assert.equal(mod.ipAllowed("8.8.8.8"), false);
	});

	it("strips the IPv4-mapped prefix Node reports on dual-stack sockets", () => {
		assert.equal(mod.ipAllowed("::ffff:192.168.3.25"), true);
	});

	it("allows the host's own /24 by default, like the viewer", () => {
		// the two sides disagreeing here is what makes the page work while every
		// button returns 403, so the default is pinned on both
		assert.equal(mod.ipAllowed("10.1.2.3", "10.1.2.99"), true);
		assert.equal(mod.ipAllowed("10.2.2.3", "10.1.2.99"), false);
	});

	it("can be turned off with EINK_ALLOW_SAME_SUBNET=0", () => {
		process.env.EINK_ALLOW_SAME_SUBNET = "0";
		try {
			assert.equal(mod.ipAllowed("10.1.2.3", "10.1.2.99"), false);
		} finally {
			delete process.env.EINK_ALLOW_SAME_SUBNET;
		}
	});
});
