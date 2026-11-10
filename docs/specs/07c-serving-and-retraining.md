# Spec 07c: Model serving, monitoring and retraining

Status: done
Stage: 7. Model lifecycle
Owner: agent
Depends on: 07b, 06c

## Goal
Use the registered model live to score new spikes, monitor it for drift, and retrain it on a schedule, promoting a new version only if it's better.

## What you'll learn
- **The full model lifecycle:** train, register, deploy, monitor, retrain, promote or roll back.
- **Model registry aliases** ("champion" and "challenger") and safe promotion rules.
- **Drift:** when incoming data changes (an election season, a holiday), models degrade. Detecting it with simple statistics on features and predictions.

## Outputs
- `wikipulse/ml/serve.py`: loads `models:/news-classifier@champion` and scores each spike after its feature window; scores stored on the spike and shown in the event feed
- `dags/retrain.py`: weekly dataset build, training, evaluation against the champion on the same test set, promotion only if better by a configurable margin
- `wikipulse/ml/drift.py`: compares this week's feature and score distributions with the training data; drift results stored and alerted in stage 9
- A rollback command that points the champion alias back to the previous version

## Acceptance criteria
- Tests for loading, scoring, the promotion rule and drift calculation (synthetic data).
- One full retraining cycle in Airflow, ending in either a promotion or a documented rejection.

## Look at it yourself (optional, never blocking)
- Roll back and forward once yourself. Explain the lifecycle in your devlog as you would in an interview.
