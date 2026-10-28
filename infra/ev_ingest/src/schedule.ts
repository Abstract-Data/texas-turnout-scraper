// Pure pieces of the ingest Worker, kept free of Workers imports so `node --test` can load them.

// Cron is UTC. Each Central run hour appears under both CDT and CST, so three of the six firings
// a day pass the gate (ADR-013). tests/verify/check_worker_cron_trigger.py pins this string.
export const CRON_SCHEDULE = "0 15,16,22,23,3,4 * * *";
const RUN_HOURS_CENTRAL = new Set([10, 17, 22]);
// 11:59:59 PM Central (CST) on election day, 2026-11-03.
const LAST_RUN_MS = Date.UTC(2026, 10, 4, 5, 59, 59);

const centralHourFormat = new Intl.DateTimeFormat("en-US", {
	timeZone: "America/Chicago",
	hour: "numeric",
	hourCycle: "h23",
});

export function centralHour(now: Date): number {
	const hour = centralHourFormat.formatToParts(now).find((part) => part.type === "hour");
	return Number(hour?.value);
}

export function shouldRun(now: Date): boolean {
	return now.getTime() <= LAST_RUN_MS && RUN_HOURS_CENTRAL.has(centralHour(now));
}

export type SyncMode = "off" | "dry_run" | "apply";

// Unknown values are off, as in early_vote_workflow.mode.parse_ev_sync_mode.
export function parseSyncMode(raw: unknown): SyncMode {
	const value = typeof raw === "string" ? raw.trim().toLowerCase() : "";
	return value === "dry_run" || value === "apply" ? value : "off";
}

export const RUN_LOG = "/tmp/ev_ingest.log";
export const RUN_EXIT = "/tmp/ev_ingest.exit";

// Container entrypoint: run the steps, keep their output in RUN_LOG (and the Container log),
// write the exit code atomically, then idle until the Durable Object reads it and destroys us.
export function runEntrypoint(mode: Exclude<SyncMode, "off">): string[] {
	const script = [
		"set -o pipefail",
		"cd /app",
		`bash scripts/ev_ingest_steps.sh "$0" 2>&1 | tee ${RUN_LOG}`,
		`echo $? > ${RUN_EXIT}.tmp && mv ${RUN_EXIT}.tmp ${RUN_EXIT}`,
		"exec sleep infinity",
	].join("\n");
	return ["bash", "-c", script, mode];
}

const REPORT_LINE = /^[a-z][a-z0-9_]*=\S*$/;
const MAX_REPORT_LINES = 80;

// The loader steps print counts and hashes as key=value lines; only those reach Workers Logs.
export function runReport(output: string): string[] {
	return output
		.split("\n")
		.map((line) => line.trim())
		.filter((line) => REPORT_LINE.test(line))
		.slice(-MAX_REPORT_LINES);
}
