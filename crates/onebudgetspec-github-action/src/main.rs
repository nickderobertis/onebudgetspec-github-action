//! Placeholder command line; performs no operational work.
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    match args.as_slice() {
        [] => {
            help();
            ExitCode::SUCCESS
        }
        [arg] if arg == "--help" || arg == "-h" => {
            help();
            ExitCode::SUCCESS
        }
        [arg] if arg == "--version" || arg == "-V" => {
            println!("onebudgetspec-github-action {}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        _ => {
            eprintln!("error: unsupported arguments; use --help");
            ExitCode::from(2)
        }
    }
}

fn help() {
    println!(
        "onebudgetspec-github-action — scaffold only\n\nUsage: onebudgetspec-github-action [--help | --version]\n\nNo operational commands are implemented."
    );
}
