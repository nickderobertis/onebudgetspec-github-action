//! The production catalog, and whether each entry's existing command can run here.
//!
//! Nothing here knows a budget's kind. A command is judged only by what its own argv names:
//! a program this runner lacks or a saved input that is absent is a reason it cannot run,
//! and a missing selection label never is.
use std::path::{Component, Path, PathBuf};
use std::process::Command;

use globset::{GlobBuilder, GlobSetBuilder};
use onebudgetspec_core::{ListReport, ListedBudget};

use crate::state::Failure;
use crate::tools;

/// A tree under one of these directory names holds test data, not production budgets.
const EXCLUDED_TREES: [&str; 5] = [
    "fixture",
    "fixtures",
    "__fixtures__",
    "testdata",
    "conformance",
];
/// Arguments naming what a command writes, so their absence says nothing.
const OUTPUT_FLAGS: [&str; 4] = ["--output", "--out", "-o", "--write"];

/// How a catalog entry takes part in this run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Disposition {
    /// Its existing command runs through ordinary check.
    Runnable,
    /// It reads this tool's own telemetry, so it runs once that telemetry exists: `input`,
    /// relative to its budgets file's directory.
    Own { input: PathBuf },
    /// It cannot run here, for this reason.
    Unavailable(String),
}

/// Split comma- or newline-separated action input into its values.
pub fn split_list(input: &str) -> Vec<String> {
    input
        .split([',', '\n'])
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .collect()
}

/// The production budgets files `patterns` match, relative to the checkout.
pub fn discover(patterns: &[String]) -> Result<Vec<String>, Failure> {
    let mut set = GlobSetBuilder::new();
    for pattern in patterns {
        let glob = GlobBuilder::new(pattern)
            .literal_separator(true)
            .build()
            .map_err(|error| {
                Failure::invalid(format!(
                    "--budget-files {pattern}: {error}; pass a valid glob"
                ))
            })?;
        set.add(glob);
    }
    let set = set
        .build()
        .map_err(|error| Failure::invalid(format!("--budget-files: {error}")))?;
    let listed = Command::new("git")
        .args([
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ])
        .output()
        .map_err(|error| Failure::invalid(format!("cannot run git: {error}; install git")))?;
    if !listed.status.success() {
        return Err(Failure::invalid(format!(
            "git cannot list this directory: {}; run inside a checkout",
            String::from_utf8_lossy(&listed.stderr).trim()
        )));
    }
    let mut files: Vec<String> = String::from_utf8_lossy(&listed.stdout)
        .split('\0')
        .filter(|file| !file.is_empty() && set.is_match(file) && Path::new(file).is_file())
        .filter(|file| !file.split('/').any(|part| EXCLUDED_TREES.contains(&part)))
        .map(str::to_owned)
        .collect();
    files.sort();
    files.dedup();
    Ok(files)
}

/// The complete catalog of `files`, through ordinary `list`.
pub fn list(core: &Path, files: &[String]) -> Result<Vec<ListedBudget>, Failure> {
    if files.is_empty() {
        return Ok(Vec::new());
    }
    let output = tools::capture(
        Command::new(core).args(["list", "--json"]).args(files),
        "onebudgetspec list",
    )?;
    if !output.status.success() {
        return Err(Failure::invalid(format!(
            "onebudgetspec list refused the budgets files: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )));
    }
    let report: ListReport = serde_json::from_slice(&output.stdout).map_err(|error| {
        Failure::invalid(format!("onebudgetspec list answered unreadably: {error}"))
    })?;
    Ok(report.budgets)
}

/// Decide `budget`'s part in this run from its argv and what this runner holds.
pub fn dispose(budget: &ListedBudget, search_path: &std::ffi::OsStr) -> Disposition {
    let Some(directory) = Path::new(&budget.file).parent() else {
        return Disposition::Unavailable("its budgets file has no directory".into());
    };
    if let Some(input) = own_input(&budget.command) {
        return match checkout_relative(directory, &input) {
            Some(_) => Disposition::Own { input },
            None => Disposition::Unavailable(format!(
                "its telemetry input {} leaves the checkout",
                input.display()
            )),
        };
    }
    let program = &budget.command[0];
    let found = if is_bare(program) {
        tools::on_path(program, search_path).is_some()
    } else {
        directory.join(program).exists()
    };
    if !found {
        return Disposition::Unavailable(format!(
            "its command needs {program}, which this runner does not have"
        ));
    }
    let mut writes_next = false;
    for argument in &budget.command[1..] {
        let writes = std::mem::replace(&mut writes_next, OUTPUT_FLAGS.contains(&argument.as_str()));
        if !writes && names_a_path(argument) && !directory.join(argument).exists() {
            return Disposition::Unavailable(format!(
                "its command reads {argument}, which is not available in this job"
            ));
        }
    }
    Disposition::Runnable
}

fn is_bare(program: &str) -> bool {
    !program.contains(['/', '\\'])
}

/// A relative path written as one: code, flags, URLs and plain words are not.
fn names_a_path(argument: &str) -> bool {
    argument.contains('/')
        && !argument.starts_with('-')
        && !argument.contains(char::is_whitespace)
        && !argument.contains("://")
        && !argument.contains(['$', '=', '\'', '"', '(', ')', '{', '}', '%'])
        && Path::new(argument).is_relative()
}

/// The `--input` of a command that is this tool's own `telemetry` reader.
fn own_input(command: &[String]) -> Option<PathBuf> {
    let [program, verb, rest @ ..] = command else {
        return None;
    };
    if program != tools::DRIVER || verb != "telemetry" {
        return None;
    }
    let position = rest.iter().position(|argument| argument == "--input")?;
    rest.get(position + 1).map(PathBuf::from)
}

/// The `--metric` an own `telemetry` command reads.
pub fn own_metric(command: &[String]) -> Option<&str> {
    let position = command.iter().position(|argument| argument == "--metric")?;
    command.get(position + 1).map(String::as_str)
}

/// `input` under `directory`, unless it would leave the checkout.
pub fn checkout_relative(directory: &Path, input: &Path) -> Option<PathBuf> {
    let joined = directory.join(input);
    let inside = joined
        .components()
        .all(|part| matches!(part, Component::Normal(_) | Component::CurDir));
    inside.then_some(joined)
}
