//! `report`: settle every budget from saved results or an ordinary check, then save one result
//! compared through core. No scalar is computed here and no measurement is run to fill a row.
use std::collections::BTreeSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use onebudgetspec_core::comparison::{
    BudgetRun, ComparisonInput, ComparisonReport, ResultNote, RollingHistory, UnavailableBudget,
    validate_history,
};
use onebudgetspec_core::delivery::{DeliveryResult, HistoricalFlag, PublishRequest};
use onebudgetspec_core::{CheckReport, CheckResult, ListedBudget, Verdict, exit, render_markdown};
use serde_json::{Value, json};

use crate::catalog::{self, Disposition};
use crate::cli::{Mode, ReportArgs, ReproduceArgs};
use crate::finish;
use crate::merged;
use crate::proxy::{self, Wiring};
use crate::saved::{self, NO_SAVED_RESULT};
use crate::state::{self, Destination, Failure, Phases, RollingDecision, file};
use crate::tools;

/// The base history holds at most this many commits.
const MAX_HISTORY_COMMITS: usize = 20;

/// Why a base run leaves the history as it found it.
const NOTHING_MEASURED: &str = "no rolling file saved: this run completed no measurement, so the next base run reads the one before it";

/// An own budget's row in the main results, which are saved before its telemetry exists.
const OWN_NOT_JUDGED: &str = "not judged when this file was saved: this job's own telemetry ends with this upload, and its verdict is in the completed results";

/// The current time as Unix seconds.
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |elapsed| elapsed.as_secs_f64())
}

/// The check run's ordinary answers, merged.
pub struct Checked {
    /// Every result, unchanged, minus measurements GitHub's refusal left incomplete and
    /// report-only budgets whose measurement failed.
    pub report: CheckReport,
    /// Rows that could not be measured, with why.
    pub unavailable: Vec<UnavailableBudget>,
    /// The exit of a check that could not run at all.
    pub invocation_exit: Option<i32>,
}

/// Run `ids` through ordinary check, enforced ones apart from report-only ones, each once.
pub fn check_partitioned(
    core: &Path,
    wiring: &Wiring,
    catalog: &[ListedBudget],
    ids: &BTreeSet<&str>,
    excluded: &[String],
    deny_credentials: bool,
) -> Checked {
    let selected: Vec<&ListedBudget> = catalog
        .iter()
        .filter(|budget| ids.contains(budget.id.as_str()))
        .collect();
    let (report_only, enforced): (Vec<_>, Vec<_>) = selected
        .iter()
        .partition(|budget| catalog::carries_any(&budget.labels, excluded));
    let mut checked = Checked {
        report: CheckReport {
            schema_version: 1,
            results: Vec::new(),
        },
        unavailable: Vec::new(),
        invocation_exit: None,
    };
    let partitions = [(&enforced, excluded, true), (&report_only, &[][..], false)];
    for (budgets, exclude, is_enforced) in partitions {
        if budgets.is_empty() {
            continue;
        }
        let call = ordinary_check(core, wiring, budgets, exclude, deny_credentials);
        match call {
            Ok(report) => checked.report.results.extend(report.results),
            Err((status, reason)) => {
                if is_enforced {
                    checked.invocation_exit = Some(status);
                }
                checked
                    .unavailable
                    .extend(budgets.iter().map(|budget| UnavailableBudget {
                        file: budget.file.clone(),
                        id: budget.id.clone(),
                        reason: format!("ordinary check could not run: {reason}"),
                    }));
            }
        }
    }
    let log = proxy::read_log(&wiring.log);
    let unavailable = &mut checked.unavailable;
    checked.report.results.retain(|result| {
        let blocked = proxy::blocked_reason(&log, &result.id);
        match unmeasured_reason(result, blocked, excluded) {
            Some(reason) => {
                unavailable.push(UnavailableBudget {
                    file: result.file.clone(),
                    id: result.id.clone(),
                    reason,
                });
                false
            }
            None => true,
        }
    });
    checked
}

/// Why an errored `result` reads as unavailable rather than as the ordinary `error`: GitHub
/// refused its reads, or it is report-only.
fn unmeasured_reason(
    result: &CheckResult,
    blocked: Option<String>,
    excluded: &[String],
) -> Option<String> {
    if result.verdict != Verdict::Error {
        return None;
    }
    blocked.or_else(|| {
        catalog::carries_any(&result.labels, excluded).then(|| {
            let message = result.error.as_deref().unwrap_or("no reason given");
            format!("report-only measurement failed: {message}")
        })
    })
}

fn ordinary_check(
    core: &Path,
    wiring: &Wiring,
    budgets: &[&&ListedBudget],
    exclude: &[String],
    deny_credentials: bool,
) -> Result<CheckReport, (i32, String)> {
    let files: BTreeSet<&str> = budgets.iter().map(|budget| budget.file.as_str()).collect();
    let mut command = Command::new(core);
    command.args(["check", "--json"]).args(&files);
    for budget in budgets {
        command.args(["--id", &budget.id]);
    }
    for label in exclude {
        command.args(["--exclude-label", label]);
    }
    proxy::wire(&mut command, wiring, "commands", deny_credentials);
    let output = command
        .stderr(Stdio::inherit())
        .output()
        .map_err(|error| (exit::INVALID, format!("cannot run onebudgetspec: {error}")))?;
    serde_json::from_slice(&output.stdout).map_err(|error| {
        (
            // A check that exited zero with no readable report did not succeed.
            output
                .status
                .code()
                .filter(|code| *code != 0)
                .unwrap_or(exit::ERROR),
            format!("exit {:?}, no report: {error}", output.status.code()),
        )
    })
}

