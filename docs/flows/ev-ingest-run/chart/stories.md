## 1. Last run failed: API shows old data, 1 downloaded day not published

Rule broken: Every day the portal handed over during a run is in the API once that run ends.
Why: the portal failed on a later day, so the run stopped before publishing the days it had already downloaded.
There are 2 ways to get here in the model. This is the shortest.

0. Start: Waiting for the cron: API shows old data
1. The cron fires. Now: Run in progress, 0 of 2 days downloaded: API shows old data
2. A day downloads. Now: Run in progress, 1 of 2 days downloaded: API shows old data
3. The portal keeps returning 503 for a day. Now: Run in progress, portal returned 503 after 1 day: API shows old data
4. After 60 s, the logs show the run finished. Now: Last run failed: API shows old data, 1 downloaded day not published
