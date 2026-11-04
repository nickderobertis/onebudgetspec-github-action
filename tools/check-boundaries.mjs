// Project boundaries over the Nx graph, by tag: nothing depends on a `type:e2e` project,
// so a test suite stays a leaf no other project's change can be made to run through.
// Quiet on success; on failure names each forbidden edge, or the graph problem, and what
// to do.
import { execFileSync } from "node:child_process";
import { readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// [source tag or null for any source, forbidden target tag]
const FORBIDDEN = [[null, "type:e2e"]];

function fail(problem, action) {
  console.error(`check-boundaries: ${problem}`);
  console.error(`check-boundaries: ${action}`);
  process.exit(1);
}

function readGraph() {
  const file = join(tmpdir(), `nx-graph-${process.pid}.json`);
  let graph;
  let problem;
  try {
    execFileSync("node_modules/.bin/nx", ["graph", `--file=${file}`], {
      stdio: ["ignore", "ignore", "pipe"],
      env: { ...process.env, NX_DAEMON: "false" },
    });
    graph = JSON.parse(readFileSync(file, "utf8")).graph;
  } catch (error) {
    problem = error.stderr?.toString().trim() || error.message;
  }
  try {
    rmSync(file, { force: true });
  } catch (error) {
    console.error(`check-boundaries: could not remove ${file} (${error.message}); delete it by hand.`);
    process.exitCode = 1;
  }
  if (problem !== undefined) {
    fail(
      `could not read the Nx project graph: ${problem}`,
      "run 'just bootstrap' so Nx is installed, fix any project.json Nx rejects, then re-run.",
    );
  }
  return graph;
}

const graph = readGraph();
const nodes = graph?.nodes;
const dependencies = graph?.dependencies;
const isMap = (value) => typeof value === "object" && value !== null && !Array.isArray(value);
if (!isMap(nodes) || !isMap(dependencies) || Object.keys(nodes).length === 0) {
  fail("the Nx graph has no nodes or dependencies map", "upgrade or reinstall Nx ('just bootstrap'), then re-run.");
}

const violations = [];
for (const [source, edges] of Object.entries(dependencies)) {
  if (!Array.isArray(edges)) {
    fail(`the Nx graph lists ${source}'s dependencies as ${typeof edges}, not a list`, "reinstall Nx ('just bootstrap'), then re-run.");
  }
  const sourceTags = nodes[source]?.data?.tags;
  if (!Array.isArray(sourceTags) || !sourceTags.every((tag) => typeof tag === "string")) {
    fail(`${source} has no tags list of strings in the graph`, `give ${source}'s project.json a "tags" array of strings, then re-run.`);
  }
  for (const edge of edges) {
    if (typeof edge?.target !== "string" || !Object.hasOwn(nodes, edge.target)) {
      fail(
        `${source} lists a dependency edge ${JSON.stringify(edge)} that names no project in the graph`,
        "reinstall Nx ('just bootstrap'), then re-run; if it persists, check each project.json's implicitDependencies.",
      );
    }
    const tags = nodes[edge.target]?.data?.tags;
    if (!Array.isArray(tags) || !tags.every((tag) => typeof tag === "string")) {
      fail(
        `${source} depends on ${edge?.target}, which has no tags list of strings in the graph`,
        `give ${edge?.target}'s project.json a "tags" array of strings, then re-run.`,
      );
    }
    for (const [from, to] of FORBIDDEN) {
      if ((from === null || sourceTags.includes(from)) && tags.includes(to)) {
        violations.push(`${source} -> ${edge.target} (${from ?? "any"} may not depend on ${to})`);
      }
    }
  }
}
if (violations.length > 0) {
  fail(
    `forbidden project edges:\n  ${violations.join("\n  ")}`,
    "remove the edge; a test or tooling project depends on what it checks, never the reverse.",
  );
}