/// Run the ordinary report over saved input through core's own command.
pub fn compare(core: &Path, input: &Path) -> Result<ComparisonReport, Failure> {
    let output = tools::capture(
        Command::new(core)
            .args(["report", "--json", "--input"])
            .arg(input),
        "onebudgetspec report",
    )?;
    let mut report: ComparisonReport = serde_json::from_slice(&output.stdout).map_err(|error| {
        Failure::invalid(format!(
            "onebudgetspec report refused {}: {}{error}",
            input.display(),
            String::from_utf8_lossy(&output.stderr)
        ))
    })?;
    for row in &mut report.rows {
        // Core appends each base commit's shortfall to a row's reason, which on a row with no
        // figure would name causes this reason exists not to claim. `baseline_n` still shows them.
        if row
            .unavailable
            .as_deref()
            .is_some_and(|reason| reason.starts_with(NO_SAVED_RESULT))
        {
            row.unavailable = Some(NO_SAVED_RESULT.to_owned());
        }
    }
    Ok(report)
}

/// Save the comparison and the Markdown beside it, diagnostics appended.
pub fn save_report(
    out: &Path,
    report: &mut ComparisonReport,
    diagnostics: &[String],
) -> Result<String, Failure> {
    report.diagnostics.extend_from_slice(diagnostics);
    let markdown = render_markdown(report);
    state::write_json(&out.join(file::REPORT), report)?;
    state::write_bytes(&out.join(file::MARKDOWN), markdown.as_bytes())?;
    Ok(markdown)
}

struct Config<'a> {
    args: &'a ReportArgs,
    head_sha: String,
    base_sha: String,
    excluded: Vec<String>,
    job_labels: Vec<String>,
    omitted: Vec<String>,
}

impl<'a> Config<'a> {
    fn validated(args: &'a ReportArgs) -> Result<Self, Failure> {
        let invalid = |what: &str| Failure::invalid(format!("{what}; see --help"));
        let (owner, name) = args
            .repo
            .split_once('/')
            .ok_or_else(|| invalid("--repo must be OWNER/NAME"))?;
        if owner.is_empty() || name.is_empty() || name.contains('/') {
            return Err(invalid("--repo must be OWNER/NAME"));
        }
        if args.attempt == 0 {
            return Err(invalid("--attempt must be positive"));
        }
        if args.artifact_name.trim().is_empty() {
            return Err(invalid("--artifact-name must not be blank"));
        }
        if args.mode == Mode::PullRequest && !args.change_url.starts_with("https://") {
            return Err(invalid("--mode pull-request needs an https --change-url"));
        }
        if args.mode == Mode::Base && args.base_ref.trim().is_empty() {
            return Err(invalid("--mode base needs --base-ref"));
        }
        if !args.saved_results_artifacts.is_empty() {
            if matches!(args.mode, Mode::Base | Mode::Privileged) {
                return Err(invalid(
                    "--saved-results-artifacts applies only to a pull-request or read-only run",
                ));
            }
            if args.saved_results_dir.is_none() {
                return Err(invalid(
                    "--saved-results-artifacts needs --saved-results-dir",
                ));
            }
        }
        let excluded = catalog::split_list(&args.enforcement_exclude_labels);
        let job_labels = catalog::split_list(&args.labels);
        let omitted = catalog::split_list(&args.omit_labels);
        let pattern = |label: &String| {
            label.starts_with(|first: char| first.is_ascii_lowercase())
                && label
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
        };
        if let Some(bad) = excluded
            .iter()
            .chain(&job_labels)
            .chain(&omitted)
            .find(|label| !pattern(label))
        {
            return Err(Failure::invalid(format!(
                "label {bad} must match ^[a-z][a-z0-9-]*$; see --help"
            )));
        }
        let head_sha = match &args.head_sha {
            Some(head) => head.clone(),
            None => git_head()?,
        };
        let base_sha = args.base_sha.clone().unwrap_or_else(|| head_sha.clone());
        Ok(Self {
            args,
            head_sha,
            base_sha,
            excluded,
            job_labels,
            omitted,
        })
    }
}

