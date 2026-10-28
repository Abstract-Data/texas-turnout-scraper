import { DurableObject } from "cloudflare:workers";
import { RUN_EXIT, RUN_LOG, parseSyncMode, runEntrypoint, runReport, shouldRun, type SyncMode } from "./schedule";

interface Env {
	EV_INGEST: DurableObjectNamespace<EvIngest>;
	RUN_TOKEN: string;
	EV_SYNC_MODE: string;
	CLOUDFLARE_ACCOUNT_ID: string;
	CLOUDFLARE_KV_NAMESPACE_ID: string;
	EV_SNAPSHOT_BUCKET: string;
	CLOUDFLARE_API_TOKEN: string;
	R2_ACCESS_KEY_ID: string;
	R2_SECRET_ACCESS_KEY: string;
}

const ELECTION_ID = "53815";
// The sync's report is key=value lines; only these keys leave the Container. Rows never do.
const REPORT_KEYS = new Set(["status", "decoded_bytes", "sha256", "within_cap", "exit_code", "ev_date"]);
const DATE_RE = /^(0[1-9]|1[0-2])\/(0[1-9]|[12]\d|3[01])\/\d{4}$/;

// The cron and POST /run share this Durable Object, so it is the one place a run can start.
const INGEST = "ingest";
const RUN_KEY = "run";
const CHECK_MS = 60_000;
const MAX_RUN_MS = 3 * 60 * 60 * 1000;
// The 60 s alarm keeps the Durable Object active; this only bounds a lost alarm.
const INACTIVITY_TIMEOUT_MS = 15 * 60 * 1000;

type RunMode = Exclude<SyncMode, "off">;

interface RunState {
	mode: RunMode;
	trigger: "cron" | "manual";
	startedAt: number;
}

export interface SpikeResult {
	exit_code: number;
	seconds: number;
	report: Record<string, string>;
}

export function reportLines(stdout: string): Record<string, string> {
	const report: Record<string, string> = {};
	for (const line of stdout.split("\n")) {
		const eq = line.indexOf("=");
		const key = line.slice(0, eq);
		if (eq > 0 && REPORT_KEYS.has(key) && !(key in report)) {
			report[key] = line.slice(eq + 1).trim();
		}
	}
	return report;
}

export function syncArgs(date: string | null): string[] {
	const when = date ? ["--sync-mode", "single", "--date", date] : ["--sync-mode", "latest"];
	return [
		"python", "-m", "texas_turnout_scraper.early_vote_sync",
		"--election-id", ELECTION_ID,
		...when,
		"--payload-kind", "statewide_roster",
		"--http-backend", "cloudscraper",
		"--dry-run",
	];
}

async function run(container: Container, argv: string[]): Promise<{ exitCode: number; stdout: string }> {
	const output = await (await container.exec(argv)).output();
	return { exitCode: output.exitCode, stdout: new TextDecoder().decode(output.stdout) };
}

function log(event: string, fields: object): void {
	console.log(JSON.stringify({ event, ...fields }));
}

