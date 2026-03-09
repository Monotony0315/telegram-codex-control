use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;

use crate::events::normalize_line;

pub enum Mode {
    Exec { prompt: String },
    Resume { thread_id: String, prompt: String },
}

pub struct RunConfig {
    pub workspace_root: String,
    pub codex_bin: String,
    pub codex_args: Vec<String>,
    pub mode: Mode,
}

pub fn run(config: RunConfig) -> Result<i32, String> {
    let mut argv = config.codex_args;
    match config.mode {
        Mode::Exec { prompt } => {
            argv.push("exec".to_string());
            argv.push("--json".to_string());
            argv.push("--".to_string());
            argv.push(prompt);
        }
        Mode::Resume { thread_id, prompt } => {
            argv.push("exec".to_string());
            argv.push("resume".to_string());
            argv.push("--json".to_string());
            argv.push(thread_id);
            argv.push("--".to_string());
            argv.push(prompt);
        }
    }

    let mut child = Command::new(&config.codex_bin)
        .args(&argv)
        .current_dir(Path::new(&config.workspace_root))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("failed to spawn codex helper child: {err}"))?;

    let output = Arc::new(Mutex::new(std::io::stdout()));
    let stdout_handle = child.stdout.take().map(|stdout| {
        let output = Arc::clone(&output);
        thread::spawn(move || read_stream(stdout, output, "stdout"))
    });
    let stderr_handle = child.stderr.take().map(|stderr| {
        let output = Arc::clone(&output);
        thread::spawn(move || read_stream(stderr, output, "stderr"))
    });

    let status = child
        .wait()
        .map_err(|err| format!("failed waiting for child process: {err}"))?;

    if let Some(handle) = stdout_handle {
        handle
            .join()
            .map_err(|_| "stdout reader thread panicked".to_string())?
            .map_err(|err| format!("failed to read child stdout: {err}"))?;
    }
    if let Some(handle) = stderr_handle {
        handle
            .join()
            .map_err(|_| "stderr reader thread panicked".to_string())?
            .map_err(|err| format!("failed to read child stderr: {err}"))?;
    }

    if status.success() {
        return Ok(status.code().unwrap_or(0));
    }

    Err(format!("child exited with status {:?}", status.code()))
}

fn read_stream<T>(stream: T, output: Arc<Mutex<std::io::Stdout>>, stream_name: &str) -> Result<(), String>
where
    T: std::io::Read,
{
    let reader = BufReader::new(stream);
    for line in reader.lines() {
        let line = line.map_err(|err| format!("{stream_name}: {err}"))?;
        if let Some(normalized) = normalize_line(&line) {
            let mut guard = output
                .lock()
                .map_err(|_| format!("{stream_name}: failed to lock stdout"))?;
            writeln!(guard, "{normalized}")
                .map_err(|err| format!("{stream_name}: failed to write normalized output: {err}"))?;
        }
    }
    Ok(())
}
