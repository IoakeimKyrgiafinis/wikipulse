# Spec 07b: News classifier with experiment tracking

Status: ready
Stage: 7. Model lifecycle
Owner: agent
Depends on: 07a

## Goal
Train models that predict whether a spike is real news, track every experiment with MLflow, and register the best one.

## What you'll learn
- **Baselines first:** a simple rule and logistic regression before anything fancy. Often they win.
- **Experiment tracking:** parameters, metrics and artifacts for every run, comparable side by side in the MLflow UI.
- **PyTorch basics:** a small neural network with a training loop you can read line by line.
- **Model evaluation:** precision-recall curves, choosing a threshold for the use case, and calibration.

## Outputs
- MLflow tracking server in `docker-compose.yml` (artifacts in MinIO, metadata in Postgres)
- `wikipulse/ml/train.py`: trains (1) the current rule-based detector as a baseline, (2) logistic regression, (3) gradient-boosted trees, (4) a small PyTorch model on the features plus a summary embedding; logs everything to MLflow
- `wikipulse/ml/evaluate.py`: test-set metrics, precision-recall curve, feature importance; logged as artifacts
- Registers the best model in the MLflow Model Registry with the dataset version as a tag. Choose it by test-set performance, weighing simplicity and speed, and explain the choice in the learning note

## Acceptance criteria
- Tests on a tiny synthetic dataset (training runs, metrics computed, registration works).
- All four models trained on the real dataset and visible in the MLflow UI; report the comparison table.

## Look at it yourself (optional, never blocking)
- Read the learning note: do you agree with the model the agent promoted, given accuracy, simplicity and speed?
