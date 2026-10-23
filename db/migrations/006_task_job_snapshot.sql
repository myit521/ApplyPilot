-- New reservations freeze their job input; existing tasks remain explicitly legacy.
ALTER TABLE workflow_tasks ADD COLUMN job_snapshot JSONB;
