//! `finish`: the measured interval ends when the main results are saved. Only then do the
//! commands that read this job's own telemetry run, the one comment post with their verdicts,
//! and the completed results save.
use std::collections::BTreeSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use onebudgetspec_core::comparison::{
    BudgetRun, ComparisonInput, ComparisonReport, RollingHistory,
};
use onebudgetspec_core::render_markdown;
use serde_json::Value;

use crate::catalog;
use crate::cli::FinishArgs;
use crate::proxy;
use crate::report;
use crate::state::{self, Destination, Failure, Phases, RollingDecision, file};
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

fn saved_document<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T, Failure> {
    serde_json::from_value(state::read_json(path)?)
        .map_err(|error| Failure::output(format!("saved {}: {error}", path.display())))
}

/// Stop the timer, save the telemetry, run the own commands, post the one comment and save the
/// completed verdicts.
pub fn complete(out: &Path, main_upload: &str) -> Result<ExitCode, Failure> {
    let stopped_at = report::now();
    let state_directory = state::state_directory(out);
    let mut continuation = state::read_json(&state_directory.join(file::CONTINUATION))?;
    let rolling = RollingDecision::from_continuation(&continuation)?;
    let wiring = report::wiring(&state_directory)?;
    let mut input: ComparisonInput = saved_document(&out.join(file::INPUT))?;
    // The comparison's copy has no omitted budgets; this one, which is saved, keeps them.
    let mut current: BudgetRun = saved_document(&out.join(file::BUDGET_RUN))?;
    let own = own_inputs(&continuation, &current);
    let phased = continuation["phased"].as_bool().unwrap_or(false);
    let job_labels = job_labels(&continuation);
    let persistence = persistence(phased, own.is_empty());
    let own_figures = metrics(
        &continuation,
        &wiring.log,
        Interval::Ended {
            stopped_at,
            delivered: false,
        },
        main_upload,
    );
    telemetry::write(
        &out.join(file::TELEMETRY),
        &job_labels,
        &own_figures,
        persistence,
    )?;
    let (own, not_run) = write_own_telemetry(own, &current, &job_labels, &own_figures, persistence);
    let changed = !own.is_empty() || !not_run.is_empty();

    let mut diagnostics = strings(&continuation["diagnostics"]);
    if phased && main_upload != "success" {
        diagnostics.push(format!(
            "the main result upload ended {main_upload}: the previous artifact stays intact and this run's entry is lost from the base history"
        ));
    }
    let core_cli = tools::locate(tools::CORE)?;
    apply_reasons(&mut current, not_run);
    let mut own_check_exit = None;
    if changed {
        own_check_exit = judge_own(
            &core_cli,
            &wiring,
            &own,
            &mut current,
            report::deny_of(&continuation),
        );
        carry_into_rolling(out, &current)?;
        state::write_json(&out.join(file::BUDGET_RUN), &current)?;
        input.current = report::without_omitted(&current, &strings(&continuation["omit_labels"]));
        state::write_json(&out.join(file::INPUT), &input)?;
    }
    report::settle_pending_rolling(
        out,
        rolling,
        &input.baseline,
        &current,
        own_check_exit,
        &mut diagnostics,
    )?;
    let phase_start = report::now();
    let mut compared = report::compare(&core_cli, &out.join(file::INPUT))?;
    if let Some(to) = destination(&continuation) {
        post_comment(
            &wiring,
            &to,
            &compared,
            &mut diagnostics,
            &mut continuation,
            phase_start,
        );
        // The saved file holds the whole job's requests; the interval it records ended earlier.
        telemetry::write(
            &out.join(file::TELEMETRY),
            &job_labels,
            &metrics(
                &continuation,
                &wiring.log,
                Interval::Ended {
                    stopped_at,
                    delivered: true,
                },
                main_upload,
            ),
            persistence,
        )?;
    }
    report::save_report(out, &mut compared, &diagnostics)?;
    let exit_status = job_exit(&continuation, own_check_exit, &compared);
    if phased {
        writeln!(std::io::stdout().lock(), "final-upload={changed}")
            .map_err(|error| Failure::output(format!("output failed: {error}")))?;
    }
    Ok(ExitCode::from(u8::try_from(exit_status).unwrap_or(1)))
}

fn apply_reasons(current: &mut BudgetRun, reasons: Vec<(String, String)>) {
    for (id, reason) in reasons {
        if let Some(row) = current.unavailable.iter_mut().find(|row| row.id == id) {
            row.reason = reason;
        }
    }
}

/// Which uploads a telemetry document cannot include.
fn persistence(phased: bool, no_own_commands: bool) -> &'static str {
    if !phased {
        "no artifact upload: this run saved its results locally"
    } else if no_own_commands {
        "the one result upload ended before this was written"
    } else {
        "the main result upload ended before this was written; the final upload holding this file was still pending"
    }
}