fn git_head() -> Result<String, Failure> {
    let output = tools::capture(Command::new("git").args(["rev-parse", "HEAD"]), "git")?;
    if !output.status.success() {
        return Err(Failure::invalid(
            "cannot read HEAD; run inside a checkout or pass --head-sha".into(),
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

/// Every budget's part in this run, settled: what it took, then its commands run.
struct Settled {
    current: BudgetRun,
    own: Vec<(String, PathBuf)>,
    invocation_exit: Option<i32>,
    diagnostics: Vec<String>,
    /// The looked-up workflow the history read still needs, if it reads the host.
    workflow: Option<Result<u64, String>>,
}

fn settle_rows(
    config: &Config<'_>,
    budgets: &[ListedBudget],
    core_cli: &Path,
    wiring: &Wiring,
    phases: &mut Phases,
) -> Settled {
    let args = config.args;
    let (shown, omitted_line) = shown_budgets(config, budgets);
    let (mut runnable, own, mut unavailable, mut diagnostics) =
        sort_catalog(config, &shown, wiring);
    diagnostics.extend(omitted_line);
    let phase_start = now();
    let workflow = host_workflow(config, wiring);
    let mut taken = Taken::default();
    if args.mode == Mode::Base {
        take_merged_run(
            config,
            wiring,
            workflow.as_ref(),
            &shown,
            (&mut runnable, &own, &mut unavailable),
            &mut taken,
            &mut diagnostics,
        );
    }
    phases.archive = now() - phase_start;
    if !args.saved_results_artifacts.is_empty() {
        take_saved_results(
            config,
            budgets,
            &shown,
            (&mut runnable, &own, &mut unavailable),
            &mut taken,
            &mut diagnostics,
        );
    }
    let phase_start = now();
    let mut checked = check_partitioned(
        core_cli,
        wiring,
        &shown,
        &runnable,
        &config.excluded,
        denies_credentials(args.mode),
    );
    phases.check = now() - phase_start;
    checked.report.results.append(&mut taken.results);
    let invocation_exit = checked.invocation_exit;
    let current = current_run(config, budgets.to_vec(), checked, unavailable, taken.notes);
    Settled {
        current,
        own,
        invocation_exit,
        diagnostics,
        workflow,
    }
}

/// `report`.
pub fn run(args: &ReportArgs) -> Result<ExitCode, Failure> {
    let config = Config::validated(args)?;
    let started_at = args.started_at.unwrap_or_else(now);
    let out = &args.out;
    let state_directory = state::state_directory(out);
    std::fs::create_dir_all(&state_directory).map_err(|error| {
        Failure::output(format!(
            "cannot create {}: {error}; choose a writable --out",
            state_directory.display()
        ))
    })?;
    let wiring = wiring(&state_directory)?;
    // A reused directory's old requests say nothing about this run.
    match std::fs::remove_file(&wiring.log) {
        Err(error) if error.kind() != std::io::ErrorKind::NotFound => {
            return Err(Failure::output(format!(
                "cannot reset {}: {error}; choose a writable --out",
                wiring.log.display()
            )));
        }
        _ => {}
    }
    let core_cli = tools::locate(tools::CORE)?;
    let files = catalog::discover(&catalog::split_list(&args.budget_files))?;
    let budgets = catalog::list(&core_cli, &files)?;
    let mut phases = Phases {
        install: args.install_seconds,
        ..Phases::default()
    };
    let Settled {
        current,
        own,
        invocation_exit,
        mut diagnostics,
        workflow,
    } = settle_rows(&config, &budgets, &core_cli, &wiring, &mut phases);

    let phase_start = now();
    let (prior, history_cost) = read_history(&config, &wiring, workflow, &mut diagnostics);
    phases.archive += now() - phase_start;
    diagnostics.extend(mode_diagnostics(args, &main_artifact(args, &own)));
    let rolling_decision =
        decide_rolling(&config, &current, invocation_exit, &own, prior.is_some());
    let rolling = if rolling_decision == RollingDecision::Settled {
        rolling_history(
            &config,
            prior.as_ref(),
            &current,
            invocation_exit,
            &mut diagnostics,
        )
    } else {
        None
    };
    write_rolling(out, rolling.as_ref())?;
    state::write_json(&out.join(file::BUDGET_RUN), &current)?;
    compare_and_save(
        &core_cli,
        args,
        without_omitted(&current, &config.omitted),
        prior,
        &diagnostics,
        &mut phases,
    )?;

    let continuation = json!({
        "started_at": started_at,
        "phases": phases.to_json(),
        "own": own.iter().map(|(id, input)| json!({"id": id, "input": input})).collect::<Vec<_>>(),
        "job_labels": config.job_labels,
        "diagnostics": diagnostics,
        "omit_labels": config.omitted,
        "destination": destination(args).map(|to| json!({"change_url": to.change_url, "key": to.key})),
        "invocation_exit": invocation_exit,
        "phased": args.phased,
        "deny_credentials": denies_credentials(args.mode),
        "history_cost": history_cost,
        "run_own": args.mode != Mode::Privileged,
        "rolling": rolling_decision.name(),
    });
    state::write_json(&state_directory.join(file::CONTINUATION), &continuation)?;
    if args.phased {
        finish::write_interim_telemetry(out, &continuation, &wiring.log)?;
        writeln!(
            std::io::stdout().lock(),
            "main-artifact={}",
            main_artifact(args, &own)
        )
        .map_err(|error| Failure::output(format!("output failed: {error}")))?;
        return Ok(ExitCode::SUCCESS);
    }
    let answer = finish::complete(out, "none")?;
    let markdown = std::fs::read_to_string(out.join(file::MARKDOWN)).unwrap_or_default();
    write!(std::io::stdout().lock(), "{markdown}")
        .map_err(|error| Failure::output(format!("output failed: {error}")))?;
    Ok(answer)
}

/// Where this run's one comment goes, if it posts one.
fn destination(args: &ReportArgs) -> Option<Destination> {
    (args.mode == Mode::PullRequest).then(|| Destination {
        change_url: args.change_url.clone(),
        key: args.key.clone(),
    })
}

/// Where the main results go. With own commands still to run, the final upload takes the name
/// the history reads, so a failure there can never destroy these immutable results.
fn main_artifact(args: &ReportArgs, own: &[(String, PathBuf)]) -> String {
    if own.is_empty() || args.mode == Mode::Privileged {
        args.artifact_name.clone()
    } else {
        format!("{}-main", args.artifact_name)
    }
}

fn denies_credentials(mode: Mode) -> bool {
    !matches!(mode, Mode::PullRequest | Mode::Base)
}

fn current_run(
    config: &Config<'_>,
    budgets: Vec<ListedBudget>,
    checked: Checked,
    mut unavailable: Vec<UnavailableBudget>,
    result_notes: Vec<ResultNote>,
) -> BudgetRun {
    let args = config.args;
    unavailable.extend(checked.unavailable);
    BudgetRun {
        schema_version: 1,
        repository: args.repo.clone(),
        head_sha: config.head_sha.clone(),
        base_ref: if args.base_ref.is_empty() {
            "none".into()
        } else {
            args.base_ref.clone()
        },
        run_id: args.run_id.clone(),
        run_url: args.run_url.clone(),
        attempt: args.attempt,
        completed_at: finish::timestamp(),
        catalog: budgets,
        results: checked.report,
        unavailable,
        enforcement_exclude_labels: config.excluded.clone(),
        result_notes,
    }
}

/// `run` as the comparison reads it: core gives every catalog entry a row, so an omitted budget
/// leaves this copy's catalog and stays in the saved `budget-run.json`.
pub fn without_omitted(run: &BudgetRun, omitted: &[String]) -> BudgetRun {
    let mut shown = run.clone();
    shown
        .catalog
        .retain(|budget| !catalog::carries_any(&budget.labels, omitted));
    shown
}

fn mode_diagnostics(args: &ReportArgs, main_artifact: &str) -> Vec<String> {
    let mut diagnostics = Vec::new();
    if args.mode == Mode::ReadOnly && !args.read_only_reason.is_empty() {
        diagnostics.push(format!(
            "read-only run, no comment posted: {}",
            args.read_only_reason
        ));
    }
    if args.mode == Mode::Privileged {
        diagnostics.push(
            "privileged event: no budget command ran and no comment was posted; run this on pull_request".into(),
        );
    }
    let saved_in = if main_artifact == args.artifact_name {
        format!("artifact {main_artifact}")
    } else {
        format!(
            "artifact {main_artifact}, then with this job's own-command verdicts in artifact {}",
            args.artifact_name
        )
    };
    diagnostics.push(format!(
        "complete results are in {saved_in} of {}; artifacts expire under the repository's retention setting, after which that baseline reads as unavailable",
        args.run_url
    ));
    diagnostics
}

/// Save the input and the comparison of the main results. The comment posts once, from `finish`,
/// where the job's own figure is known.
fn compare_and_save(
    core_cli: &Path,
    args: &ReportArgs,
    current: BudgetRun,
    prior: Option<RollingHistory>,
    diagnostics: &[String],
    phases: &mut Phases,
) -> Result<(), Failure> {
    let baseline: Vec<BudgetRun> = prior
        .map(|history| history.runs)
        .unwrap_or_default()
        .into_iter()
        .filter(|run| run.head_sha != current.head_sha)
        .collect();
    let input = ComparisonInput {
        schema_version: 1,
        current,
        baseline,
    };
    let input_path = args.out.join(file::INPUT);
    state::write_json(&input_path, &input)?;

    let phase_start = now();
    let mut report = compare(core_cli, &input_path)?;
    save_report(&args.out, &mut report, diagnostics)?;
    phases.render = now() - phase_start;
    Ok(())
}

/// The wiring of the run whose state directory is `state_directory`.
pub fn wiring(state_directory: &Path) -> Result<Wiring, Failure> {
    let proxy_directory = state_directory.join("bin");
    let proxy = proxy::install(&proxy_directory)?;
    Ok(Wiring {
        proxy,
        search_path: tools::search_path(&proxy_directory),
        log: state_directory.join(file::REST_LOG),
        real: proxy::real_gh(),
    })
}

type Sorted<'a> = (
    BTreeSet<&'a str>,
    Vec<(String, PathBuf)>,
    Vec<UnavailableBudget>,
    Vec<String>,
);

/// Decide every catalog entry's part in this run.
fn sort_catalog<'a>(
    config: &Config<'_>,
    budgets: &'a [ListedBudget],
    wiring: &Wiring,
) -> Sorted<'a> {
    let mut runnable = BTreeSet::new();
    let mut own = Vec::new();
    let mut unavailable = Vec::new();
    let mut diagnostics = Vec::new();
    for budget in budgets {
        let disposition = if config.args.mode == Mode::Privileged {
            Disposition::Unavailable(
                "this privileged event never runs budget commands, which a fork could have changed"
                    .into(),
            )
        } else {
            catalog::dispose(budget, &wiring.search_path)
        };
        match disposition {
            Disposition::Runnable => {
                runnable.insert(budget.id.as_str());
            }
            Disposition::Own { input } => {
                own.push((budget.id.clone(), input));
                unavailable.push(UnavailableBudget {
                    file: budget.file.clone(),
                    id: budget.id.clone(),
                    reason: OWN_NOT_JUDGED.into(),
                });
            }
            Disposition::Unavailable(reason) => {
                // With saved results asked for, a row left with none claims no cause.
                if config.args.saved_results_artifacts.is_empty()
                    && catalog::carries_any(&budget.labels, &config.job_labels)
                {
                    diagnostics.push(format!(
                        "{} is labelled as measurable in this job, yet {reason}",
                        budget.id
                    ));
                }
                unavailable.push(UnavailableBudget {
                    file: budget.file.clone(),
                    id: budget.id.clone(),
                    reason,
                });
            }
        }
    }
    (runnable, own, unavailable, diagnostics)
}

/// The budgets that get a row, and the one line counting the rest.
fn shown_budgets(
    config: &Config<'_>,
    budgets: &[ListedBudget],
) -> (Vec<ListedBudget>, Option<String>) {
    let (omitted, shown): (Vec<_>, Vec<_>) = budgets
        .iter()
        .cloned()
        .partition(|budget| catalog::carries_any(&budget.labels, &config.omitted));
    let labels: Vec<&str> = config
        .omitted
        .iter()
        .filter(|label| omitted.iter().any(|budget| budget.labels.contains(label)))
        .map(String::as_str)
        .collect();
    let label_word = if labels.len() == 1 { "label" } else { "labels" };
    let labels = labels.join(", ");
    let line = match omitted.len() {
        0 => None,
        1 => Some(format!(
            "1 budget is left out because a pull request cannot measure it ({label_word} {labels})."
        )),
        count => Some(format!(
            "{count} budgets are left out because a pull request cannot measure them ({label_word} {labels})."
        )),
    };
    (shown, line)
}

/// What rows came from elsewhere than a command run in this job.
#[derive(Default)]
struct Taken {
    results: Vec<CheckResult>,
    notes: Vec<ResultNote>,
}

/// The parts of `sort_catalog`'s answer a taken result changes.
type Rows<'r, 'a> = (
    &'r mut BTreeSet<&'a str>,
    &'r [(String, PathBuf)],
    &'r mut Vec<UnavailableBudget>,
);

/// Settle `budget` with `result`: its command does not run and it has no unavailable reason.
fn take(
    result: CheckResult,
    budget: &ListedBudget,
    runnable: &mut BTreeSet<&str>,
    unavailable: &mut Vec<UnavailableBudget>,
    taken: &mut Taken,
) {
    runnable.remove(budget.id.as_str());
    unavailable.retain(|row| row.file != budget.file || row.id != budget.id);
    taken.results.push(result);
}

/// A base push: each row not this job's own takes the merged pull request's figure, if it has one.
fn take_merged_run(
    config: &Config<'_>,
    wiring: &Wiring,
    workflow: Option<&Result<u64, String>>,
    shown: &[ListedBudget],
    (runnable, own, unavailable): Rows<'_, '_>,
    taken: &mut Taken,
    diagnostics: &mut Vec<String>,
) {
    // A run naming its saved history reads nothing from the host.
    let Some(workflow) = workflow else {
        return;
    };
    let args = config.args;
    let read = workflow
        .as_ref()
        .map_err(String::clone)
        .and_then(|workflow| {
            merged::read(
                wiring,
                &args.repo,
                &config.head_sha,
                *workflow,
                &args.artifact_name,
            )
        });
    let run = match read {
        Ok(run) => run,
        Err(reason) => {
            diagnostics.push(format!(
                "merged pull request's figures not read, so each row stands as this job has it: {reason}"
            ));
            return;
        }
    };
    let mut count = 0;
    for budget in shown {
        if own.iter().any(|(id, _)| *id == budget.id) {
            continue;
        }
        // An errored result holds no figure to carry, and the merge is not this job's failure.
        let Some(found) = saved::latest_for(&run.results, budget)
            .filter(|result| result.verdict != Verdict::Error)
        else {
            continue;
        };
        match saved::adopted(found, budget) {
            Ok(result) => {
                take(result, budget, runnable, unavailable, taken);
                count += 1;
            }
            Err(reason) => diagnostics.push(format!(
                "{}'s figure from {} ignored: {reason}",
                budget.id, run.source
            )),
        }
    }
    diagnostics.push(format!("{count} rows are the figures of {}", run.source));
}

/// A pull request: each row not this job's own takes its saved result, keeping its verdict; what
/// nothing can settle reads as having none, claiming no cause.
fn take_saved_results(
    config: &Config<'_>,
    budgets: &[ListedBudget],
    shown: &[ListedBudget],
    (runnable, own, unavailable): Rows<'_, '_>,
    taken: &mut Taken,
    diagnostics: &mut Vec<String>,
) {
    let args = config.args;
    if args.saved_results_download == "failure" {
        diagnostics.push(format!(
            "downloading the {} artifacts failed, so this report holds what arrived",
            args.saved_results_artifacts
        ));
    }
    let results = args
        .saved_results_dir
        .as_deref()
        .map(|directory| saved::read(directory, diagnostics))
        .unwrap_or_default();
    let unknown: BTreeSet<&str> = results
        .iter()
        .map(|result| result.id.as_str())
        .filter(|id| !budgets.iter().any(|budget| budget.id == *id))
        .collect();
    for id in unknown {
        diagnostics.push(format!(
            "saved result {id} ignored: the catalog holds no such budget"
        ));
    }
    let head_committed = (!results.is_empty())
        .then(|| saved::committer_date(&config.head_sha))
        .transpose()
        .unwrap_or_else(|reason| {
            diagnostics.push(format!(
                "cannot tell replayed results from fresh ones, so none is marked: the head {}: {reason}",
                config.head_sha
            ));
            None
        });
    for row in unavailable
        .iter_mut()
        .filter(|row| !own.iter().any(|(id, _)| *id == row.id))
    {
        NO_SAVED_RESULT.clone_into(&mut row.reason);
    }
    for budget in shown {
        if own.iter().any(|(id, _)| *id == budget.id) {
            continue;
        }
        let Some(found) = saved::latest_for(&results, budget) else {
            continue;
        };
        let result = match saved::adopted(found, budget) {
            Ok(result) => result,
            Err(reason) => {
                diagnostics.push(format!("saved result {} ignored: {reason}", budget.id));
                continue;
            }
        };
        if let Some(reason) = unmeasured_reason(&result, None, &config.excluded) {
            runnable.remove(budget.id.as_str());
            unavailable.retain(|row| row.file != budget.file || row.id != budget.id);
            unavailable.push(UnavailableBudget {
                file: budget.file.clone(),
                id: budget.id.clone(),
                reason,
            });
            continue;
        }
        taken
            .notes
            .extend(head_committed.and_then(|date| saved::replay_note(&result, date)));
        take(result, budget, runnable, unavailable, taken);
    }
}

/// This run's workflow, when anything will read the host for it; looked up once, at most.
fn host_workflow(config: &Config<'_>, wiring: &Wiring) -> Option<Result<u64, String>> {
    let args = config.args;
    if args.mode == Mode::Privileged || args.base_ref.is_empty() || args.saved_history.is_some() {
        return None;
    }
    Some(match args.workflow {
        Some(workflow) => Ok(workflow),
        None => look_up_workflow(args, wiring),
    })
}

/// The prior rolling history, or nothing with its reasons in `diagnostics`, and its
/// `QueryCost` when the plugin read it.
fn read_history(
    config: &Config<'_>,
    wiring: &Wiring,
    workflow: Option<Result<u64, String>>,
    diagnostics: &mut Vec<String>,
) -> (Option<RollingHistory>, Value) {
    let args = config.args;
    if args.mode == Mode::Privileged {
        return (None, Value::Null);
    }
    if args.base_ref.is_empty() {
        diagnostics.push("baseline unavailable: this run names no base branch".into());
        return (None, Value::Null);
    }
    if let Some(saved) = &args.saved_history {
        return (read_saved_history(config, saved, diagnostics), Value::Null);
    }
    let answer = workflow
        .unwrap_or_else(|| Err("no workflow names the history to read".into()))
        .and_then(|workflow| read_host_history(config, wiring, workflow, diagnostics));
    match answer {
        Ok(answer) => answer,
        Err(reason) => {
            diagnostics.push(format!("baseline unavailable: {reason}"));
            (None, Value::Null)
        }
    }
}

fn read_saved_history(
    config: &Config<'_>,
    saved: &Path,
    diagnostics: &mut Vec<String>,
) -> Option<RollingHistory> {
    let args = config.args;
    let parsed = std::fs::read(saved)
        .map_err(|error| error.to_string())
        .and_then(|bytes| {
            serde_json::from_slice::<RollingHistory>(&bytes).map_err(|error| error.to_string())
        })
        .and_then(|history| validate_history(&history).map(|()| history))
        .and_then(|history| {
            if history.repository != args.repo || history.base_ref != args.base_ref {
                return Err(format!(
                    "it names {} on {}, not {} on {}",
                    history.repository, history.base_ref, args.repo, args.base_ref
                ));
            }
            let unrelated = history.runs.iter().find(|run| {
                !Command::new("git")
                    .args([
                        "merge-base",
                        "--is-ancestor",
                        &run.head_sha,
                        &config.base_sha,
                    ])
                    .stderr(Stdio::null())
                    .status()
                    .is_ok_and(|status| status.success())
            });
            match unrelated {
                Some(run) => Err(format!(
                    "embedded commit {} is not an ancestor of {}",
                    run.head_sha, config.base_sha
                )),
                None => Ok(history),
            }
        });
    match parsed {
        Ok(history) => Some(history),
        Err(reason) => {
            diagnostics.push(format!(
                "baseline unavailable: saved history {} refused: {reason}",
                saved.display()
            ));
            None
        }
    }
}

fn read_host_history(
    config: &Config<'_>,
    wiring: &Wiring,
    workflow: u64,
    diagnostics: &mut Vec<String>,
) -> Result<(Option<RollingHistory>, Value), String> {
    let args = config.args;
    let plugin = tools::locate(tools::PLUGIN).map_err(|failure| failure.to_string())?;
    let path = state::state_directory(&args.out).join("prior-rolling.json");
    let mut command = Command::new(plugin);
    command
        .args([
            "history",
            "--repo",
            &args.repo,
            "--base-ref",
            &args.base_ref,
        ])
        .args(["--base-sha", &config.base_sha, "--workflow"])
        .arg(workflow.to_string())
        .args(["--artifact-name", &args.artifact_name, "--output"])
        .arg(&path);
    proxy::wire(&mut command, wiring, "history", false);
    let output = command
        .output()
        .map_err(|error| format!("cannot run the history plugin: {error}"))?;
    let answer: Value = serde_json::from_slice(&output.stdout).map_err(|error| {
        format!(
            "the history plugin answered unreadably ({error}): {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )
    })?;
    let cost = answer["cost"]["rest_requests"].clone();
    for (list, what) in [
        ("skipped", "history run refused"),
        ("lost", "history entry lost"),
    ] {
        for item in answer[list].as_array().map_or(&[][..], Vec::as_slice) {
            diagnostics.push(format!(
                "{what}: run {} at {}: {}",
                item["run_id"].as_str().unwrap_or_default(),
                item["head_sha"].as_str().unwrap_or_default(),
                item["reason"].as_str().unwrap_or_default()
            ));
        }
    }
    if answer["status"] != "available" {
        let reason = answer["reason"]
            .as_str()
            .unwrap_or("the history plugin named no reason");
        diagnostics.push(format!("baseline unavailable: {reason}"));
        return Ok((None, cost));
    }
    let bytes = std::fs::read(&path).map_err(|error| error.to_string())?;
    let history: RollingHistory =
        serde_json::from_slice(&bytes).map_err(|error| format!("unreadable history: {error}"))?;
    Ok((Some(history), cost))
}

fn look_up_workflow(args: &ReportArgs, wiring: &Wiring) -> Result<u64, String> {
    if args.run_id.parse::<u64>().is_err() {
        return Err("no --workflow or numeric --run-id names the workflow to read".into());
    }
    if std::env::var_os("GH_TOKEN").is_none() && std::env::var_os("GITHUB_TOKEN").is_none() {
        return Err("no GitHub credential: set GH_TOKEN or GITHUB_TOKEN".into());
    }
    let mut command = Command::new(&wiring.proxy);
    command.args([
        "api".into(),
        format!("repos/{}/actions/runs/{}", args.repo, args.run_id),
        "--jq".into(),
        ".workflow_id".into(),
    ]);
    proxy::wire(&mut command, wiring, "workflow", false);
    let output = command
        .output()
        .map_err(|error| format!("cannot look up this run's workflow: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "cannot look up this run's workflow: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let answer = String::from_utf8_lossy(&output.stdout);
    answer
        .trim()
        .parse()
        .map_err(|error| format!("this run's workflow id {answer} is unreadable: {error}"))
}

/// Own commands run in `finish`, so only then can a base run with nothing else measured decide.
fn decide_rolling(
    config: &Config<'_>,
    current: &BudgetRun,
    invocation_exit: Option<i32>,
    own: &[(String, PathBuf)],
    prior_read: bool,
) -> RollingDecision {
    let waits = config.args.mode == Mode::Base
        && invocation_exit.is_none()
        && !measured(current)
        && !own.is_empty();
    match (waits, prior_read) {
        (false, _) => RollingDecision::Settled,
        (true, false) => RollingDecision::PendingFresh,
        (true, true) => RollingDecision::PendingOnPrior,
    }
}

/// Whether `run` holds any figure: an errored result records none.
fn measured(run: &BudgetRun) -> bool {
    run.results
        .results
        .iter()
        .any(|result| result.actual.is_some())
}

/// The base job's next rolling file, if this run measured anything.
fn rolling_history(
    config: &Config<'_>,
    prior: Option<&RollingHistory>,
    current: &BudgetRun,
    invocation_exit: Option<i32>,
    diagnostics: &mut Vec<String>,
) -> Option<RollingHistory> {
    if config.args.mode != Mode::Base {
        return None;
    }
    if invocation_exit.is_some() || !measured(current) {
        diagnostics.push(NOTHING_MEASURED.into());
        return None;
    }
    let runs = prior.map(|history| history.runs.clone());
    extended_history(runs, current, diagnostics)
}

/// Settle the rolling file `report` left to `finish`, now that the own commands have run. A
/// prior history extends through the runs the comparison read, which omit this commit.
pub fn settle_pending_rolling(
    out: &Path,
    decision: RollingDecision,
    baseline: &[BudgetRun],
    current: &BudgetRun,
    own_check_exit: Option<i32>,
    diagnostics: &mut Vec<String>,
) -> Result<(), Failure> {
    let prior = match decision {
        RollingDecision::Settled => return Ok(()),
        RollingDecision::PendingFresh => None,
        RollingDecision::PendingOnPrior => Some(baseline.to_vec()),
    };
    let rolling = if own_check_exit.is_some() || !measured(current) {
        diagnostics.push(NOTHING_MEASURED.into());
        None
    } else {
        extended_history(prior, current, diagnostics)
    };
    write_rolling(out, rolling.as_ref())
}

/// `prior`'s entries, none when no history was read, with this run last: deduplicated by commit
/// and trimmed.
fn extended_history(
    prior: Option<Vec<BudgetRun>>,
    current: &BudgetRun,
    diagnostics: &mut Vec<String>,
) -> Option<RollingHistory> {
    let fresh = prior.is_none();
    let mut runs = prior.unwrap_or_default();
    runs.retain(|run| run.head_sha != current.head_sha);
    runs.push(current.clone());
    let keep_from = runs.len().saturating_sub(MAX_HISTORY_COMMITS);
    runs.drain(..keep_from);
    if fresh {
        diagnostics.push("base history starts fresh with this commit".into());
    }
    let history = RollingHistory {
        schema_version: 1,
        repository: current.repository.clone(),
        base_ref: current.base_ref.clone(),
        runs,
    };
    match validate_history(&history) {
        Ok(()) => Some(history),
        Err(reason) => {
            diagnostics.push(format!("no rolling file saved: {reason}"));
            None
        }
    }
}

fn write_rolling(out: &Path, rolling: Option<&RollingHistory>) -> Result<(), Failure> {
    let path = out.join(file::ROLLING);
    match rolling {
        Some(history) => state::write_json(&path, history),
        None => match std::fs::remove_file(&path) {
            Err(error) if error.kind() != std::io::ErrorKind::NotFound => Err(Failure::output(
                format!("cannot replace {}: {error}", path.display()),
            )),
            _ => Ok(()),
        },
    }
}

/// Hand the rendered report to the plugin, over the reporter protocol core defines.
pub fn deliver(
    wiring: &Wiring,
    to: &Destination,
    report: &ComparisonReport,
    markdown: String,
) -> DeliveryResult {
    let request = PublishRequest {
        schema_version: 1,
        destination: to.change_url.clone(),
        key: to.key.clone(),
        head_sha: report.current.head_sha.clone(),
        run_id: report.current.run_id.clone(),
        run_url: report.current.run_url.clone(),
        markdown,
        attempt: report.current.attempt,
        flags: report
            .rows
            .iter()
            .filter(|row| row.flag)
            .map(|row| HistoricalFlag {
                file: row.file.clone(),
                id: row.id.clone(),
                sigma_change: row.sigma_change,
                constant_baseline_change: row.sd == Some(0.0),
            })
            .collect(),
    };
    publish(wiring, &request).unwrap_or_else(|reason| DeliveryResult {
        delivered: false,
        url: None,
        diagnostic: Some(reason),
    })
}

fn publish(wiring: &Wiring, request: &PublishRequest) -> Result<DeliveryResult, String> {
    let plugin = tools::locate(tools::PLUGIN).map_err(|failure| failure.to_string())?;
    let body = serde_json::to_vec(request).map_err(|error| error.to_string())?;
    let mut command = Command::new(plugin);
    proxy::wire(&mut command, wiring, "delivery", false);
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("cannot run the reporter: {error}"))?;
    let mut stdin = child.stdin.take().ok_or("the reporter took no input")?;
    let written = stdin.write_all(&body);
    drop(stdin);
    let output = child
        .wait_with_output()
        .map_err(|error| format!("the reporter did not finish: {error}"))?;
    written.map_err(|error| format!("the reporter took no request: {error}"))?;
    serde_json::from_slice(&output.stdout).map_err(|error| {
        format!(
            "the reporter answered unreadably ({error}): {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )
    })
}

/// `reproduce`: the identical comparison from saved input, through core, nothing executed.
pub fn reproduce(args: &ReproduceArgs) -> Result<ExitCode, Failure> {
    let core = tools::locate(tools::CORE)?;
    let report = compare(&core, &args.input)?;
    let rendered = if args.json {
        serde_json::to_string_pretty(&report)
            .map_err(|error| Failure::output(format!("cannot encode the report: {error}")))?
            + "\n"
    } else {
        render_markdown(&report)
    };
    write!(std::io::stdout().lock(), "{rendered}")
        .map_err(|error| Failure::output(format!("output failed: {error}")))?;
    Ok(ExitCode::from(
        u8::try_from(report.exit_code()).unwrap_or(1),
    ))
}

/// Whether budget commands get no GitHub credential in this run.
pub fn deny_of(continuation: &Value) -> bool {
    continuation["deny_credentials"].as_bool().unwrap_or(false)
}