export class EvIngest extends DurableObject<Env> {
	constructor(ctx: DurableObjectState, env: Env) {
		super(ctx, env);
		// The timeout does not survive a restart, such as a deploy during a run.
		const container = ctx.container;
		if (container?.running) {
			void ctx.blockConcurrencyWhile(() => container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS));
		}
	}

	// Dry run only: hashes one day from the portal and writes nothing.
	async spike(date: string | null): Promise<SpikeResult> {
		const container = this.ctx.container!;
		const started = Date.now();
		if (!container.running) {
			container.start({
				image: container.images.ingest,
				instance: "standard-2",
				enableInternet: true,
				entrypoint: ["sleep", "infinity"],
			});
		}
		try {
			const output = await (await container.exec(syncArgs(date), { cwd: "/app" })).output();
			return {
				exit_code: output.exitCode,
				seconds: Math.round((Date.now() - started) / 1000),
				report: reportLines(new TextDecoder().decode(output.stdout)),
			};
		} finally {
			await container.destroy();
		}
	}

	async startRun(mode: RunMode, trigger: RunState["trigger"]): Promise<{ started: boolean; detail: string }> {
		const container = this.ctx.container!;
		// `running` is true as soon as start() returns, so a second caller sees it.
		if (container.running || (await this.ctx.storage.get<RunState>(RUN_KEY))) {
			return { started: false, detail: "a run is in progress" };
		}
		container.start({
			image: container.images.ingest,
			instance: "standard-2",
			enableInternet: true,
			entrypoint: runEntrypoint(mode),
			env: {
				CLOUDFLARE_ACCOUNT_ID: this.env.CLOUDFLARE_ACCOUNT_ID,
				CLOUDFLARE_KV_NAMESPACE_ID: this.env.CLOUDFLARE_KV_NAMESPACE_ID,
				EV_SNAPSHOT_BUCKET: this.env.EV_SNAPSHOT_BUCKET,
				CLOUDFLARE_API_TOKEN: this.env.CLOUDFLARE_API_TOKEN,
				R2_ACCESS_KEY_ID: this.env.R2_ACCESS_KEY_ID,
				R2_SECRET_ACCESS_KEY: this.env.R2_SECRET_ACCESS_KEY,
			},
		});
		await container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);
		await this.ctx.storage.put<RunState>(RUN_KEY, { mode, trigger, startedAt: Date.now() });
		await this.ctx.storage.setAlarm(Date.now() + CHECK_MS);
		log("ev-ingest started", { mode, trigger });
		return { started: true, detail: `${mode} run started` };
	}

	async status(): Promise<{ running: boolean; run: RunState | null }> {
		return {
			running: this.ctx.container?.running ?? false,
			run: (await this.ctx.storage.get<RunState>(RUN_KEY)) ?? null,
		};
	}

	async alarm(): Promise<void> {
		const state = await this.ctx.storage.get<RunState>(RUN_KEY);
		const container = this.ctx.container!;
		if (!state) {
			return;
		}
		const seconds = Math.round((Date.now() - state.startedAt) / 1000);
		if (!container.running) {
			log("ev-ingest lost", { ...state, seconds, detail: "Container stopped before recording an exit code" });
			await this.ctx.storage.delete(RUN_KEY);
			return;
		}
		const exit = await run(container, ["cat", RUN_EXIT]);
		if (exit.exitCode !== 0) {
			if (Date.now() - state.startedAt > MAX_RUN_MS) {
				log("ev-ingest timed out", { ...state, seconds });
				await this.finish(container);
				return;
			}
			await this.ctx.storage.setAlarm(Date.now() + CHECK_MS);
			return;
		}
		const output = await run(container, ["cat", RUN_LOG]);
		log("ev-ingest finished", {
			...state,
			seconds,
			exit_code: Number(exit.stdout.trim()),
			report: runReport(output.stdout),
		});
		await this.finish(container);
	}

	private async finish(container: Container): Promise<void> {
		await container.destroy();
		await this.ctx.storage.delete(RUN_KEY);
	}
}

async function tokenMatches(given: string, expected: string): Promise<boolean> {
	const enc = new TextEncoder();
	const [a, b] = await Promise.all([
		crypto.subtle.digest("SHA-256", enc.encode(given)),
		crypto.subtle.digest("SHA-256", enc.encode(expected)),
	]);
	return crypto.subtle.timingSafeEqual(a, b);
}

async function authorized(request: Request, env: Env): Promise<boolean> {
	const auth = request.headers.get("Authorization") ?? "";
	return Boolean(env.RUN_TOKEN) && auth.startsWith("Bearer ") && (await tokenMatches(auth.slice(7), env.RUN_TOKEN));
}

export default {
	async fetch(request, env): Promise<Response> {
		const url = new URL(request.url);
		const route = `${request.method} ${url.pathname}`;
		if (!["POST /spike", "POST /run", "GET /status"].includes(route)) {
			return new Response(null, { status: 404 });
		}
		if (!(await authorized(request, env))) {
			return new Response(null, { status: 401 });
		}
		const ingest = env.EV_INGEST.getByName(INGEST);
		if (route === "GET /status") {
			return Response.json({ mode: parseSyncMode(env.EV_SYNC_MODE), ...(await ingest.status()) });
		}
		if (route === "POST /run") {
			const mode = parseSyncMode(env.EV_SYNC_MODE);
			if (mode === "off") {
				return Response.json({ started: false, detail: "EV_SYNC_MODE is off" }, { status: 409 });
			}
			const result = await ingest.startRun(mode, "manual");
			return Response.json(result, { status: result.started ? 202 : 409 });
		}
		const date = url.searchParams.get("date");
		if (date !== null && !DATE_RE.test(date)) {
			return Response.json({ detail: "date must be MM/DD/YYYY" }, { status: 400 });
		}
		return Response.json(await env.EV_INGEST.getByName("spike").spike(date));
	},

	async scheduled(controller, env): Promise<void> {
		if (!shouldRun(new Date(controller.scheduledTime))) {
			return;
		}
		const mode = parseSyncMode(env.EV_SYNC_MODE);
		if (mode === "off") {
			log("ev-ingest cron skipped", { detail: "EV_SYNC_MODE is off" });
			return;
		}
		const { started, detail } = await env.EV_INGEST.getByName(INGEST).startRun(mode, "cron");
		log("ev-ingest cron", { started, detail });
	},
} satisfies ExportedHandler<Env>;
