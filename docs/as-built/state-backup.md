# AWS cost history backup

The backup copies AWS history from natedev to the Mac. It selects files with the include and exclude patterns in `config/state_backup.conf`.

An exclude pattern takes priority. The tool skips non-regular files, symlinks, and temporary files from the shared atomic writer.

The exclude list omits known files that hold presigned URLs.

The tool checks the selected files for AWS credentials, presigned URL fields, and private key headers. A match stops the backup before it sends data.

## Commands

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/backup/state_backup.py run
~/.claude/scripts/lib/py ~/.claude/scripts/backup/state_backup.py status
```

The Mac refuses symlinks in the archive path. It verifies the SHA-256 and publishes the checksum before the archive. The tool keeps earlier archives.

The tool saves the time, archive name, file hashes, and destination in `~/.local/state/state-backup/last.json`. It uses `XDG_STATE_HOME` when set.

The next run prints `unchanged` if the file paths, hashes, and destination are the same. The `status` command prints the saved time, archive name, and file count.

A failed run sends an alert with the key `state-backup:aws`. A successful run retires the alert if the failed run saved the pending alert marker.

## Settings

Edit `~/.claude/config/state_backup.conf` on natedev.

| Key | Purpose |
| --- | --- |
| `source.folder` | Local folder that holds AWS history. |
| `include.patterns` | Space-separated patterns for paths relative to the source. A `/**` suffix includes all files under a folder. |
| `exclude.patterns` | Space-separated patterns for paths to omit, including logs, locks, and presigned URL files. |
| `interval_minutes` | Minutes between timer starts. |
| `mac.folder` | Archive folder on the Mac. A leading `~/` uses the Mac account's home folder. |

The backup uses the `mac` target in `config/build_targets.conf`. It reads the SSH host and connection timeout from that target's settings file, `config/mac_test.conf`.

## Restore to scratch

1. Copy one `aws-state-*.tar.gz` archive and its adjacent `.sha256` file to the same local folder.
2. Choose an empty or absent scratch folder outside the backup source.
3. Run the restore command with the actual archive name and scratch folder.
4. Inspect the restored files in the scratch folder.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/backup/state_backup.py \
  restore ./aws-state-20261120T230000000000Z.tar.gz --into ./aws-history-check
```

The tool checks the archive SHA-256 before it extracts files. It refuses absolute paths, parent traversal, links, and special files.

## NixOS user service and timer

Save this module next to a copy of `state_backup.conf` in the NixOS configuration. Use the same interval value as the live configuration.

Rebuild the NixOS configuration after you change the interval. The service reads the live backup settings on each run.

```nix
{ lib, pkgs, ... }:
let
  settings = builtins.readFile ./state_backup.conf;
  intervalLines = builtins.filter (match: match != null) (
    map (builtins.match "[[:space:]]*interval_minutes[[:space:]]*=[[:space:]]*([1-9][0-9]*)[[:space:]]*")
      (lib.splitString "\n" settings)
  );
  intervalMinutes =
    assert builtins.length intervalLines == 1;
    builtins.head (builtins.head intervalLines);
in {
  systemd.user.services.state-backup = {
    description = "Copy AWS cost history to the Mac";
    path = [ pkgs.bash pkgs.coreutils pkgs.openssh pkgs.python313 ];
    serviceConfig = {
      Type = "oneshot";
      UMask = "0077";
      ExecStart = "%h/.claude/scripts/lib/py %h/.claude/scripts/backup/state_backup.py run";
    };
  };

  systemd.user.timers.state-backup = {
    description = "Copy AWS cost history at the configured interval";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnStartupSec = "${intervalMinutes}m";
      OnUnitActiveSec = "${intervalMinutes}m";
      Unit = "state-backup.service";
    };
  };
}
```
