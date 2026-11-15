-- Drift reports keyed by their reference data too (spec 11a review).

-- `wikipulse.ml.drift --reference-dataset vN` compares a model's live scores with the
-- training rows of *another* dataset version (a what-if, e.g. "would a model trained on
-- v5 still see drift?"). Stored under the same key as the model's own monitoring report,
-- it replaced that report, and the drift metrics read it as the model's status. Now the
-- key includes the reference dataset: '' is the model's own training data (the
-- monitoring report the metrics read), 'v5' a comparison with dataset v5.
ALTER TABLE drift_reports ADD COLUMN reference_dataset text NOT NULL DEFAULT '';

DO $$
DECLARE
    old_key text;
BEGIN
    SELECT conname INTO old_key
    FROM pg_constraint
    WHERE conrelid = 'drift_reports'::regclass AND contype = 'u';
    EXECUTE format('ALTER TABLE drift_reports DROP CONSTRAINT %I', old_key);
END $$;

ALTER TABLE drift_reports ADD CONSTRAINT drift_reports_key
    UNIQUE (model_name, model_version, run_id, period_start, period_end, reference_dataset);
