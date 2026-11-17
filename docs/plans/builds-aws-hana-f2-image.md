# Hana F2 image proposal

The natedev owner applies these changes in the NixOS branch.
This worktree changes no NixOS file and runs no AWS command.
Placement remains disabled.

1. Add `git` and `curl` to the Hana image.
2. Expose the shared toolchain from `hosts/aws-build.nix` through a module without host services.
3. Import that module in the Hana image.
4. Keep the shared Rust proxies, compiler tools, linker, Cargo configuration, and sccache environment together.
5. Install the rendered `runtime/` directory at `/var/lib/hana-runtime` with root ownership.
6. Give runtime directories mode `0755` and runtime files mode `0644`.
7. Preserve mode `0600` for the receiver, safety configuration, and control files.
8. Give `hana-runner` the home `/var/lib/hana/runner` on the data filesystem.
9. Create that home with `hana-runner` ownership after the data mount succeeds.
10. Give `hana-runner` its approved slot, Cargo, mirror, shell, and temporary directories on the data filesystem.
11. Install the approved `roots.slots.aws-hana`, `slots.aws-hana.hana.max`, and `slots.aws-hana.hana.max_bytes` settings.
12. Preserve the root-only IMDS rule and the independent guard and watchdog units.
13. Review the complete import closure in `scripts/aws_target/hana_render.files`.
14. Build the image after the owner reviews the shared toolchain and slot capacity.

The receiver reads signed command and fence objects beside the permit object.
It keeps attempt journals outside the leased source directories.
It serializes command acceptance and fences through one root-owned lock.
It checks the actual data mount and the current acknowledged permit before command admission.

The receiver invokes `hana_guest.admit_command` before it starts the unit.
The unit runs `/var/lib/hana-runtime/runner.py` as `hana-runner` inside `hana-work.slice`.
The unit acquires the assigned slot after systemd starts the unit.
The unit retains maker descriptors through source preparation, shell preparation, Cargo, and result capture.
`ExitType=cgroup` keeps the unit active while command descendants remain.
The watchdog can kill these processes during source or shell preparation.
`RuntimeMaxSec` bounds the whole unit, including source and shell preparation.
The guest also bounds command output reads and the final process wait with a monotonic deadline.
The deadline ends the command cgroup even when the controller continues permit renewal.
The receiver reports a terminal loss after the cgroup and exact slot lease end.

The receiver supplies only the request and the output bound through standard input.
The request includes scoped source GET access.
The runner receives no observation PUT URL, result PUT URL, permit file, or controller credential.
Root captures the bounded result and publishes it through the signed result URL.
Missing or uncertain command evidence retains the attempt.

| New configuration key | Value | Source and reason |
| --- | --- | --- |
| `execution_shell` | `ci` | The existing Linux policy documents `devshell.hana=ci` in `config/README.md`. |
| `execution_shell_timeout_s` | `120` | `loopback.shell_timeout_s` supplies the existing Linux shell deadline. |
| `execution_compiler_version` | `1.99.1` | `hosts/aws-build.nix` supplies the shared AWS compiler release. |
| `execution_command_timeout_s` | `600` | `loopback.command_timeout_s` supplies the existing Linux verify proof deadline in `build_targets.conf`. |
| `free_floor_gib` | `30` | E supplies this shared host floor in `hana_aws.conf`. The guest uses the same floor. |

The shell and compiler checks precede the requested Cargo judgment.
The source toolchain hash still binds `rust-toolchain.toml` to the exact captured commit.
The image owner reviews the disk floor before activation.
This proposal changes no existing configuration value.

F2 reports `MissingBasis` as a bounded refusal diagnostic.
F1 calls `SourceSender.retry_complete` for the original captured commit.
F1 sends `HanaSourceUpdateEnvelope` with the `source-update` action through the command object.
The action carries no new command permission.
The receiver checks the original request binding, the current permit, and the exact attempt fence.
The receiver requires the original unit and its slot lease to end before it accepts source preparation.
The source update must preserve the commit, command, environment, features, toolchain, repository, attempt, and slot.
The complete bundle must require no basis commits.

The receiver starts one bounded source-only unit as `hana-runner` inside `hana-work.slice`.
That unit materializes the complete source and runs no shell preparation or Cargo command.
The receiver reports `source_updated` after the source-only unit and its exact slot lease end.
Repeated delivery cannot repeat preparation or grant command execution.
Fencing stops both the original unit and the source-only unit.
F1 fences and settles the original attempt after source preparation.
Only a new queue assignment can run Cargo.
