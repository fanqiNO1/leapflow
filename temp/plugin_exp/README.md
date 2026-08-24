# Adaptive Plugin Experiment Workspace

This scratch workspace contains the next-generation adaptive plugin experiment.
It replaces the old lifecycle-only plugin experiment: mechanical lifecycle checks
now live in `tests/`, while this directory focuses on decision transparency.

## What this validates

- Environment fingerprint changes when structured environment facts change.
- A capability requirement is matched only against declared tool capabilities.
- Candidates that require unsupported platform capabilities are excluded with a
  visible reason, not merely given a lower score.
- Trust and reliability evidence can change the selected plugin.
- A capability plan orders selected plugin tools by declared capability
  dependencies.

## Run

From the repository root:

```bash
python temp/plugin_exp/scripts/adaptive_plugin_exp.py
```

The script writes a timestamped JSON report under `temp/plugin_exp/reports/`.
It is deterministic and does not call an LLM, network, daemon, or real plugin
installation path.
