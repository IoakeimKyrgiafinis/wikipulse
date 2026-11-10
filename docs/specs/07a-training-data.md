# Spec 07a: Training dataset

Status: done
Stage: 7. Model lifecycle
Owner: agent
Depends on: 02b, 06a

## Goal
Build a versioned, labeled dataset of spikes for training a classifier that predicts whether a spike is real news.

## What you'll learn
- **Weak supervision:** using an imperfect automatic label (appears on the Current Events portal) plus a small labeled sample, and their trade-offs.
- **Feature engineering on time series:** shape of the burst, editor counts, revert share, page age, category signals.
- **Data leakage:** features must only use information available at prediction time (the first 10 minutes of the spike, not its whole life). This is the most common way ML projects fool themselves.
- **Dataset versioning:** every model must be traceable to the exact data it was trained on.

## Outputs
- `wikipulse/ml/features.py`: features computed from the first `FEATURE_WINDOW_MIN=10` minutes of each spike only
- `wikipulse/ml/dataset.py`: builds a dataset (features, weak label, and the 02b label where available), split by time (train on earlier days, test on later days), saved as Parquet in `datasets/v<n>/` in MinIO with a manifest (date range, row counts, label balance, feature list, git commit)
- `uv run python -m wikipulse.ml.dataset build`

## Requirements
1. No feature uses data from after the feature window (a test proves it on a synthetic spike).
2. Time-based splits only, never random.

## Acceptance criteria
- Unit tests for features and the leakage guard.
- Build a dataset from all available history (backfill with the stream's `since` parameter as far back as its retention allows); report its size and label balance.

## Look at it yourself (optional, never blocking)
- Read the feature list and argue in your devlog which ones you expect to matter most. Check after training.
