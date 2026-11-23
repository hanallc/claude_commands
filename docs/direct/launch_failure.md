# Worker launch failure

<WorkerLaunchFailure>
Check the local launcher, model, configuration, and publication fault before treating a provider message as the cause.
A real `hit your usage limit` launch or turn failure turns on <NoWorkerMode/> in `commands/unit/direct.md`.
A failed launch keeps the run open and records the failure only for a pass that the launch started.
A retry uses the same run.
The launcher keeps the phase reservation and the launch exit code.
When the director ends the run, record `finish-run` and run `end_session.sh` under <RunSummary/>.

Inspect `worker.py list all` and the named pane.
Keep incomplete, unpublished, busy, and unknown workers unchanged.
A failed fresh start prints `publication failed: ...` and retains its pane without a saved pass.
Rerun the original launcher command. It publishes once from that pane.
For a published worker, resume the same pass through its saved launcher command.
</WorkerLaunchFailure>
