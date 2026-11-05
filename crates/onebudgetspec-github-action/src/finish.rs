//! `finish`: the measured interval ends when the main results are saved, and only then do
//! the commands that read this job's own telemetry run, their verdicts saved afterwards.
use std::collections::BTreeSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use onebudgetspec_core::comparison::{
    BudgetRun, ComparisonInput, ComparisonReport, RollingHistory,
};
use serde_json::Value;

use crate::catalog;
use crate::cli::FinishArgs;
use crate::proxy;
use crate::report;
use crate::state::{self, Failure, Phases, file};
use crate::telemetry::{self, Metric};
use crate::tools;

/// Every GitHub API request of a run, REST and GraphQL, retries and pages included.
const GITHUB_API_REQUESTS: &str = "report-github-api-requests-per-run";

/// Each own command's budget id and the telemetry file it reads.
type OwnInputs = Vec<(String, PathBuf)>;

/// Now, as the saved results record it.
pub fn timestamp() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

/// `finish`: prints whether a final upload is needed, for the action's output file.
///
/// A failure to save is a diagnostic: the exit stays what the saved results already say,
/// so a full disk neither fails a passing job nor hides a known budget failure.
pub fn run(args: &FinishArgs) -> Result<ExitCode, Failure> {
    complete(&args.out, &args.main_upload).or_else(|failure| {
        let saved = state::read_json(&args.out.join(file::BUDGET_RUN))
            .and_then(|run| saved_exit(run).ok_or(failure));
        match saved {
            Ok(status) => Ok(ExitCode::from(status)),
            Err(failure) => Err(failure),
        }
    })
}

/// The ordinary exit of a saved run, under its saved enforcement selection.
fn saved_exit(run: Value) -> Option<u8> {
    let current: BudgetRun = serde_json::from_value(run).ok()?;
    let report = ComparisonReport {
        schema_version: 1,
        current,
        rows: Vec::new(),
        diagnostics: Vec::new(),
    };
    u8::try_from(report.exit_code()).ok()
}

