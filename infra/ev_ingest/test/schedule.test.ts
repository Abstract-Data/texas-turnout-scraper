import assert from "node:assert/strict";
import { test } from "node:test";

import { centralHour, parseSyncMode, runEntrypoint, runReport, shouldRun } from "../src/schedule.ts";

const utc = (iso: string) => new Date(`${iso}Z`);

test("under CDT the 15, 22 and 03 UTC firings are 10:00, 17:00 and 22:00 Central", () => {
	assert.equal(centralHour(utc("2026-10-28T15:00:00")), 10);
	assert.ok(shouldRun(utc("2026-10-28T15:00:00")));
	assert.ok(shouldRun(utc("2026-10-28T22:00:00")));
	assert.ok(shouldRun(utc("2026-10-29T03:00:00")));
	for (const hour of ["16", "23"]) {
		assert.ok(!shouldRun(utc(`2026-10-28T${hour}:00:00`)), `${hour} UTC is not a CDT run hour`);
	}
	assert.ok(!shouldRun(utc("2026-10-29T04:00:00")));
});

test("after DST ends on 2026-11-01 the 16, 23 and 04 UTC firings run instead", () => {
	assert.equal(centralHour(utc("2026-11-02T16:00:00")), 10);
	assert.ok(shouldRun(utc("2026-11-02T16:00:00")));
	assert.ok(shouldRun(utc("2026-11-02T23:00:00")));
	assert.ok(shouldRun(utc("2026-11-03T04:00:00")));
	assert.ok(!shouldRun(utc("2026-11-02T15:00:00")));
	assert.ok(!shouldRun(utc("2026-11-02T22:00:00")));
});

test("the last run is 22:00 Central on election day and nothing runs after it", () => {
	assert.ok(shouldRun(utc("2026-11-04T04:00:00")));
	assert.ok(!shouldRun(utc("2026-11-04T16:00:00")));
	assert.ok(!shouldRun(utc("2026-11-05T04:00:00")));
});

test("unknown sync modes are off", () => {
	assert.equal(parseSyncMode(" Apply "), "apply");
	assert.equal(parseSyncMode("dry_run"), "dry_run");
	assert.equal(parseSyncMode("on"), "off");
	assert.equal(parseSyncMode(undefined), "off");
});

test("the entrypoint passes the mode as an argument, not inside the script", () => {
	const argv = runEntrypoint("apply");
	assert.deepEqual([argv[0], argv[1], argv[3]], ["bash", "-c", "apply"]);
	assert.ok(!argv[2].includes("apply"));
});

test("only key=value report lines leave the run log", () => {
	const log = [
		"INFO early-vote unchanged election_id=53815 ev_date=2026-10-24",
		"status=wrote",
		"sha256=abc123",
		"Traceback (most recent call last):",
		'rules={"exact": 2916, "zfill4": 6430}',
		"geo_status=wrote",
	].join("\n");
	assert.deepEqual(runReport(log), ["status=wrote", "sha256=abc123", "geo_status=wrote"]);
});
