//! `report`: ordinary checks, then one saved result, compared and delivered through core and
//! the plugin. No scalar is computed here and no measurement is run to fill a row.
use std::collections::BTreeSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use onebudgetspec_core::comparison::{
    BudgetRun, ComparisonInput, ComparisonReport, RollingHistory, UnavailableBudget,
    validate_history,
};
use onebudgetspec_core::delivery::{DeliveryResult, HistoricalFlag, PublishRequest};
use onebudgetspec_core::{CheckReport, ListedBudget, exit, render_markdown};
use serde_json::{Value, json};

use crate::catalog::{self, Disposition};
use crate::cli::{Mode, ReportArgs, ReproduceArgs};
use crate::finish;
use crate::proxy::{self, Wiring};
use crate::state::{self, Failure, Phases, file};
use crate::tools;

/// The base history holds at most this many commits.
const MAX_HISTORY_COMMITS: usize = 20;

/// The current time as Unix seconds.
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |elapsed| elapsed.as_secs_f64())
}

/// The check run's ordinary answers, merged.
pub struct Checked {
    /// Every result, unchanged, minus measurements GitHub's refusal left incomplete.
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
        .partition(|budget| budget.labels.iter().any(|label| excluded.contains(label)));
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
        let blocked = (result.verdict == onebudgetspec_core::Verdict::Error)
            .then(|| proxy::blocked_reason(&log, &result.id))
            .flatten();
        if let Some(reason) = &blocked {
            unavailable.push(UnavailableBudget {
                file: result.file.clone(),
                id: result.id.clone(),
                reason: reason.clone(),
            });
        }
        blocked.is_none()
    });
    checked
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
            output.status.code().unwrap_or(exit::ERROR),
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
    serde_json::from_slice(&output.stdout).map_err(|error| {
        Failure::invalid(format!(
            "onebudgetspec report refused {}: {}{error}",
            input.display(),
            String::from_utf8_lossy(&output.stderr)
        ))
    })
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
        let excluded = catalog::split_list(&args.enforcement_exclude_labels);
        let job_labels = catalog::split_list(&args.labels);
        let pattern = |label: &String| {
            label.starts_with(|first: char| first.is_ascii_lowercase())
                && label
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
        };
        if let Some(bad) = excluded
            .iter()
            .chain(&job_labels)
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
    let (runnable, own, unavailable, mut diagnostics) = sort_catalog(&config, &budgets, &wiring);
    let phase_start = now();
    let checked = check_partitioned(
        &core_cli,
        &wiring,
        &budgets,
        &runnable,
        &config.excluded,
        denies_credentials(args.mode),
    );
    phases.check = now() - phase_start;
    let invocation_exit = checked.invocation_exit;
    let current = current_run(&config, budgets.clone(), checked, unavailable);

    let phase_start = now();
    let (prior, history_cost) = read_history(&config, &wiring, &mut diagnostics);
    phases.archive = now() - phase_start;
    diagnostics.extend(mode_diagnostics(args, &main_artifact(args, &own)));
    let rolling = rolling_history(
        &config,
        prior.as_ref(),
        &current,
        invocation_exit,
        &mut diagnostics,
    );
    write_rolling(out, rolling.as_ref())?;
    state::write_json(&out.join(file::BUDGET_RUN), &current)?;
    let saved_diagnostics = compare_and_deliver(
        &core_cli,
        &wiring,
        args,
        current,
        prior,
        diagnostics,
        &mut phases,
    )?;

    let continuation = json!({
        "started_at": started_at,
        "phases": phases.to_json(),
        "own": own.iter().map(|(id, input)| json!({"id": id, "input": input})).collect::<Vec<_>>(),
        "job_labels": config.job_labels,
        "diagnostics": saved_diagnostics,
        "invocation_exit": invocation_exit,
        "phased": args.phased,
        "deny_credentials": denies_credentials(args.mode),
        "history_cost": history_cost,
        "run_own": args.mode != Mode::Privileged,
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
    }
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

/// Save the input, compare and render through core, deliver through the plugin, and
/// answer with the diagnostics the saved report carries.
fn compare_and_deliver(
    core_cli: &Path,
    wiring: &Wiring,
    args: &ReportArgs,
    current: BudgetRun,
    prior: Option<RollingHistory>,
    diagnostics: Vec<String>,
    phases: &mut Phases,
) -> Result<Vec<String>, Failure> {
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
    let plain = report.diagnostics.clone();
    report.diagnostics.extend_from_slice(&diagnostics);
    let body = render_markdown(&report);
    phases.render = now() - phase_start;

    let mut saved = diagnostics;
    let phase_start = now();
    if args.mode == Mode::PullRequest {
        let delivered = deliver(wiring, args, &report, body);
        saved.extend(delivered.diagnostic.clone());
        if !delivered.delivered {
            saved.push(
                "report not delivered; this summary and the saved artifact remain available".into(),
            );
        }
    }
    phases.delivery = now() - phase_start;
    report.diagnostics = plain;
    save_report(&args.out, &mut report, &saved)?;
    Ok(saved)
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
                    reason: "measured once this report's results are saved: its completed verdict is in the saved artifact".into(),
                });
            }
            Disposition::Unavailable(reason) => {
                if budget
                    .labels
                    .iter()
                    .any(|label| config.job_labels.contains(label))
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

/// The prior rolling history, or nothing with its reasons in `diagnostics`, and its
/// `QueryCost` when the plugin read it.
fn read_history(
    config: &Config<'_>,
    wiring: &Wiring,
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
    match read_host_history(config, wiring, diagnostics) {
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
    diagnostics: &mut Vec<String>,
) -> Result<(Option<RollingHistory>, Value), String> {
    let args = config.args;
    let workflow = match args.workflow {
        Some(workflow) => workflow,
        None => look_up_workflow(args, wiring)?,
    };
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

/// The base job's next rolling file: the prior entries carried forward with this run last.
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
    if invocation_exit.is_some() || current.results.results.is_empty() {
        diagnostics.push(
            "no rolling file saved: this run completed no measurement, so the next base run reads the one before it".into(),
        );
        return None;
    }
    let fresh = prior.is_none();
    let mut runs: Vec<BudgetRun> = prior
        .map(|history| history.runs.clone())
        .unwrap_or_default();
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
fn deliver(
    wiring: &Wiring,
    args: &ReportArgs,
    report: &ComparisonReport,
    markdown: String,
) -> DeliveryResult {
    let request = PublishRequest {
        schema_version: 1,
        destination: args.change_url.clone(),
        key: args.key.clone(),
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