/// A check that could not run at all outranks the ordinary verdict.
fn job_exit(continuation: &Value, own_check_exit: Option<i32>, compared: &ComparisonReport) -> i32 {
    continuation["invocation_exit"]
        .as_i64()
        .and_then(|code| i32::try_from(code).ok())
        .filter(|code| *code != 0)
        .or(own_check_exit)
        .unwrap_or_else(|| compared.exit_code())
}

/// Run the own commands through ordinary check and settle their rows, answering a check that
/// could not run at all.
fn judge_own(
    core_cli: &Path,
    wiring: &proxy::Wiring,
    own: &OwnInputs,
    current: &mut BudgetRun,
    deny_credentials: bool,
) -> Option<i32> {
    let ids: BTreeSet<&str> = own.iter().map(|(id, _)| id.as_str()).collect();
    let checked = report::check_partitioned(
        core_cli,
        wiring,
        &current.catalog,
        &ids,
        &current.enforcement_exclude_labels,
        deny_credentials,
    );
    current
        .unavailable
        .retain(|row| !ids.contains(row.id.as_str()));
    current.unavailable.extend(checked.unavailable);
    current.results.results.extend(checked.report.results);
    current.completed_at = timestamp();
    checked.invocation_exit
}

/// Post the run's one comment, keeping what became of it in `diagnostics` and its seconds in the
/// saved phases.
fn post_comment(
    wiring: &proxy::Wiring,
    to: &Destination,
    compared: &ComparisonReport,
    diagnostics: &mut Vec<String>,
    continuation: &mut Value,
    render_start: f64,
) {
    let mut report = compared.clone();
    report.diagnostics.extend_from_slice(diagnostics);
    let body = render_markdown(&report);
    let post_start = report::now();
    continuation["phases"]["render"] = (post_start - render_start).into();
    let delivered = report::deliver(wiring, to, &report, body);
    diagnostics.extend(delivered.diagnostic);
    if !delivered.delivered {
        diagnostics.push(
            "report not delivered; this summary and the saved artifact remain available".into(),
        );
    }
    continuation["phases"]["delivery"] = (report::now() - post_start).into();
}

/// A saved list of strings, none when absent.
fn strings(saved: &Value) -> Vec<String> {
    saved
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default()
}

/// Where the saved run posts its comment, if it does.
fn destination(continuation: &Value) -> Option<Destination> {
    let to = &continuation["destination"];
    Some(Destination {
        change_url: to["change_url"].as_str()?.to_owned(),
        key: to["key"].as_str()?.to_owned(),
    })
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
    let mut labels = strings(&continuation["job_labels"]);
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
        &metrics(continuation, log, Interval::Running, "pending"),
        "written before any result upload, all of them still pending",
    )
}

/// Where a metrics document stands in the job.
#[derive(Clone, Copy)]
enum Interval {
    /// The main upload has not ended, so the timer is still running.
    Running,
    /// The timer stopped when it ended; the comment posts after that.
    Ended { stopped_at: f64, delivered: bool },
}

/// Only what the request log and clocks hold. Artifact uploads run in another process whose
/// traffic this tool cannot see, so none is counted here or estimated.
fn metrics<'a>(
    continuation: &Value,
    log: &Path,
    interval: Interval,
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
    let awaits_post = !continuation["destination"].is_null()
        && !matches!(
            interval,
            Interval::Ended {
                delivered: true,
                ..
            }
        );
    // Before the post, its requests and seconds are not known and this figure does not hold them.
    let (delivery, delivery_seconds) = if awaits_post {
        (
            "pending, the comment posts after this".to_owned(),
            "pending".to_owned(),
        )
    } else {
        (delivery.to_string(), format!("{:.2}", phases.delivery))
    };
    let breakdown = format!(
        "GitHub API requests made through gh (REST and GraphQL): commands={commands} workflow={workflow} archive={history} (history QueryCost {}, the rest merged-run reads and retries) delivery={delivery} including retries={retries}; artifact and storage transfers are not GitHub API requests and are not in this figure, main upload {main_upload}; seconds: install={:.2} check={:.2} archive={:.2} render={:.2} delivery={delivery_seconds}",
        continuation["history_cost"], phases.install, phases.check, phases.archive, phases.render,
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
    if let Interval::Ended { stopped_at, .. } = interval {
        let started_at = continuation["started_at"].as_f64().unwrap_or(stopped_at);
        metrics.push(Metric {
            name: "report-command-seconds",
            value: Ok((stopped_at - started_at).max(0.0)),
            detail: format!(
                "the interval runs from cold install to the end of the main result upload, before the comment post; {breakdown}"
            ),
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
