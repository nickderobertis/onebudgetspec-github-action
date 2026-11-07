//! A base push's reads of the merged pull request's last CI run. Every read goes through `gh`, so
//! the job's proxy counts them: 4 requests with the archive, 3 when the run has no such artifact,
//! 1 when the commit has no pull request.
use std::io::Read;
use std::process::Command;

use onebudgetspec_core::{CheckReport, CheckResult};
use serde_json::Value;

use crate::proxy::{self, Wiring};
use crate::state::file;

/// The most a saved `budget-run.json` may hold; a result's `detail` alone can reach 152.6 KB.
const MAX_BUDGET_RUN_BYTES: u64 = 256 * 1024 * 1024;

/// What the merged pull request's run recorded, or why this commit has none to read.
pub struct MergedRun {
    /// Its ordinary results, without `detail`: one policy result's reaches 152.6 KB, and 20
    /// history entries of them would approach 6 MB. Its own artifact keeps it.
    pub results: Vec<CheckResult>,
    /// Where they came from, for the report's diagnostics.
    pub source: String,
}

/// Read `sha`'s merged pull request's latest `pull_request` run of `workflow`, and that run's
/// newest `artifact` artifact. An `Err` is the reason there is nothing to take.
pub fn read(
    wiring: &Wiring,
    repository: &str,
    sha: &str,
    workflow: u64,
    artifact: &str,
) -> Result<MergedRun, String> {
    let pulls = read_json(wiring, &format!("repos/{repository}/commits/{sha}/pulls"))?;
    let (number, head) = merged_pull_request(&pulls, sha)
        .ok_or_else(|| format!("commit {sha} has no merged pull request"))?;
    let runs = read_json(
        wiring,
        &format!(
            "repos/{repository}/actions/workflows/{workflow}/runs?event=pull_request&head_sha={head}&per_page=1"
        ),
    )?;
    let run = runs["workflow_runs"][0]["id"].as_u64().ok_or_else(|| {
        format!("pull request #{number}'s head {head} has no pull_request run of this workflow")
    })?;
    let listed = read_json(
        wiring,
        &format!("repos/{repository}/actions/runs/{run}/artifacts?name={artifact}"),
    )?;
    // A re-run leaves two artifacts of one name, and only the newest is the run's last word.
    let newest = listed["artifacts"]
        .as_array()
        .and_then(|artifacts| artifacts.iter().max_by_key(|item| item["id"].as_u64()))
        .ok_or_else(|| format!("run {run} holds no {artifact} artifact"))?;
    if newest["expired"] == true {
        return Err(format!("run {run}'s {artifact} artifact has expired"));
    }
    let id = newest["id"]
        .as_u64()
        .ok_or_else(|| format!("run {run}'s {artifact} artifact names no id"))?;
    let archive = read_gh(
        wiring,
        &format!("repos/{repository}/actions/artifacts/{id}/zip"),
    )?;
    let results = saved_results(&archive)
        .map_err(|reason| format!("run {run}'s {artifact} artifact: {reason}"))?;
    Ok(MergedRun {
        results,
        source: format!("merged pull request #{number}'s run {run}"),
    })
}

/// The pull request `sha` is the merge commit of: `commits/SHA/pulls` also lists open ones that
/// merely contain it.
fn merged_pull_request(pulls: &Value, sha: &str) -> Option<(u64, String)> {
    pulls.as_array()?.iter().find_map(|pull| {
        let merged = pull["merged_at"].is_string() && pull["merge_commit_sha"] == sha;
        let head = pull["head"]["sha"]
            .as_str()
            .filter(|head| head.chars().all(|c| c.is_ascii_hexdigit()))?;
        merged.then(|| (pull["number"].as_u64().unwrap_or_default(), head.to_owned()))
    })
}

/// The results of the archive's `budget-run.json`, reading nothing else of it.
fn saved_results(archive: &[u8]) -> Result<Vec<CheckResult>, String> {
    let mut archive = zip::ZipArchive::new(std::io::Cursor::new(archive))
        .map_err(|error| format!("not a readable archive: {error}"))?;
    let member = archive
        .by_name(file::BUDGET_RUN)
        .map_err(|error| format!("no {}: {error}", file::BUDGET_RUN))?;
    let mut bytes = Vec::new();
    member
        .take(MAX_BUDGET_RUN_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("cannot read {}: {error}", file::BUDGET_RUN))?;
    if bytes.len() as u64 > MAX_BUDGET_RUN_BYTES {
        return Err(format!(
            "{} is over the {MAX_BUDGET_RUN_BYTES} byte limit",
            file::BUDGET_RUN
        ));
    }
    let mut run: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("{} is not JSON: {error}", file::BUDGET_RUN))?;
    let report: CheckReport = serde_json::from_value(run["results"].take())
        .map_err(|error| format!("{} holds no readable results: {error}", file::BUDGET_RUN))?;
    let mut results = report.results;
    for result in &mut results {
        result.detail = None;
    }
    Ok(results)
}

fn read_json(wiring: &Wiring, endpoint: &str) -> Result<Value, String> {
    let bytes = read_gh(wiring, endpoint)?;
    serde_json::from_slice(&bytes).map_err(|error| format!("{endpoint} is not JSON: {error}"))
}

/// The body of one read. `--include` shows the proxy a refusal's `Retry-After`, which it honours.
fn read_gh(wiring: &Wiring, endpoint: &str) -> Result<Vec<u8>, String> {
    let mut command = Command::new(&wiring.proxy);
    command.args(["api", "--include", endpoint]);
    proxy::wire(&mut command, wiring, "history", false);
    let output = command
        .output()
        .map_err(|error| format!("cannot run gh: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "{endpoint}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let separator = b"\r\n\r\n";
    let body = output
        .stdout
        .windows(separator.len())
        .position(|window| window == separator)
        .map_or(0, |head| head + separator.len());
    Ok(output.stdout[body..].to_vec())
}
