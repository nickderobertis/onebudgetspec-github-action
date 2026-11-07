//! What `report` hands to `finish`, the failure type, and the small file helpers both use.
use std::fmt;
use std::path::{Path, PathBuf};

use serde_json::{Value, json};

/// A failure with the exit code it answers with.
#[derive(Debug)]
pub struct Failure {
    message: String,
    code: u8,
}

impl Failure {
    /// Invalid arguments or budgets files: exit 2, as ordinary check does.
    pub fn invalid(message: String) -> Self {
        Self { message, code: 2 }
    }

    /// A file or stream that could not be used: exit 1.
    pub fn output(message: String) -> Self {
        Self { message, code: 1 }
    }

    /// The exit code.
    pub fn code(&self) -> u8 {
        self.code
    }
}

impl fmt::Display for Failure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

/// Every file name one run saves, directly under its output directory.
pub mod file {
    /// The current `BudgetRun`.
    pub const BUDGET_RUN: &str = "budget-run.json";
    /// The `ComparisonInput` that reproduces this report.
    pub const INPUT: &str = "comparison-input.json";
    /// The `ComparisonReport`.
    pub const REPORT: &str = "comparison-report.json";
    /// The rendered report.
    pub const MARKDOWN: &str = "report.md";
    /// The base job's `RollingHistory`; the plugin reads exactly this name.
    pub const ROLLING: &str = "rolling-history.json";
    /// The job's own telemetry.
    pub const TELEMETRY: &str = "report-job.json";
    /// The hidden state directory, which an artifact upload leaves out.
    pub const STATE: &str = ".state";
    /// What `finish` needs from `report`.
    pub const CONTINUATION: &str = "continuation.json";
    /// Every `gh api` call this run made.
    pub const REST_LOG: &str = "rest.jsonl";
}

/// The state directory of `out`.
pub fn state_directory(out: &Path) -> PathBuf {
    out.join(file::STATE)
}

/// Read a JSON file this run or an earlier one saved.
pub fn read_json(path: &Path) -> Result<Value, Failure> {
    let bytes = std::fs::read(path).map_err(|error| {
        Failure::output(format!(
            "cannot read {}: {error}; run the step that writes it first",
            path.display()
        ))
    })?;
    serde_json::from_slice(&bytes)
        .map_err(|error| Failure::output(format!("{} is not JSON: {error}", path.display())))
}

/// Save `value` as JSON.
pub fn write_json(path: &Path, value: &impl serde::Serialize) -> Result<(), Failure> {
    let bytes = serde_json::to_vec_pretty(value)
        .map_err(|error| Failure::output(format!("cannot encode {}: {error}", path.display())))?;
    write_bytes(path, &bytes)
}

/// Save `bytes`.
pub fn write_bytes(path: &Path, bytes: &[u8]) -> Result<(), Failure> {
    std::fs::write(path, bytes).map_err(|error| {
        Failure::output(format!(
            "cannot write {}: {error}; choose a writable directory",
            path.display()
        ))
    })
}

/// Where and under which key `finish` delivers a pull request's comment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Destination {
    /// The change request.
    pub change_url: String,
    /// The standing comment's key.
    pub key: String,
}

/// What `report` left `finish` to do about the base job's rolling file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RollingDecision {
    /// `report` already saved it or said why not; `finish` only carries a saved one forward.
    Settled,
    /// It waits on the own commands, and no prior history was read.
    PendingFresh,
    /// It waits on the own commands, and extends the prior history.
    PendingOnPrior,
}

impl RollingDecision {
    /// The continuation's spelling.
    pub fn name(self) -> &'static str {
        match self {
            Self::Settled => "settled",
            Self::PendingFresh => "pending-fresh",
            Self::PendingOnPrior => "pending-on-prior",
        }
    }

    /// Back from the saved continuation, refusing anything but a spelling `name` gives.
    pub fn from_continuation(continuation: &Value) -> Result<Self, Failure> {
        let saved = &continuation["rolling"];
        [Self::Settled, Self::PendingFresh, Self::PendingOnPrior]
            .into_iter()
            .find(|decision| saved == decision.name())
            .ok_or_else(|| {
                Failure::output(format!(
                    "the saved continuation's rolling decision {saved} is unreadable; re-run the report step"
                ))
            })
    }
}

/// The per-phase seconds and uploads the telemetry reports.
#[derive(Debug, Clone, Default)]
pub struct Phases {
    /// Installing the tools, as the action measured it.
    pub install: f64,
    /// Running the budget commands.
    pub check: f64,
    /// Reading the rolling history.
    pub archive: f64,
    /// Comparing and rendering.
    pub render: f64,
    /// Delivering the comment.
    pub delivery: f64,
}

impl Phases {
    /// The JSON the continuation saves.
    pub fn to_json(&self) -> Value {
        json!({
            "install": self.install, "check": self.check, "archive": self.archive,
            "render": self.render, "delivery": self.delivery,
        })
    }

    /// Back from the saved JSON.
    pub fn from_json(value: &Value) -> Self {
        let seconds = |name: &str| value[name].as_f64().unwrap_or_default();
        Self {
            install: seconds("install"),
            check: seconds("check"),
            archive: seconds("archive"),
            render: seconds("render"),
            delivery: seconds("delivery"),
        }
    }
}