/// Stop the timer, save the telemetry, run the own commands and save their verdicts.
pub fn complete(out: &Path, main_upload: &str) -> Result<ExitCode, Failure> {
    let stopped_at = report::now();
    let state_directory = state::state_directory(out);
    let continuation = state::read_json(&state_directory.join(file::CONTINUATION))?;
    let wiring = report::wiring(&state_directory)?;
    let mut input: ComparisonInput =
        serde_json::from_value(state::read_json(&out.join(file::INPUT))?)
            .map_err(|error| Failure::output(format!("saved comparison input: {error}")))?;
    let own = own_inputs(&continuation, &input.current);
    let phased = continuation["phased"].as_bool().unwrap_or(false);
    let job_labels = job_labels(&continuation);
    let persistence = if !phased {
        "no artifact upload: this run saved its results locally"
    } else if own.is_empty() {
        "the one result upload ended before this was written"
    } else {
        "the main result upload ended before this was written; the final upload holding this file was still pending"
    };
    let metrics = metrics(&continuation, &wiring.log, Some(stopped_at), main_upload);
    telemetry::write(
        &out.join(file::TELEMETRY),
        &job_labels,
        &metrics,
        persistence,
    )?;
    let (own, not_run) =
        write_own_telemetry(own, &input.current, &job_labels, &metrics, persistence);
    let changed = !own.is_empty() || !not_run.is_empty();

    let mut diagnostics: Vec<String> = continuation["diagnostics"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    if phased && main_upload != "success" {
        diagnostics.push(format!(
            "the main result upload ended {main_upload}: the previous artifact stays intact and this run's entry is lost from the base history"
        ));
    }
    let core_cli = tools::locate(tools::CORE)?;
    let current = &mut input.current;
    for (id, reason) in not_run {
        if let Some(row) = current.unavailable.iter_mut().find(|row| row.id == id) {
            row.reason = reason;
        }
    }
    if changed {
        let ids: BTreeSet<&str> = own.iter().map(|(id, _)| id.as_str()).collect();
        let checked = report::check_partitioned(
            &core_cli,
            &wiring,
            &current.catalog,
            &ids,
            &current.enforcement_exclude_labels,
            report::deny_of(&continuation),
        );
        current
            .unavailable
            .retain(|row| !ids.contains(row.id.as_str()));
        current.unavailable.extend(checked.unavailable);
        current.results.results.extend(checked.report.results);
        current.completed_at = timestamp();
        carry_into_rolling(out, current)?;
        state::write_json(&out.join(file::BUDGET_RUN), current)?;
        state::write_json(&out.join(file::INPUT), &input)?;
    }
    let mut compared = report::compare(&core_cli, &out.join(file::INPUT))?;
    report::save_report(out, &mut compared, &diagnostics)?;
    let exit_status = continuation["invocation_exit"]
        .as_i64()
        .and_then(|code| i32::try_from(code).ok())
        .filter(|code| *code != 0)
        .unwrap_or_else(|| compared.exit_code());
    if phased {
        writeln!(std::io::stdout().lock(), "final-upload={changed}")
            .map_err(|error| Failure::output(format!("output failed: {error}")))?;
    }
    Ok(ExitCode::from(u8::try_from(exit_status).unwrap_or(1)))
}

/// Write the telemetry each own command reads. A command whose file cannot be written, or whose
/// figure could not be established, does not run: that is its row's reason and nothing more.
fn write_own_telemetry(
    own: OwnInputs,
    current: &BudgetRun,
    labels: &[String],
    metrics: &[Metric<'_>],
    persistence: &str,
) -> (OwnInputs, Vec<(String, String)>) {
    let mut writable = Vec::new();
    let mut unwritable = Vec::new();
    for (id, path) in own {
        let unknown = current
            .catalog
            .iter()
            .find(|budget| budget.id == id)
            .and_then(|budget| catalog::own_metric(&budget.command))
            .and_then(|name| metrics.iter().find(|metric| metric.name == name))
            .and_then(|metric| metric.value.as_ref().err());
        if let Some(reason) = unknown {
            unwritable.push((id, reason.clone()));
            continue;
        }
        match telemetry::write(&path, labels, metrics, persistence) {
            Ok(()) => writable.push((id, path)),
            Err(failure) => {
                unwritable.push((id, format!("its telemetry was not saved: {failure}")));
            }
        }
    }
    (writable, unwritable)
}

/// Each own command's id and the checkout path of the telemetry file it reads.
fn own_inputs(continuation: &Value, current: &BudgetRun) -> OwnInputs {
    if continuation["run_own"] != true {
        return Vec::new();
    }
    continuation["own"]
        .as_array()
        .map_or(&[][..], Vec::as_slice)
        .iter()
        .filter_map(|entry| {
            let id = entry["id"].as_str()?;
            let budget = current.catalog.iter().find(|budget| budget.id == id)?;
            let directory = Path::new(&budget.file).parent()?;
            let input = Path::new(entry["input"].as_str()?);
            Some((id.to_owned(), catalog::checkout_relative(directory, input)?))
        })
        .collect()
}

/// The labels the telemetry carries: the job's, and the reader's default, so a repository's own
/// `labels` cannot strand its telemetry budgets.
fn job_labels(continuation: &Value) -> Vec<String> {
    let mut labels: Vec<String> = continuation["job_labels"]
        .as_array()
        .map(|labels| {
            labels
                .iter()
                .filter_map(|label| label.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    if !labels.iter().any(|label| label == telemetry::JOB_LABEL) {
        labels.push(telemetry::JOB_LABEL.to_owned());
    }
    labels
}

/// Save what the main upload can carry: the requests so far, its timer still running.
pub fn write_interim_telemetry(
    out: &Path,
    continuation: &Value,
    log: &Path,
) -> Result<(), Failure> {
    telemetry::write(
        &out.join(file::TELEMETRY),
        &job_labels(continuation),
        &metrics(continuation, log, None, "pending"),
        "written before any result upload, all of them still pending",
    )
}

/// Only what the request log and clocks hold. Artifact uploads run in another process whose
/// traffic this tool cannot see, so none is counted here or estimated.
fn metrics<'a>(
    continuation: &Value,
    log: &Path,
    stopped_at: Option<f64>,
    main_upload: &str,
) -> Vec<Metric<'a>> {
    let lines = proxy::read_log(log);
    let reached = |phase: &str| {
        lines
            .iter()
            .filter(|line| line["reached"] == true && line["phase"] == phase)
            .count()
    };
    let retries = lines.iter().filter(|line| line["retry"] == true).count();
    let uninstrumented = lines
        .iter()
        .filter(|line| line["uninstrumented"] == true)
        .count();
    let commands = reached("commands");
    let (workflow, history, delivery) =
        (reached("workflow"), reached("history"), reached("delivery"));
    let total = commands + workflow + history + delivery;
    let phases = Phases::from_json(&continuation["phases"]);
    let breakdown = format!(
        "GitHub API requests made through gh (REST and GraphQL): commands={commands} workflow={workflow} archive={history} (QueryCost {}) delivery={delivery} including retries={retries}; artifact and storage transfers are not GitHub API requests and are not in this figure, main upload {main_upload}; seconds: install={:.2} check={:.2} archive={:.2} render={:.2} delivery={:.2}",
        continuation["history_cost"],
        phases.install,
        phases.check,
        phases.archive,
        phases.render,
        phases.delivery,
    );
    let mut metrics = vec![Metric {
        name: GITHUB_API_REQUESTS,
        value: if uninstrumented == 0 {
            Ok(f64::from(u32::try_from(total).unwrap_or(u32::MAX)))
        } else {
            Err(format!(
                "gh gave no record of its requests on {uninstrumented} call(s), so this run's GitHub API request count cannot be established"
            ))
        },
        detail: breakdown.clone(),
    }];
    if let Some(stopped_at) = stopped_at {
        let started_at = continuation["started_at"].as_f64().unwrap_or(stopped_at);
        metrics.push(Metric {
            name: "report-command-seconds",
            value: Ok((stopped_at - started_at).max(0.0)),
            detail: breakdown,
        });
    }
    metrics
}

/// Replace the newest rolling entry with the run now holding its own verdicts.
fn carry_into_rolling(out: &Path, current: &BudgetRun) -> Result<(), Failure> {
    let path = out.join(file::ROLLING);
    if !path.is_file() {
        return Ok(());
    }
    let mut history: RollingHistory = serde_json::from_value(state::read_json(&path)?)
        .map_err(|error| Failure::output(format!("saved rolling history: {error}")))?;
    if let Some(newest) = history.runs.last_mut() {
        *newest = current.clone();
    }
    state::write_json(&path, &history)
}
