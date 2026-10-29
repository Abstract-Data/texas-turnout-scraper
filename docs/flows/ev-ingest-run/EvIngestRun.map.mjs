// Chart labels for EvIngestRun. The reader is the operator: they see Workers Logs and the read API.
export const readerActions = ["StartRun"];

const days = (v) => Object.values(v.api);
const fresh = (v) => days(v).filter((d) => d === "new").length;
const stranded = (v) => Object.keys(v.served).filter((d) => v.served[d] && v.api[d] !== "new").length;

function context(v) {
	if (v.container === "running") {
		const got = Object.values(v.served).filter(Boolean).length;
		if (v.exit === "fail") return `Run in progress, portal returned 503 after ${got} day${got === 1 ? "" : "s"}`;
		if (v.exit === "ok") return "Run in progress, every day published";
		return `Run in progress, ${got} of ${days(v).length} days downloaded`;
	}
	if (v.logged === "finished_ok") return "Last run succeeded";
	if (v.logged === "finished_fail") return "Last run failed";
	return "Waiting for the cron";
}

function progress(v) {
	const n = days(v).length;
	const f = fresh(v);
	let text = f === n ? "API shows every day current" : f === 0 ? "API shows old data" : `API shows ${f} of ${n} days current`;
	const s = stranded(v);
	if (s > 0 && v.container === "stopped") text += `, ${s} downloaded day not published`;
	return text;
}

export function state(v, facts) {
	return `${context(v)}: ${progress(v)}`;
}

export const regionNames = ["What the logs say", "What the API serves"];

export function group(v, facts) {
	const f = fresh(v);
	return f === days(v).length ? "API current" : f === 0 ? "API stale" : "API partly current";
}
export const groupColors = { "API current": "#d1f2eb", "API stale": "#fdebd0", "API partly current": "#fcf3cf" };

const STEPS = {
	AlarmFinish: "the logs show the run finished",
	DayFails: "the portal keeps returning 503 for a day",
	FetchDay: "a day downloads",
	Publish: "every day is published to the API",
	StartRun: "the cron fires",
};
export function step(action, before, after) {
	return STEPS[action] ?? action;
}

export function guard(action, before, after) {
	if (action === "StartRun") return "10am, 5pm or 10pm Central, mode apply";
	return "";
}

export function timer(action, before, after) {
	if (action === "AlarmFinish") return 60;
}

export function cause(v, facts, invariant) {
	if (invariant === "NoStrandedDay") {
		return "the portal failed on a later day, so the run stopped before publishing the days it had already downloaded";
	}
	return null;
}
