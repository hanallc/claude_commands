# Run one Hana nextest step on a GPU Spot box

Start with one headless shader test on an 8-vCPU, 32 GiB box. Choose the lowest live Spot price between `g4dn.2xlarge` and `g6.2xlarge`. Both sizes fit an applied G and VT quota of 8 vCPUs. [EC2 instance specifications](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html#accelerated-computing-performance)

## 1. What exists

| Part | Existing path and evidence |
| --- | --- |
| Launch and quota | Gate deployment records the AMI, launch template, security group, and subnets. The GPU trial checks applied G and VT Spot quota, offerings, and price. It submits one tagged Spot instance with a client token and a delete-on-termination root disk. `scripts/aws_target/deploy.py:1543–1549`, `scripts/aws_target/gpu_size_trial.py:888–932,1082–1140`. |
| Image | The trial starts the gate NixOS AMI, imports a pinned NixOS closure with NVIDIA, and reboots. The guest checks hardware Vulkan. The boot timer and the gate expiry job bound the lifetime. `scripts/aws_target/gpu_size_trial.py:118–133,789–809,831–877`, `docs/plans/builds-aws/gpu-trial.nix:5–25,28–57`, `docs/plans/builds-aws/cloudformation.json:645,983–1022`. |
| Source and results | The GPU trial uploads a source tar, locked Cargo inputs, a closure, and a nextest archive. Signed S3 requests deliver inputs and return result archives. The guest runs the archive twice. The gate worker also validates and imports result records. `scripts/aws_target/gpu_size_trial.py:517–561,771–809,934–1018`, `scripts/aws_target/cloud_gate.py:939–1017,1251–1262`. |
| Permissions | The scaler is an IAM user. `GateGpuTrialScalerPolicy` grants tagged GPU Spot launches for configured types. `GateScalerPolicy` grants the other launch resources, role pass, termination, and S3 access. The instance uses the existing gate profile. `docs/plans/builds-aws/cloudformation.json:171–178,517–569,700–975,1170–1246`. |
| Cost | The worker reserves the full box lifetime before launch. It records the launch, then settles the reservation after verified termination. The trial cap sits inside the shared account limits. `scripts/aws_target/gpu_size_trial.py:1082–1140`, `scripts/aws_target/cloud_gate.py:216–273,453–464`, `scripts/aws_target/ledger.py:1585–1617,1664–1668`, `config/aws_target.conf:14–16`, `config/cloud_gate.conf:11–24`. |

## 2. What is missing

- **Route.** `verify.sh test` builds the nextest arguments and requests placement before it takes a local token. The default route is `AnyEligibleTarget`. Add a one-slot Linux target named `hana-gpu` through the existing transport and assignment boundary. Admit only the exact pilot selection and the same captured source. Use the normal queue and hold rules. After the pinned proof, put this target before natedev for eligible steps. Keep the separate `hana-aws` check and clippy route. `scripts/direct/verify.sh:1354–1377,2700–2718,2895–2905`, `scripts/build_targets/build_targets.py:352–438`, `scripts/build_targets/execution_transport.py:7–17`, `scripts/aws_target/hana_placement_adapter.py:62–86`.
- **Source and cache.** Capture the caller tree with `copies.capture_source` and export that commit with the trial's tar path. A fresh guest needs a complete tree. Package locked Cargo registry and Git dependencies with the trial's isolated `cargo fetch --locked` path. The local `origin/main` reference at `262c8a2275` has 886 registry sources, zero Git source entries, and zero submodule entries. The source tar includes workspace and tracked vendor paths. Fetch future locked Git inputs on natedev and fail before launch if they are unavailable. Run Cargo offline in the guest. Use a fresh `CARGO_TARGET_DIR` on the disposable root. A shared sccache or a persistent target volume can follow measured need. `scripts/copies/copies.py:275–318`, `scripts/aws_target/hana_transport.py:146–158,172–194`, `scripts/aws_target/gpu_size_trial.py:353–361,434–440`, Hana `Cargo.toml:1–52,189` and `Cargo.lock:1–9,551–554,2756–2758` at that reference.
- **Build and run.** Reuse the trial's Nix toolchain and Hana's `.#default` closure. Keep pkg-config, the C linker and mold, bindgen, ALSA, udev, Vulkan, Wayland, X11, PipeWire, GBM, DRM, and XCB. Use NVIDIA Vulkan and require a hardware adapter before Cargo starts. Preserve the requested selection and nextest profile. Use Hana's pinned nextest version for wrapper support. Limit the guest service to 28 GiB. Set eight Cargo jobs. Preserve the nextest profile and its test groups. Keep the remaining RAM for the OS. Hana `nix/bevy-shell.nix:29–69`, `flake.nix:26–62`, `.github/workflows/ci.yml:73–76`, `scripts/aws_target/gpu_size_trial.py:118–133,380–440`.
- **Return.** Add a one-command path to the trial worker. Use its existing S3 prefixes, reservation, lifetime, and cleanup. Send the attempt, source identity, exit status, stdout, and stderr back to the waiting caller. Reuse the Hana result validation and publication pattern. A completed failing test returns its original exit status. A missing or invalid result reports infrastructure failure. Retain the claim until termination settles before a retry or fallback. `scripts/aws_target/gpu_size_trial.py:962–1018,1132–1140`, `scripts/aws_target/hana_result.py:41–67,148–162`, `scripts/mac_test/offload.py:2881–2905,3239–3266`.

## 3. User commands

**Setup on natedev.** Run this command in the commands checkout after the implementation lands.

```sh
python3 scripts/aws_target/deploy.py up --workload merge-gates --approve-resources
```

After deployment, the command uses the worker's scaler profile to check GPU access in the home region.
Setup requires the profile's caller ARN to match the stack's scaler user ARN.
It reads quota, offerings, Spot prices, instances, and Cost Explorer charges.
It checks local credential export with the same reader that the trial uses.
It sends a `RunInstances` DryRun with the trial launch arguments when a saved subnet offers the type.
If no saved subnet offers a type, setup reports that launch as unverified.
Setup retries denied launch DryRuns within the deadline in `cloud_gate.conf`.
The DryRun must return `DryRunOperation`. [EC2 RunInstances API](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_RunInstances.html)
The request includes tags for the instance and volume. [EC2 tag permissions](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/supported-iam-actions-tagging.html)
It checks termination with DryRun if it finds a matching GPU Spot instance.
The administrator simulates termination in each saved Spot candidate region with subnets.
Each simulation includes the region in `aws:RequestedRegion` and uses the instance ARN and tag context.
A simulated denial fails setup.
Setup reports termination as unverified if the administrator cannot call `iam:SimulatePrincipalPolicy`. [IAM simulation API](https://docs.aws.amazon.com/IAM/latest/APIReference/API_SimulatePrincipalPolicy.html)
Setup checks S3 put and get for one fixed object under each trial prefix.
It lists the exact result key.
It creates and aborts a multipart upload on the input probe key.
A failed required probe makes setup exit with status 1.
Setup prints one result per operation and one advice line per error kind.
Use the pilot to verify capacity, guest boot, signed transfers, and cleanup.

One approved setup command applies the gate policy update and checks the resulting access.
The local deployment record lists 19 regions and includes `us-east-2`.
Reuse the gate bucket, template, profile, and NixOS AMI.
Prepare the NVIDIA closure locally. The trial uploads the closure to the gate bucket.
Setup creates the probe objects. Keep the target disabled until the required checks pass.

**One normal test command for the pilot.** Setup and the first test take two commands in total. Run the test from the Hana checkout at the reviewed source. It creates one temporary Spot instance and root disk, plus request and result objects. `scripts/direct/verify.sh:1370–1377,2955–2961`, `scripts/aws_target/gpu_size_trial.py:1082–1140`.

```sh
~/.claude/scripts/direct/verify.sh test hana_mimesis_tools --test shader_variants --target hana-gpu
```

## 4. Which steps fit

- Hana's `gpu` group runs two large tests one at a time. The `gpu-render` group covers shader binaries and pixel tests. The wrapper hides the GPU from other groups. The `real-camera` group needs camera hardware. Start with the `hana_mimesis_tools::shader_variants` binary. It uses a render device and disables Winit and the primary window. The production display smoke uses Xvfb. Exclude live camera and desktop cases. Hana `.config/nextest.toml:52–103,249–256,276–288`, `crates/hana_mimesis_tools/tests/shader_variants.rs:17–42,135–155`, `scripts/ci/production_display_smoke.sh:52–60`, all at `262c8a2275`.
- The no-adapter run found 946 of 11,958 executed tests that needed an adapter, or **7.9%**. It skipped 36 tests. The failed tests accounted for **56.4% estimated** historical Hana nextest CPU. That share covers GPU need alone. [No-adapter report, lines 1–6 and 147–160](/tmp/claude/unit/8bf65047-bbca-4bf7-819b-f44a96b6b053/phases/hana-nextest/measure/report.md:1)
- A read-only join of the November 10–17 EST buildlog with that report found **146** exact one-test GPU runs at or below 14 GiB. They used 67,705 CPU-seconds, or **3.3%** of the reported nextest CPU. The selected shader test had three matching runs and a 10.51 GiB maximum. This is a historical screen. A 4-vCPU cold pass must verify the fit. Admit more exact selections only after their measured peaks fit the target. `scripts/buildlog/index.py:153–171`, `scripts/build_queue/step_estimator.py:379–380`, and the report's `tests.csv`.

## 5. Baseline estimate on 4 vCPUs

These estimates assume a ready closure, an available Spot offer, and one shader test. Measure each stage during the pilot.

| Stage | Time | Basis |
| --- | ---: | --- |
| Box request to EC2 start | 1–3 min, estimate | Time after a successful offer. Capacity refusal can end the attempt. `scripts/aws_target/gpu_size_trial.py:905–932,1122–1135`. |
| Image boot, closure import, reboot | 3–8 min, estimate | The trial already records import and reboot timestamps. `scripts/aws_target/gpu_size_trial.py:831–877`. |
| Source and Cargo inputs | 2–8 min, estimate | Includes host upload and guest download. Measure bytes and time independently of the closure transfer. `scripts/aws_target/gpu_size_trial.py:934–957`. |
| Cold test build | 25–65 min, estimate | Natedev built 666 crates for the separate no-adapter proof in **18 min 43 s measured**. Its buildlog peak was 16.16 GiB. The records have no 4-vCPU cold measurement. [Proof log](/tmp/claude/unit/8bf65047-bbca-4bf7-819b-f44a96b6b053/phases/hana-nextest/measure/proof.log:750) |
| Test | 0.5–2 min, estimate | The pilot's historical median test time was 1.136 s. The estimate includes guest test startup and nextest discovery. `scripts/buildlog/index.py:153–171`. |
| Result return | 0.5–1 min, estimate | Includes S3 upload and caller collection. `scripts/aws_target/gpu_size_trial.py:781–782,994–1018`. |

The estimated work totals **32–87 min**. The local build has a stated **90 min queue wait**. The comparison lifetime stops guest work at minute 55. The Hana guest stops at the end of its 90-minute lifetime. Record the cold pass before claiming a faster route. Prepare each new closure during setup.

## 6. Cost

The saved snapshot at **23:39:59 EST on November 17** showed `g4dn.xlarge` at **$0.2733/h** in `us-east-2` and **$0.1137/h** in `eu-north-1`. Both regions showed 8 applied G and VT Spot vCPUs. Refresh eligibility and check quota in use before launch. A snapshot gives a price observation. `scripts/aws_target/aws_target.py:63–75,223–241`, `scripts/aws_target/spot_table.py:135–170`.

The one-step caps are **$0.75/h** for `g4dn.2xlarge` and **$0.98/h** for `g6.2xlarge`. The 157-minute reservations are about **$2.01** and **$2.61**, including the configured $0.05 extra. The trial cap is `gpu_size_trial_limit_usd=6`. The account limits are `daily_limit_usd=35` and `monthly_limit_usd=400`. The worker refuses launch when the full reservation exceeds a limit. `config/cloud_gate.conf:11–34`, `config/aws_target.conf:14–16`, `scripts/aws_target/ledger.py:1585–1617`.

The worker estimates S3 LIST requests, gp3 root storage, and public IPv4 for the booked lifetime. The LIST estimate includes the final check. The worker uses `gpu_size_trial_gp3_usd_per_gib_month_by_region` for the selected region. If the map has no entry, the worker uses `gp3_usd_per_gib_month`. The disk estimate uses a 30-day month. The IPv4 estimate uses `ipv4_usd_per_month` and a 730-hour month. The worker refuses launch if the estimate exceeds the extra allowance. [Amazon EBS pricing](https://aws.amazon.com/ebs/pricing/)

Hana excludes offers that exceed the extra allowance. It ranks the remaining offers by Spot compute cost plus the extra cost for the booked lifetime. The worker checks the allowance again after it rechecks Spot price and quota.

## 7. Work order A implementation

| Files | Behavior and fake proof |
| --- | --- |
| `config/cloud_gate.conf` | `hana_gpu_instance_types` sets the candidate list. `hana_gpu_spot_max_usd_per_hour` caps each type. `hana_gpu_lifetime_minutes` sets the guest lifetime to 90 minutes. |
| `scripts/aws_target/deploy.py` | Gate setup grants the types from both GPU trial lists. It saves the deployed list in `gate-deployment.json`. |
| `scripts/aws_target/aws_target.py`, `spot_table.py`, `test_aws_target.py` | The hourly read requests both types. The AWS page shows their prices and quota fit. The snapshot test covers a type shared with the comparison list. |
| `scripts/aws_target/gpu_size_trial.py`, `docs/plans/builds-aws/gpu-trial.nix` | Preparation exports the selected commit and fetches its locked dependencies. The toolchain includes the official nextest 0.9.143 release with a fixed SHA-256. The host and guest verify the nextest version against the Hana CI pin. The guest checks hardware Vulkan and runs nextest once. Cargo uses a fresh target directory, offline mode, eight jobs, and a 28 GiB service memory limit. |
| `scripts/aws_target/test_gpu_size_trial.py`, `test_control.py` | Fakes cover type choice, quota in use, stale offers, zero tests, wrong nextest versions, launch timeouts, and cleanup. The CLI test covers preparation progress during a hold. The snapshot fixture covers both GPU type lists and a shared type. |

Run the one-step command from the commands checkout after the setup proof in work order B.

```sh
python3 -m scripts.aws_target.gpu_size_trial nextest \
  --repo ~/rust/hana --commit "$HANA_COMMIT" \
  --instance-types g4dn.2xlarge,g6.2xlarge -- \
  --profile ci --workspace --test shader_variants -E 'package(hana_mimesis_tools)'
```

Supply an explicit test filter. A pass requires a positive test count and a passing result for each selected test. The completed call prints one JSON object with the attempt, commit, tree, exit code, stdout, and stderr. Nextest writes test events to stdout. Preparation progress goes to stderr. An infrastructure failure returns 125 and a message on stderr.

The worker subtracts pending and running Spot instances from the applied G and VT quota. It checks the selected price and quota again before launch. A confirmed first-attempt quota refusal ends the booking and sends Hana to the next eligible region. A confirmed capacity refusal lets the comparison continue to the next size. Uncertain launch errors leave the booking open for discovery. An open gate booking blocks a new Hana nextest launch until cleanup.

The worker lists the exact S3 result key before it downloads the object. It uses `gpu_size_trial_result_poll_s` between GPU result checks. AWS service errors retain their codes. Both collectors check for outstanding results once more at the deadline.

The guest arms shutdown before downloading its inputs. The Hana expiry tag and guest timer use the same deadline. The expiry job provides cleanup if the guest timer and host cleanup fail. The reservation adds 60 minutes for the schedule and one minute for EventBridge delivery. It also includes three one-minute function attempts and retry delays of one and two minutes. [Lambda function error retries](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html)

The worker selects only types listed in the saved deployment record.
Gate setup refreshes older records and checks access for every saved GPU type.

## 8. Work order B2 implementation

The queue and `verify.sh` support one pinned `hana-gpu` slot. The configured pilot is `hana_mimesis_tools:shader_variants`.
The executor captures the assigned source and delegates the nextest run to Work Order A.
A completed test returns its original status and both output streams.
A claim remains pending after the GPU service starts until it confirms cleanup.
Cancellation stops the assigned service and runs its cleanup.
A settled infrastructure refusal releases the slot.
The pinned caller sees the cause and exits with status 2.

The route starts disabled. Set `hana_gpu_route_enabled=on` after the gate setup GPU probe succeeds.
The route also checks the gate deployment record for the configured GPU types.
The live pinned run remains the proof for target order changes.
