"""Implement Hana placement behind the guest-safe boundary types."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from scripts.aws_target.hana_placement import (
    ExecutionTransport, HanaControlTransport, HanaHostGeneration,
    DisabledHanaRoute, EnabledHanaRoute, HanaRouteSettings, HanaCheck, HanaClippyJudgment,
    HanaStepRefused, HanaStepPolicy, HanaLifecycleEvidence, HanaUnprovisioned,
    HanaLifecycleUnavailable, HanaClaimResult, HanaClaimPending, HanaClaimAdopted,
    HanaClaimRefused, HanaClaimRetained, generation_text,
)
if TYPE_CHECKING:
    from scripts.aws_target import hana_box as box
    from scripts.aws_target.hana_placement import HanaEvidenceRequest
    from scripts.build_queue import admission_records as records, placement
    from scripts.build_targets.target_config import SshTarget
    from scripts.lib import conf
    from scripts.mac_test import claim_state

def read_execution_transport(target: SshTarget) -> ExecutionTransport:
    """Select the configured transport before reading SSH settings."""
    from scripts.build_targets import target_config

    return target_config.read_execution_transport(target)

def read_settings(configuration: conf.ConfigRead) -> HanaRouteSettings:
    """Read the workload without reading credentials or deployment records."""
    from scripts.buildlog.repository import remote_identity
    from scripts.lib import conf

    def repository_key(value: str) -> str:
        if value != remote_identity(value, str(configuration.source.parent)):
            raise ValueError('expected a normalized repository identity')
        return value

    def permitted_kinds(value: str) -> tuple[placement.CommandKind, ...]:
        from typing import cast

        kinds = tuple(part.strip() for part in value.split(','))
        if len(set(kinds)) != len(kinds) or any(kind not in ('check', 'lint') for kind in kinds):
            raise ValueError('Hana permits only check and lint judgments')
        return cast('tuple[placement.CommandKind, ...]', kinds)

    if not configuration.value('enabled', conf.on_off):
        return DisabledHanaRoute()
    key = configuration.value('repository_identity', repository_key)
    kinds = configuration.value('kinds', permitted_kinds)
    def single_step(value: str) -> int:
        if conf.positive_whole(value) != 1:
            raise ValueError('Hana requires one step')
        return 1

    maximum = configuration.value('max_steps', single_step)
    floor = configuration.value('free_floor_gib', conf.positive_whole)
    return EnabledHanaRoute(key, kinds, maximum, floor)


def classify_command(step: str, kind: str, command: tuple[str, ...]) -> HanaStepPolicy:
    """Permit direct Cargo judgments without repair or command wrappers."""
    if step not in ('check', 'clippy', 'lint') or kind not in ('check', 'lint'):
        return HanaStepRefused('Hana permits only check and clippy judgments')
    if len(command) < 2 or Path(command[0]).name != 'cargo':
        return HanaStepRefused('Hana requires a direct Cargo judgment')
    arguments = command[1:]
    if arguments[0].startswith('+'):
        arguments = arguments[1:]
    verb = 'check' if kind == 'check' else 'clippy'
    if not arguments or arguments[0] != verb:
        return HanaStepRefused('The Cargo command differs from the judgment kind')
    forbidden = ('--fix', '--allow-dirty', '--allow-staged', '--broken-code', '--manifest-path', '--config', '--target-dir')
    if any(arg == flag or arg.startswith(flag + '=') for arg in arguments for flag in forbidden):
        return HanaStepRefused('Hana refuses repair flags and command path overrides')
    if verb == 'check' and step != 'check' or verb == 'clippy' and step not in ('clippy', 'lint'):
        return HanaStepRefused('The recorded step differs from the Cargo judgment')
    return HanaCheck(command, step) if verb == 'check' else HanaClippyJudgment(command, step)


def classify_step(profile: records.StepProfile | records.ProgramProfile,
                  constraints: placement.PlacementConstraints) -> HanaStepPolicy:
    kind = constraints.command_kind
    return classify_command(profile.step, kind if isinstance(kind, str) else '', constraints.command)


def controller_for(transport: HanaControlTransport, *, read_only: bool = False) -> box.HanaController:
    """Use the existing account directory and lifecycle controller."""
    from scripts.aws_target import hana_box as box
    from scripts.build_targets import target_config
    from scripts.lib import conf

    directory = target_config.ssh_target_state_directory('aws')
    if read_only:
        provider: box.HanaProvider = box.ProviderNotInstalled()
    else:
        from scripts.aws_target.hana_provider import AwsHanaProvider
        provider = AwsHanaProvider.from_settings(conf.path('aws_target'), directory, transport.settings)
    return box.HanaController(directory, conf.path('aws_target'), transport.settings, provider)


def validate_assignment(assignment: placement.PlacementAssignment, holder: placement.ClaimHolderIdentity
                        ) -> HanaControlTransport | HanaClaimRefused:
    """Check the configured destination, scope, source, and live local holder."""
    from scripts.build_queue import placement
    from scripts.build_targets import target_config
    from scripts.lib import conf, process_start
    from scripts.mac_test import claim_state
    from scripts.buildlog import store

    destination = assignment.destination
    target = target_config.configured_ssh_target(destination.target)
    transport = read_execution_transport(target)
    if not isinstance(transport, HanaControlTransport):
        return HanaClaimRefused('The assigned target does not use Hana control')
    settings = read_settings(conf.read(transport.settings))
    if isinstance(settings, DisabledHanaRoute):
        return HanaClaimRefused('Hana is disabled')
    if (destination.provider != 'ssh' or destination.host != transport.host or destination.slot != 1
            or not isinstance(destination.scope, claim_state.RepositoryScope)
            or destination.scope.slot != 1 or destination.scope.key != settings.repository_key
            or not isinstance(assignment.source_key, placement.SourceKeyKnown)
            or not assignment.source_key.key or not assignment.episode or not assignment.attempt):
        return HanaClaimRefused('The assignment differs from the configured Hana scope')
    if (holder.host != store.host_name() or holder.pid <= 0
            or process_start.process_start(holder.pid) != process_start.Started(holder.start_time)):
        return HanaClaimRefused('The local claim holder is not current')
    return transport


def claim_assigned(assignment: placement.PlacementAssignment, holder: placement.ClaimHolderIdentity,
                   controller: box.HanaController, *, worktree: str = '') -> HanaClaimResult:
    """Commit or inspect only the claim for this exact assignment."""
    from datetime import UTC, datetime
    import json
    from scripts.aws_target import hana_box as box
    from scripts.aws_target.hana_execution import execution_command_settled
    from scripts.build_queue import placement
    from scripts.lib.atomic_file import write_text_atomic
    from scripts.lib import conf
    from scripts.mac_test import claim_state, mac_test

    transport = validate_assignment(assignment, holder)
    if isinstance(transport, HanaClaimRefused):
        return transport
    if controller.workload_path.resolve() != transport.settings.resolve():
        return HanaClaimRefused('The lifecycle controller differs from the assigned workload')
    destination = assignment.destination
    execution = placement.assigned_claim_id(assignment.episode, assignment.attempt)
    reference = placement.ProviderClaimReference('ssh', destination.target, destination.scope,
        execution, assignment.episode, assignment.attempt)
    paths = claim_state.state_paths(destination.target)
    with claim_state.locked(paths.lock):
        held = claim_state.read_claims(paths)
        for old in held:
            previous = old.record
            if (old.scope != destination.scope or previous['claim_id'] != execution
                    or previous['episode'] != assignment.episode or previous['attempt'] != assignment.attempt
                    or previous['pid'] != holder.pid or previous['proc_start'] != holder.start_time
                    or previous['submitting_session'] != claim_state.session_id(assignment.session)):
                return HanaClaimRefused('Hana is busy')
        if execution_command_settled(controller.directory, execution):
            return HanaClaimRefused('The Hana attempt already has a settled command path')
        if held:
            # An existing target claim never creates another demand start for this assignment.
            status = controller.claim_status(execution)
            result = box.ClaimCommitted(status.claim) if isinstance(status, box.ClaimWaiting) else status
        else:
            # Bind before requesting work so a dead holder cannot release an unresolved start.
            controller.bind_execution(execution)
            result = controller.request(execution)
        if isinstance(result, box.ClaimRefused):
            return HanaClaimRefused(result.reason)
        snapshot = controller.inspect()
        if not isinstance(snapshot, HanaLifecycleEvidence):
            return HanaClaimRetained(reference, 'The Hana lifecycle evidence is unavailable')
        generation = (HanaHostGeneration(snapshot.deployment.deployment, snapshot.boot.start_token, snapshot.boot.boot_id)
            if isinstance(snapshot.boot, box.VerifiedBoot) else HanaHostGeneration(snapshot.deployment.deployment, '', ''))
        record: claim_state.RemoteJobClaim = {
            'claim_id': execution, 'episode': assignment.episode, 'attempt': assignment.attempt,
            'submitting_session': claim_state.session_id(assignment.session), 'pid': holder.pid,
            'proc_start': holder.start_time, 'what': 'Hana judgment', 'worktree': worktree,
            'since': datetime.fromtimestamp(result.claim.requested_at, UTC).isoformat(),
            'repo_name': destination.scope.name if isinstance(destination.scope, claim_state.RepositoryScope) else '',
            'repo_key': destination.scope.key if isinstance(destination.scope, claim_state.RepositoryScope) else '',
            'slot': 1, 'remote_host': transport.host,
        }
        if generation.boot_id:
            record['instance_generation'] = generation_text(generation)
        if not (snapshot.state == box.Lifecycle.READY and result.claim.active and generation.boot_id
                and isinstance(snapshot.permit, box.DeliveredPermit) and snapshot.permit.execution == execution
                and snapshot.permit.expires > int(controller.clock())):
            record['cleanup_pending'] = True
            write_text_atomic(mac_test.claim_path(paths, destination.scope), json.dumps(record) + '\n')
            deadline = result.claim.requested_at + conf.read(transport.settings).whole('start_timeout_seconds')
            return HanaClaimPending(reference, result.claim, deadline)
        write_text_atomic(mac_test.claim_path(paths, destination.scope), json.dumps(record) + '\n')
        adopted = placement.AdoptedProviderClaim(reference, holder, assignment.session, transport.host, generation_text(generation))
        return HanaClaimAdopted(placement.ReservedHost(assignment, adopted), generation)


def release_assigned(assignment: placement.PlacementAssignment, holder: placement.ClaimHolderIdentity,
                     controller: box.HanaController) -> bool:
    """Release the exact target claim after durable execution and lifecycle settlement."""
    from scripts.aws_target.hana_execution import execution_command_settled
    from scripts.build_queue import placement
    from scripts.build_targets import target_config
    from scripts.mac_test import claim_state

    destination = assignment.destination
    execution = placement.assigned_claim_id(assignment.episode, assignment.attempt)
    if not execution_command_settled(controller.directory, execution):
        return False
    target = target_config.configured_ssh_target(destination.target)
    transport = read_execution_transport(target)
    if (not isinstance(transport, HanaControlTransport) or destination.host != transport.host
            or transport.settings.resolve() != controller.workload_path.resolve()):
        return False
    paths = claim_state.state_paths(destination.target)
    with claim_state.locked(paths.lock):
        held = claim_state.read_claims(paths)
        matching = tuple(claim for claim in held if claim.scope == destination.scope
            and claim.record['claim_id'] == execution and claim.record['episode'] == assignment.episode
            and claim.record['attempt'] == assignment.attempt and claim.record['pid'] == holder.pid
            and claim.record['proc_start'] == holder.start_time
            and claim.record['submitting_session'] == claim_state.session_id(assignment.session))
        if held and not matching and any(claim.record['claim_id'] == execution for claim in held):
            return False
        return _release_claims(controller, execution, matching)


def _release_claims(controller: box.HanaController, execution: str, matching: tuple[claim_state.HeldClaim, ...],
                    *, detached_only: bool = False) -> bool:
    from scripts.aws_target import hana_box as box

    snapshot = controller.inspect()
    if not isinstance(snapshot, HanaLifecycleEvidence):
        return False
    current = any(claim.execution == execution for claim in snapshot.claims)
    pending = isinstance(snapshot.operation, box.PendingOperation) and snapshot.operation.execution == execution
    if detached_only:
        if current or pending or snapshot.state in (box.Lifecycle.STARTING, box.Lifecycle.STOPPING):
            return False
    elif not matching and not current:
        return not pending
    elif snapshot.claims:
        if len(snapshot.claims) != 1 or not current or not controller.release(execution):
            return False
    elif (snapshot.state in (box.Lifecycle.STARTING, box.Lifecycle.STOPPING)
            or isinstance(snapshot.operation, box.PendingOperation)):
        return False
    for claim in matching:
        claim.path.unlink(missing_ok=True)
    return True


def fixed_policy(transport: HanaControlTransport, repository: str, kind: str) -> HanaStepRefused | EnabledHanaRoute:
    """Apply configuration and repository capabilities without lifecycle reads."""
    from scripts.lib import conf
    return configured_route(conf.read(transport.settings), repository, kind)


def configured_route(configuration: conf.ConfigRead, repository: str, kind: str) -> HanaStepRefused | EnabledHanaRoute:
    from scripts.mac_test import eligibility

    settings = read_settings(configuration)
    if isinstance(settings, DisabledHanaRoute):
        return HanaStepRefused('off')
    identity = eligibility.repository_identity(Path(repository))
    if isinstance(identity, eligibility.UnknownRepository) or identity.key != settings.repository_key:
        return HanaStepRefused('repo')
    if kind not in settings.kinds:
        return HanaStepRefused('command')
    return settings


def read_evidence(key: placement.HostEvidenceKey, request: HanaEvidenceRequest) -> placement.HostEvidence:
    """Project existing lifecycle and execution records without provider mutations."""
    from functools import partial
    import time
    from scripts.aws_target import hana_box as box, hana_spend, ledger
    from scripts.aws_target.hana_execution import (
        HanaSettled, claim_admission_refused, read_claim_execution, settle_unpublished_claim,
    )
    from scripts.build_queue import admission_records, placement
    from scripts.build_targets import target_config
    from scripts.lib import conf, process_start
    from scripts.mac_test import claim_state, mac_test
    from scripts.build_hold.owning_session import NoOwningSession, OwningSession
    from scripts.buildlog import store

    now = time.time()
    target = admission_records.effective_target(key.destination.target, key.settings)
    if not isinstance(target, target_config.SshTarget):
        return placement.HostEvidenceUnavailable(key, now, 'The SSH target is no longer configured')
    configuration = conf.read(target.settings, load=conf.read_bytes)
    transport = target_config.read_execution_transport(target, configuration=configuration)
    if not isinstance(transport, HanaControlTransport):
        return placement.HostEvidenceUnavailable(key, now, 'The target does not use Hana control')
    policy = configured_route(configuration, request.constraints.repository,
        request.constraints.command_kind if isinstance(request.constraints.command_kind, str) else '')
    if isinstance(policy, HanaStepRefused):
        return placement.FreshHostEvidence(key, now, '', placement.FixedExclusion(policy.reason))
    step = classify_step(request.profile, request.constraints)
    if isinstance(step, HanaStepRefused):
        return placement.FreshHostEvidence(key, now, '', placement.FixedExclusion('command'))
    controller = controller_for(transport, read_only=True)
    paths = claim_state.state_paths(target.name)
    with claim_state.locked(paths.lock):
        snapshot = controller.inspect()
        if isinstance(snapshot, HanaUnprovisioned):
            return placement.FreshHostEvidence(key, now, '', placement.TemporaryExclusion('unprovisioned'))
        if isinstance(snapshot, HanaLifecycleUnavailable):
            return placement.HostEvidenceUnavailable(key, snapshot.observed_at, snapshot.reason)
        held: list[claim_state.HeldClaim] = []
        for claim in claim_state.read_claims(paths):
            record = claim.record
            execution = record['claim_id']
            if ('remote_host' not in record or record['remote_host'] != transport.host
                    or execution != placement.assigned_claim_id(record['episode'], record['attempt'])
                    or snapshot.state in (box.Lifecycle.STARTING, box.Lifecycle.STOPPING)
                    or any(row.execution == execution for row in snapshot.claims)
                    or isinstance(snapshot.operation, box.PendingOperation) and snapshot.operation.execution == execution):
                held.append(claim)
                continue
            owner = process_start.process_start(record['pid'])
            if isinstance(owner, (process_start.Gone, process_start.Zombie)) or (
                    isinstance(owner, process_start.Started) and owner.start != record['proc_start']):
                reference = placement.ProviderClaimReference('ssh', target.name, claim.scope,
                    execution, record['episode'], record['attempt'])
                release = partial(_release_claims, controller, execution, (claim,), detached_only=True)
                if claim_admission_refused(controller.directory, reference):
                    released = release()
                else:
                    closed = settle_unpublished_claim(controller.directory, reference, release_claim=release,
                        reason='The Hana start owner exited')
                    released = isinstance(closed, HanaSettled)
                if released:
                    continue
            held.append(claim)
        block = mac_test.blocking_claim_block(paths, key.session)
    adopted: list[placement.AdoptedProviderClaim] = []
    cleanup: list[placement.RetainedCleanupClaim] = []
    executions: list[tuple[placement.ProviderClaimReference, placement.ExecutionEvidence]] = []
    generation = (generation_text(HanaHostGeneration(snapshot.deployment.deployment,
        snapshot.boot.start_token, snapshot.boot.boot_id)) if isinstance(snapshot.boot, box.VerifiedBoot) else '')
    holder: placement.ResourceHolder = placement.ResourceHolderUnknown()
    diagnostic: placement.BusyProviderDiagnostic | placement.NoProviderDiagnostic = placement.NO_PROVIDER_DIAGNOSTIC
    own_claim = False
    for held_claim in held:
        record = held_claim.record
        reference = placement.ProviderClaimReference('ssh', target.name, held_claim.scope,
            record['claim_id'], record['episode'], record['attempt'])
        own_claim = own_claim or record['episode'] == request.episode
        diagnostic = placement.BusyProviderDiagnostic(target.name, record['what'], record['since'])
        session = OwningSession(record['submitting_session']) if record['submitting_session'] else NoOwningSession()
        if isinstance(session, OwningSession):
            holder = placement.ResourceHolderSession(session.session_id)
        current = any(claim.execution == record['claim_id'] for claim in snapshot.claims)
        if (not current or record.get('cleanup_pending') or not generation
                or record.get('instance_generation') != generation or 'remote_host' not in record):
            cleanup.append(placement.RetainedCleanupClaim(reference, 'Hana startup or execution settlement remains pending'))
        else:
            adopted.append(placement.AdoptedProviderClaim(reference,
                placement.ClaimHolderIdentity(store.host_name(), record['pid'], record['proc_start']), session,
                transport.host, generation))
        executions.append((reference, read_claim_execution(controller.directory, reference)))
    if any(not any(row.record['claim_id'] == claim.execution for row in held) for claim in snapshot.claims):
        return placement.HostEvidenceUnavailable(key, snapshot.observed_at, 'A lifecycle claim has no readable target claim')
    eligibility: placement.HostEligibility = placement.AllowedHost()
    if not isinstance(block, mac_test.NoMacBlock):
        eligibility = placement.TemporaryExclusion('blocked')
    elif (held or snapshot.claims) and not own_claim:
        eligibility = placement.TemporaryExclusion('busy')
    elif snapshot.state == box.Lifecycle.STARTING:
        eligibility = placement.TemporaryExclusion('starting')
    elif snapshot.state not in (box.Lifecycle.IDLE_STOPPED, box.Lifecycle.READY):
        eligibility = placement.TemporaryExclusion('recovery')
    elif isinstance(snapshot.operation, box.PendingOperation) and snapshot.operation.action == box.Action.STOP:
        eligibility = placement.TemporaryExclusion('recovery')
    elif snapshot.state == box.Lifecycle.READY and not generation:
        eligibility = placement.TemporaryExclusion('starting')
    elif snapshot.state == box.Lifecycle.IDLE_STOPPED:
        preview = hana_spend.preview_placement_start(ledger.CostLedger(controller.directory),
            controller.settings_path, configuration, controller.path)
        if isinstance(preview, hana_spend.PermitRefused):
            eligibility = placement.TemporaryExclusion(preview.reason)
    return placement.FreshHostEvidence(key, snapshot.observed_at, generation, eligibility,
        tuple(adopted), tuple(cleanup), tuple(executions), holder, diagnostic)
