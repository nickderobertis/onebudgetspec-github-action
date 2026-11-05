//! The saved job telemetry: written by `finish`, read back as ordinary budget measurements.
use std::path::Path;
use std::process::ExitCode;

use serde_json::{Map, Value, json};

use crate::cli::TelemetryArgs;
use crate::state::Failure;

/// The label proving a telemetry file is a report job's own.
pub const JOB_LABEL: &str = "ci-report";

/// What a live job's request figure covers. Only an external boundary sees every client, and
/// `gh`'s own record cannot tell a silent request from a local command.
const SCOPE: &str = "only requests made through gh are counted here; requests from any other client are not seen; a gh verb other than api that shows no request line counts as zero, since it cannot be told apart from a local command such as gh auth token";

/// One metric as a budget reports it.
pub struct Metric<'a> {
    /// The key a budget's `--metric` names.
    pub name: &'a str,
    /// The figure, or why it cannot be established. An unknown figure is never estimated.
    pub value: Result<f64, String>,
    /// Its breakdown, shown in the result's detail.
    pub detail: String,
}

/// Write the telemetry document carrying `labels` and `metrics` to `path`. `persistence` says
/// which artifact uploads the document cannot include: they happen in another process, and
/// are file transfers rather than GitHub API requests.
pub fn write(
    path: &Path,
    labels: &[String],
    metrics: &[Metric<'_>],
    persistence: &str,
) -> Result<(), Failure> {
    let metrics: Map<String, Value> = metrics
        .iter()
        .map(|metric| {
            (
                metric.name.to_owned(),
                json!({
                    "value": metric.value.as_ref().ok(),
                    "unavailable": metric.value.as_ref().err(),
                    "detail": metric.detail,
                }),
            )
        })
        .collect();
    let document = json!({
        "schema_version": 1,
        "labels": labels,
        "metrics": metrics,
        "persistence": persistence,
        "scope": SCOPE,
    });
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
    if let Some(reason) = entry["unavailable"].as_str() {
        return Err(format!("metric {} is unavailable: {reason}", args.metric));
    }
    let value = entry["value"]
        .as_f64()
        .filter(|value| value.is_finite())
        .ok_or_else(|| format!("it records no finite metric {}", args.metric))?;
    let detail = entry["detail"].as_str().unwrap_or_default().to_owned();
    Ok((value, detail))
}
