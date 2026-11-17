//! Child processes for the reader fixtures.

use std::error::Error;
use std::io::Write;
use std::path::Path;
use std::process::Child;
use std::process::ChildStdin;
use std::process::Command;
use std::process::Output;
use std::process::Stdio;

/// A fixture result with its original error.
pub(crate) type TestResult<T> = Result<T, Box<dyn Error>>;

/// Reap the child after an input error or a panic.
pub(crate) struct ReapedChild(Child);

impl ReapedChild {
    pub(crate) fn spawn(command: &mut Command) -> TestResult<Self> {
        Ok(Self(
            command
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()?,
        ))
    }

    pub(crate) fn input(&mut self) -> TestResult<ChildStdin> {
        self.0
            .stdin
            .take()
            .ok_or_else(|| "the child has no stdin".into())
    }

    pub(crate) fn output(mut self) -> TestResult<Output> {
        // Close stdin before reading output when the caller supplies no input.
        drop(self.0.stdin.take());
        let mut stdout = self.0.stdout.take().ok_or("the child has no stdout")?;
        let mut stderr = self.0.stderr.take().ok_or("the child has no stderr")?;
        let (stdout, stderr) = std::thread::scope(|scope| {
            let errors = scope.spawn(move || {
                let mut bytes = Vec::new();
                std::io::Read::read_to_end(&mut stderr, &mut bytes).map(|_| bytes)
            });
            let mut bytes = Vec::new();
            std::io::Read::read_to_end(&mut stdout, &mut bytes).inspect_err(|_| {
                let _ = self.0.kill();
            })?;
            let errors = errors.join().map_err(|_| "the stderr reader panicked")??;
            Ok::<_, Box<dyn Error>>((bytes, errors))
        })?;
        Ok(Output {
            status: self.0.wait()?,
            stdout,
            stderr,
        })
    }
}

impl Drop for ReapedChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// Run one Git operation and return its text output.
pub(crate) fn git(repository: &Path, arguments: &[&str]) -> TestResult<String> {
    git_input(repository, arguments, &[])
}

/// Run one Git operation with fixture bytes on stdin.
pub(crate) fn git_input(repository: &Path, arguments: &[&str], input: &[u8]) -> TestResult<String> {
    let mut child = ReapedChild::spawn(
        Command::new("git")
            .arg("-C")
            .arg(repository)
            .args(arguments),
    )?;
    child.input()?.write_all(input)?;
    let output = child.output()?;
    assert!(
        output.status.success(),
        "Git {arguments:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(String::from_utf8(output.stdout)?.trim().to_owned())
}
