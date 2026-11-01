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
// fixed port: the per-session hash would land in the 8010+ range real bridges use
const TEST_PORT = 18019;

const PENDING = join(EINK, "pending.json");
const ANSWER = join(EINK, "answer.json");

let handlers = {};
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
	process.env.EINK_BRIDGE_PORT = String(TEST_PORT);
	process.env.EINK_BIND = "127.0.0.1";
	mod = await import("../extensions/eink-bridge.ts");
}

const pi = {
	on: (name, fn) => {
		handlers[name] = fn;
	},
	registerCommand: () => {},
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
		assert.equal(reg["test-session"].port, TEST_PORT);
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
});

describe("loopbackOnly", () => {
	it("keeps only read/stop reachable from the LAN", () => {
		assert.equal(mod.loopbackOnly("stop"), false);
		assert.equal(mod.loopbackOnly("status"), false);
		for (const action of ["say", "approve", "deny", "continue", "review", "execute"]) {
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
		for (const action of ["say", "approve", "deny", "continue", "review", "execute"]) {
			assert.notEqual(mod.gate("192.168.3.9", action, "", "192.168.3.45"), null, action);
		}
	});

	it("treats /state like an action, not like a static page", () => {
		assert.notEqual(mod.gate("192.168.3.9", "", "/state", "192.168.3.45"), null);
		assert.equal(mod.gate("127.0.0.1", "", "/state", "192.168.3.45"), null);
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
