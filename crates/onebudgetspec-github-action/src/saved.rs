//! Results this workflow run's test jobs saved with `check --save-results`, and how a catalog
//! budget takes one: its verdict stands, and its command does not run.
use std::path::{Path, PathBuf};
use std::process::Command;

use chrono::{DateTime, Utc};
use onebudgetspec_core::comparison::{BudgetRun, ResultNote, validate_run};
use onebudgetspec_core::{CheckReport, CheckResult, ListedBudget};

/// A budget nothing settled, when saved results were asked for. The job cannot tell a budget this
/// change did not reach from a result that was lost, so this names no cause.
pub const NO_SAVED_RESULT: &str = "no saved result was found on this run";

/// Every result in the `*.json` files under `directory`, however many artifacts they came from.
/// A directory that is not there is no artifact having arrived; a file that is no `CheckReport`
/// is a diagnostic.
pub fn read(directory: &Path, diagnostics: &mut Vec<String>) -> Vec<CheckResult> {
    let mut results = Vec::new();
    for path in json_files(directory) {
        let parsed = std::fs::read(&path)
            .map_err(|error| error.to_string())
            .and_then(|bytes| {
                serde_json::from_slice::<CheckReport>(&bytes).map_err(|error| error.to_string())
            });
        match parsed {
            Ok(report) => results.extend(report.results),
            Err(reason) => diagnostics.push(format!(
                "saved results file {} ignored: {reason}",
                path.display()
            )),
        }
    }
    results
}

fn json_files(directory: &Path) -> Vec<PathBuf> {
    let mut pending = vec![directory.to_path_buf()];
    let mut files = Vec::new();
    while let Some(next) = pending.pop() {
        let Ok(entries) = std::fs::read_dir(&next) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            match entry.file_type() {
                Ok(kind) if kind.is_dir() => pending.push(path),
                Ok(kind) if kind.is_file() && path.extension().is_some_and(|e| e == "json") => {
                    files.push(path);
                }
                _ => {}
            }
        }
    }
    files.sort();
    files
}

/// The latest of `results` for `budget`, matched by id alone: the latest `ended_at` wins whichever
/// file a result names, and `adopted` then gives it the catalog's.
pub fn latest_for<'a>(
    results: &'a [CheckResult],
    budget: &ListedBudget,
) -> Option<&'a CheckResult> {
    results
        .iter()
        .filter(|result| result.id == budget.id)
        .max_by_key(|result| result.ended_at)
}

/// `result` as `budget`'s own, or the reason core would refuse a run holding it, which would cost
/// every other row its figure.
pub fn adopted(result: &CheckResult, budget: &ListedBudget) -> Result<CheckResult, String> {
    let mut adopted = result.clone();
    adopted.file.clone_from(&budget.file);
    let run = BudgetRun {
        schema_version: 1,
        repository: "checked".into(),
        head_sha: "checked".into(),
        base_ref: "checked".into(),
        run_id: "checked".into(),
        run_url: "checked".into(),
        attempt: 1,
        completed_at: result.ended_at.to_rfc3339(),
        catalog: vec![budget.clone()],
        results: CheckReport {
            schema_version: 1,
            results: vec![adopted.clone()],
        },
        unavailable: Vec::new(),
        enforcement_exclude_labels: Vec::new(),
        result_notes: Vec::new(),
    };
    validate_run(&run)?;
    Ok(adopted)
}

/// When `sha` was committed, from local git. Its author date and the merge commit a pull request
/// job checks out both say nothing about when that head appeared.
pub fn committer_date(sha: &str) -> Result<DateTime<Utc>, String> {
    let output = Command::new("git")
        .args(["log", "-1", "--format=%cI", sha, "--"])
        .output()
        .map_err(|error| format!("cannot run git: {error}"))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_owned());
    }
    let text = String::from_utf8_lossy(&output.stdout);
    DateTime::parse_from_rfc3339(text.trim())
        .map(|date| date.with_timezone(&Utc))
        .map_err(|error| format!("unreadable committer date {}: {error}", text.trim()))
}

/// The note on a result measured before its pull request's head existed: a build cache replayed
/// it. `ended_at` is the signal, because a restored file's modification time is the restore time.
pub fn replay_note(result: &CheckResult, head_committed: DateTime<Utc>) -> Option<ResultNote> {
    (result.ended_at < head_committed).then(|| ResultNote {
        file: result.file.clone(),
        id: result.id.clone(),
        note: format!(
            "replayed from cache, measured {}",
            result.ended_at.format("%Y-%m-%d")
        ),
    })
}
