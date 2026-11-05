//! The saved job telemetry: written by `finish`, read back as ordinary budget measurements.
use std::path::Path;
use std::process::ExitCode;

use serde_json::{Map, Value, json};

use crate::cli::TelemetryArgs;
use crate::state::Failure;

/// The label proving a telemetry file is a report job's own.
pub const JOB_LABEL: &str = "ci-report";

/// One metric as a budget reports it.
pub struct Metric<'a> {
    /// The key a budget's `--metric` names.
    pub name: &'a str,
    /// The figure.
    pub value: f64,
    /// Its breakdown, shown in the result's detail.
    pub detail: String,
}

/// Write the telemetry document carrying `labels` and `metrics` to `path`.
pub fn write(path: &Path, labels: &[String], metrics: &[Metric<'_>]) -> Result<(), Failure> {
    let metrics: Map<String, Value> = metrics
        .iter()
        .map(|metric| {
            (
                metric.name.to_owned(),
                json!({"value": metric.value, "detail": metric.detail}),
            )
        })
        .collect();
    let document = json!({"schema_version": 1, "labels": labels, "metrics": metrics});
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| {
            Failure::output(format!(
                "cannot create {}: {error}; choose a writable directory",
                parent.display()
            ))
        })?;
    }
    std::fs::write(path, document.to_string()).map_err(|error| {
        Failure::output(format!(
            "cannot write {}: {error}; choose a writable file",
            path.display()
        ))
    })
}

/// Report `--metric` from `--input` through the core reporter, or say what is wrong with it.
pub fn read(args: &TelemetryArgs) -> Result<ExitCode, Failure> {
    let (value, detail) = metric(args).map_err(|reason| {
        Failure::output(format!(
            "{}: {reason}; run the journey or job that records it, then re-run",
            args.input.display()
        ))
    })?;
    let reported = onebudgetspec_core::report(value, Some(&detail))
        .map_err(|error| Failure::output(format!("cannot save the result: {error}")))?;
    if !reported {
        use std::io::Write;
        writeln!(std::io::stdout().lock(), "{value} ({detail})")
            .map_err(|error| Failure::output(format!("output failed: {error}")))?;
    }
    Ok(ExitCode::SUCCESS)
}

fn metric(args: &TelemetryArgs) -> Result<(f64, String), String> {
    let bytes = std::fs::read(&args.input).map_err(|error| format!("cannot read it: {error}"))?;
    let document: Value =
        serde_json::from_slice(&bytes).map_err(|error| format!("not JSON: {error}"))?;
    if document["schema_version"] != 1 {
        return Err("schema_version is not 1".into());
    }
    let labelled = document["labels"]
        .as_array()
        .is_some_and(|labels| labels.iter().any(|label| label == args.label.as_str()));
    if !labelled {
        return Err(format!("it is not labelled {}", args.label));
    }
    let entry = &document["metrics"][&args.metric];
    let value = entry["value"]
        .as_f64()
        .filter(|value| value.is_finite())
        .ok_or_else(|| format!("it records no finite metric {}", args.metric))?;
    let detail = entry["detail"].as_str().unwrap_or_default().to_owned();
    Ok((value, detail))
}
