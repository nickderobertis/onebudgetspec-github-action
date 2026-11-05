//! The command line. `action.yml` passes each action input as the flag of the same name.
use std::path::PathBuf;

use clap::{Args, Parser, Subcommand, ValueEnum};

/// Run onebudgetspec budget reports from GitHub Actions or the command line.
///
/// Exit codes: 0 success; 1 an enforced budget is over, or output failed; 2 invalid
/// arguments or an invalid budgets file; 3 an enforced measurement errored. Baseline,
/// comment, credential and artifact failures are diagnostics and never change the exit.
#[derive(Debug, Parser)]
#[command(name = "onebudgetspec-github-action", version)]
pub struct Cli {
    /// What to do; with none, this help is shown.
    #[command(subcommand)]
    pub command: Option<Command>,
}

/// The tool's verbs.
#[derive(Debug, Subcommand)]
pub enum Command {
    /// Check every runnable budget once, compare with base history, then render and deliver.
    Report(Box<ReportArgs>),
    /// Complete a `report --phased` run: stop the timer and save the own-command verdicts.
    Finish(FinishArgs),
    /// Reproduce a job's comparison from its saved `comparison-input.json`, offline.
    Reproduce(ReproduceArgs),
    /// Report one saved telemetry metric as a budget's measurement.
    Telemetry(TelemetryArgs),
}

/// What kind of run this is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum Mode {
    /// A change request: compare with base history and deliver the standing comment.
    PullRequest,
    /// A base-branch push: the same result, carried into the rolling history, never posted.
    Base,
    /// A fork or other unprivileged run: summary and artifacts only, no GitHub credential
    /// reaches a budget command.
    ReadOnly,
    /// A privileged event that could hold fork code: no budget command runs at all.
    Privileged,
}

/// `report`'s arguments: the action's inputs, then what the workflow context supplies.
#[derive(Debug, Args)]
pub struct ReportArgs {
    /// Budgets files, as globs separated by commas or newlines; fixture and conformance
    /// trees are never production budgets.
    #[arg(long, default_value = "**/budgets.yaml")]
    pub budget_files: String,
    /// Labels declaring a budget measurable in this job, separated by commas or newlines.
    /// Budgets without them still run when runnable; these are also stamped on the telemetry.
    #[arg(long, default_value = "ci-report")]
    pub labels: String,
    /// Name of the artifact holding the results and the rolling history.
    #[arg(long, default_value = "budget-results")]
    pub artifact_name: String,
    /// Labels whose budgets are report-only: run and shown, never failing this command.
    #[arg(long, default_value = "")]
    pub enforcement_exclude_labels: String,
    /// Repository as OWNER/NAME.
    #[arg(long)]
    pub repo: String,
    /// Commit this run measures; defaults to the checkout's HEAD.
    #[arg(long)]
    pub head_sha: Option<String>,
    /// Base branch the history belongs to. Without one, no baseline is read.
    #[arg(long, default_value = "")]
    pub base_ref: String,
    /// Base head every saved entry must reach; defaults to the measured commit.
    #[arg(long)]
    pub base_sha: Option<String>,
    /// Run identity saved with the results.
    #[arg(long, default_value = "local")]
    pub run_id: String,
    /// Where the complete results of this run can be read.
    #[arg(long, default_value = "local")]
    pub run_url: String,
    /// Positive attempt number of this run.
    #[arg(long, default_value_t = 1)]
    pub attempt: u32,
    /// What kind of run this is.
    #[arg(long, value_enum, default_value_t = Mode::ReadOnly)]
    pub mode: Mode,
    /// Why this run is read-only, shown in its report.
    #[arg(long, default_value = "")]
    pub read_only_reason: String,
    /// Directory receiving every result file.
    #[arg(long)]
    pub out: PathBuf,
    /// The change request to deliver to, in pull-request mode.
    #[arg(long, default_value = "")]
    pub change_url: String,
    /// Standing comment key at that change request.
    #[arg(long, default_value = "budget-report")]
    pub key: String,
    /// Numeric workflow id whose base runs saved the history. When absent, a numeric
    /// `--run-id` is looked up once; a local run passes `--saved-history` instead.
    #[arg(long)]
    pub workflow: Option<u64>,
    /// A saved `rolling-history.json` to use instead of reading one from the host.
    #[arg(long)]
    pub saved_history: Option<PathBuf>,
    /// When the job started, as Unix seconds; defaults to now.
    #[arg(long)]
    pub started_at: Option<f64>,
    /// Seconds the job spent installing, recorded in the telemetry.
    #[arg(long, default_value_t = 0.0)]
    pub install_seconds: f64,
    /// Stop after the main results; the caller uploads them and then runs `finish`.
    #[arg(long)]
    pub phased: bool,
}

/// `finish`'s arguments.
#[derive(Debug, Args)]
pub struct FinishArgs {
    /// The directory `report --phased` wrote.
    #[arg(long)]
    pub out: PathBuf,
    /// How the main result upload ended, as the workflow reports it.
    #[arg(long, default_value = "success")]
    pub main_upload: String,
}

/// `reproduce`'s arguments.
#[derive(Debug, Args)]
pub struct ReproduceArgs {
    /// A saved `comparison-input.json`.
    #[arg(long)]
    pub input: PathBuf,
    /// Print the report as JSON rather than Markdown.
    #[arg(long)]
    pub json: bool,
}

/// `telemetry`'s arguments.
#[derive(Debug, Args)]
pub struct TelemetryArgs {
    /// The metric to report, a key of the file's `metrics`.
    #[arg(long)]
    pub metric: String,
    /// The saved telemetry file, relative to the budgets file's directory under a check.
    #[arg(long)]
    pub input: PathBuf,
    /// The label the file must carry, proving it is this job's own telemetry.
    #[arg(long, default_value = "ci-report")]
    pub label: String,
}
