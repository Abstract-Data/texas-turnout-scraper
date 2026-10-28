import { DurableObject } from "cloudflare:workers";

interface Env {
	EV_INGEST: DurableObjectNamespace<EvIngest>;
	RUN_TOKEN: string;
}

const ELECTION_ID = "53815";
// The sync's report is key=value lines; only these keys leave the Container. Rows never do.
const REPORT_KEYS = new Set(["status", "decoded_bytes", "sha256", "within_cap", "exit_code", "ev_date"]);
const DATE_RE = /^(0[1-9]|1[0-2])\/(0[1-9]|[12]\d|3[01])\/\d{4}$/;

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

export class EvIngest extends DurableObject<Env> {
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
}

async function tokenMatches(given: string, expected: string): Promise<boolean> {
	const enc = new TextEncoder();
	const [a, b] = await Promise.all([
		crypto.subtle.digest("SHA-256", enc.encode(given)),
		crypto.subtle.digest("SHA-256", enc.encode(expected)),
	]);
	return crypto.subtle.timingSafeEqual(a, b);
}

export default {
	async fetch(request, env): Promise<Response> {
		const url = new URL(request.url);
		if (request.method !== "POST" || url.pathname !== "/spike") {
			return new Response(null, { status: 404 });
		}
		const auth = request.headers.get("Authorization") ?? "";
		if (!env.RUN_TOKEN || !auth.startsWith("Bearer ") || !(await tokenMatches(auth.slice(7), env.RUN_TOKEN))) {
			return new Response(null, { status: 401 });
		}
		const date = url.searchParams.get("date");
		if (date !== null && !DATE_RE.test(date)) {
			return Response.json({ detail: "date must be MM/DD/YYYY" }, { status: 400 });
		}
		const result = await env.EV_INGEST.getByName("spike").spike(date);
		return Response.json(result);
	},
} satisfies ExportedHandler<Env>;
