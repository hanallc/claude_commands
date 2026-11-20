"""Publish the AWS Machines console page from the saved projection."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from html import escape
import json
from pathlib import Path
import time
from typing import TypedDict

from scripts.aws_target import ledger, machines_data, run_cost
from scripts.aws_target.aws_page_location import output_folder as output_folder
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.review_links import console_theme, console_tune, page_frame, review_link, setting_controls

PAGE_NAME = 'machines.html'
_MINIMUM = Decimal('1.00')
_CENT = Decimal('0.01')
_REGION_NAMES = {
    'af-south-1': 'South Africa', 'ap-east-1': 'Hong Kong', 'ap-east-2': 'Taiwan',
    'ap-northeast-1': 'Tokyo', 'ap-northeast-2': 'South Korea', 'ap-northeast-3': 'Osaka',
    'ap-south-1': 'Mumbai', 'ap-south-2': 'Hyderabad', 'ap-southeast-1': 'Singapore',
    'ap-southeast-2': 'Sydney', 'ap-southeast-3': 'Indonesia', 'ap-southeast-4': 'Melbourne',
    'ap-southeast-5': 'Malaysia', 'ap-southeast-6': 'New Zealand', 'ap-southeast-7': 'Thailand',
    'ca-central-1': 'Canada', 'ca-west-1': 'Alberta', 'eu-central-1': 'Germany',
    'eu-central-2': 'Switzerland', 'eu-north-1': 'Sweden', 'eu-south-1': 'Italy',
    'eu-south-2': 'Spain', 'eu-west-1': 'Ireland', 'eu-west-2': 'United Kingdom',
    'eu-west-3': 'France', 'il-central-1': 'Israel', 'me-central-1': 'United Arab Emirates',
    'me-south-1': 'Bahrain', 'mx-central-1': 'Mexico', 'sa-east-1': 'Brazil',
    'us-east-1': 'Virginia', 'us-east-2': 'Ohio', 'us-west-1': 'California', 'us-west-2': 'Oregon',
}


class _SliderChoice(TypedDict):
    ratio: float
    ceiling_cents: int
    step_index: int


class _MonthlyProjection(TypedDict):
    extra: float
    hours: float
    cost_per_hour: float | None
    shares: dict[int, float]


def page_url() -> str:
    """Return the Machines page URL after the page exists."""
    page = output_folder().resolve() / PAGE_NAME
    if not page.is_file():
        raise FileNotFoundError(f'Machines page is not available: {page}')
    return review_link.snapshot_url(review_link.review_root().resolve(), page)


def _price_pill(price: machines_data.SavedSpotPriceRead | machines_data.NoSavedSpotPriceRead) -> str:
    if isinstance(price, machines_data.NoSavedSpotPriceRead):
        return '<span class="price-pill">No saved Spot prices</span>'
    read = datetime.fromtimestamp(price.read_at, UTC)
    if price.aws_closed:
        state = 'AWS is closed. Last saved prices'
    else:
        state = 'Spot prices'
    local = read.astimezone(user_zone())
    return (f'<span class="price-pill">{state} · '
            f'<time datetime="{read.isoformat()}">{local:%d %b %Y, %H:%M %Z}</time></span>')


def _machine_name(machine: ledger.GateRateCandidate, reference: ledger.GateRateCandidate) -> str:
    region = _REGION_NAMES.get(machine.region, 'Other region')
    label = 'Lowest-cost machine' if machine == reference else f'{ledger.gate_vcpus(machine.instance_type)} vCPU machine'
    return (f'{label} · {escape(region)}'
            f'<small>{escape(machine.instance_type)} · {escape(machine.region)}</small>')


def _duration(seconds: Decimal | run_cost.NoBoxTimeMeasurement) -> str:
    if not isinstance(seconds, Decimal):
        return '—'
    minutes, remainder = divmod(round(seconds), 60)
    return f'{minutes} min {remainder:02d} s' if minutes else f'{remainder} s'


def _usd(amount: Decimal | run_cost.NoPricedGateVariant | machines_data.NoCompletedChecks |
         machines_data.NoHoursSaved, places: int = 2) -> str:
    if not isinstance(amount, Decimal):
        return '—'
    rounded = amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f'${rounded:,.{places}f}'


def _row(cells: tuple[str, ...], labels: tuple[str, ...], *, attributes: str = '') -> str:
    rest = ''.join(f'<td data-label="{label}"><div>{cell}</div></td>'
                   for label, cell in zip(labels[1:], cells[1:], strict=True))
    return f'<tr{attributes}><th scope="row">{cells[0]}</th>{rest}</tr>'


def _table(labels: tuple[str, ...], rows: str, identity: str) -> str:
    headers = ''.join(f'<th scope="col">{label}</th>' for label in labels)
    return f'<table id="{identity}"><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table>'


def _where_checks_run(workload: machines_data.MeasuredWorkload) -> str:
    labels = ('Machine', 'Time', 'Cost per run', 'Cost ratio', 'Allowed', 'Free machine', 'Share of runs')
    reference = next(choice for choice in workload.choices if choice.machine == workload.reference)
    base = reference.usd_per_check
    rows: list[str] = []
    for index, choice in enumerate(workload.choices):
        cost = choice.usd_per_check
        ratio = '—'
        if isinstance(cost, Decimal) and isinstance(base, Decimal):
            if base:
                ratio = f'{cost / base:.2f}×'
            elif not cost:
                ratio = '1.00×'
        rows.append(_row((_machine_name(choice.machine, workload.reference), _duration(choice.median_seconds),
                          _usd(cost, 4), ratio, '<span data-allowed>—</span>', f'{choice.free_share:.1%}',
                          '<strong data-share>—</strong>'), labels, attributes=f' data-choice="{index}"'))
    return (f'<section class="card"><h2>Where the jobs run · {escape(workload.name)}'
            + ' at <span id="where-ratio"></span></h2>'
            + _table(labels, ''.join(rows), 'machine-shares') + '</section>')


def _measurements(workload: machines_data.MeasuredWorkload, data: machines_data.MachinesPageData) -> str:
    labels = ('Machine', 'Runs', 'Median time', 'Price per hour', 'Cost per run', 'Capacity refusals', 'Quota')
    rows: list[str] = []
    for choice in workload.choices:
        count = f'{choice.checks:,}'
        if choice.small_sample:
            count += '<small class="sample">Small sample</small>'
        quota = f'{choice.quota_vcpus:,} vCPUs' + ('<small>Assumed</small>' if choice.quota_assumed else '')
        rows.append(_row((_machine_name(choice.machine, workload.reference), count, _duration(choice.median_seconds),
                          _usd(choice.usd_per_hour, 4), _usd(choice.usd_per_check, 4),
                          str(choice.capacity_refusals), quota), labels))
    end = data.window_end - timedelta(days=1)
    notes = ''.join(f'<li>{escape(note)}</li>' for note in workload.assumptions)
    name = escape(workload.name)
    return (f'<section class="card"><h2>What we measured for {name}</h2>'
            + f'<p class="note">{workload.checks:,} completed runs for {name} from {data.window_start:%d %b %Y}'
            + f' to {end:%d %b %Y} UTC. The estimate uses {workload.checks_per_month:,.0f} runs per month.'
            + f' The lowest-cost machine costs {_usd(workload.reference_usd_per_month)} a month at that rate.</p>'
            + _table(labels, ''.join(rows), 'machine-measurements')
            + f'<h3>What the estimate assumes</h3><ul class="assumptions">{notes}</ul></section>')


def _steps(workload: machines_data.MeasuredWorkload, decimals: int) -> str:
    labels = ('Limit', 'Adds', 'Extra per month', 'Hours saved', 'Cost per hour saved')
    rows: list[str] = []
    for index, step in enumerate(workload.steps):
        limits = f'{step.first_ratio:.{decimals}f}'
        if step.first_ratio != step.last_ratio:
            limits += f'–{step.last_ratio:.{decimals}f}'
        button = f'<button type="button">{limits}</button>'
        added = '<br>'.join(_machine_name(machine, workload.reference) for machine in step.added)
        rows.append(_row((button, added or 'Lowest-cost machine only', _usd(step.extra_usd_per_month),
                          f'{step.hours_saved_per_month:,.1f}', _usd(step.usd_per_hour_saved)),
                         labels, attributes=f' data-step="{index}"'))
    return _table(labels, ''.join(rows), 'machine-steps')


def _projection_data(workload: machines_data.MeasuredWorkload) -> tuple[list[_SliderChoice], list[_MonthlyProjection]]:
    choices: list[_SliderChoice] = []
    projections: list[_MonthlyProjection] = []
    machines = {choice.machine: index for index, choice in enumerate(workload.choices)}
    for index, step in enumerate(workload.steps):
        for limit in step.limits:
            ceiling = (limit.ceiling_extra_usd_per_month / _CENT).to_integral_value(rounding=ROUND_CEILING)
            choices.append(_SliderChoice(ratio=float(limit.ratio), ceiling_cents=int(ceiling), step_index=index))
        projections.append(_MonthlyProjection(
            extra=float(step.extra_usd_per_month), hours=float(step.hours_saved_per_month),
            cost_per_hour=float(step.usd_per_hour_saved) if isinstance(step.usd_per_hour_saved, Decimal) else None,
            shares={machines[row.machine]: float(row.share) for row in step.shares},
        ))
    return choices, projections


def _controls(data: machines_data.MachinesPageData, workload: machines_data.MeasuredWorkload) -> str:
    live = workload.live_ratio
    live_step = next((step for step in reversed(workload.steps) if step.first_ratio <= live), workload.steps[0])
    initial = next((limit for limit in reversed(live_step.limits) if limit.ratio <= live), live_step.limits[0])
    live_extra = (live_step.extra_usd_per_month if _MINIMUM <= live <= data.ratio_max
                  else workload.live_extra_usd_per_month)
    decimals = max(2, len(format(data.ratio_step.normalize(), 'f').partition('.')[2]))
    ceiling = initial.ceiling_extra_usd_per_month.quantize(_CENT, rounding=ROUND_CEILING)
    slider = setting_controls.slider('cost-limit', 'Cost limit for one run', minimum=_MINIMUM,
        maximum=data.ratio_max, step=data.ratio_step, value=initial.ratio, live=live,
        decimals=decimals, live_note='off' if live == _MINIMUM else '')
    amount = setting_controls.amount_box('monthly-limit', 'I am willing to pay up to', 'more a month to go faster.',
        minimum=Decimal(0), step=_CENT, value=ceiling)
    go = setting_controls.go_button('save-limit', f'Set the limit for {workload.name}',
        page=console_tune.AWS_MACHINES_PAGE, key=console_tune.FASTER_BOX_RATIO_KEY)
    choices, projections = _projection_data(workload)
    model = json.dumps({'choices': choices, 'projections': projections, 'liveExtra': float(live_extra)})
    name = escape(workload.name)
    return (
        f'<div id="machine-controls" class="machine-controls" data-workload-name="{name}">{slider}{amount}</div>'
        + ('<div class="machine-tiles" aria-live="polite">'
        '<div><span>Extra cost for a month, estimate</span><strong id="extra-cost">—</strong></div>'
        '<div><span>Time saved for a month</span><strong id="saved-hours">—</strong></div>'
        '<div><span>Cost of one hour saved</span><strong id="saved-hour-cost">—</strong></div></div>'
        '<p id="monthly-cap" class="monthly-cap"></p>') + go
        + f'<script type="application/json" id="machines-model">{model}</script>'
        + ('<div class="machine-breakdown"><figure><figcaption>Extra cost for a month</figcaption>'
        '<canvas id="machine-chart" aria-hidden="true"></canvas>'
        '<div class="chart-key"><span>Estimate</span><span>Ceiling at this run count</span></div></figure>')
        + _steps(workload, decimals) + '</div>')


def render(data: machines_data.MachinesPageData, now: int) -> str:
    """Render the page using one saved projection."""
    started = time.perf_counter()
    control_section = ''
    other_workloads: list[str] = []
    for workload in data.workloads:
        if workload.mode == 'merge-gate' and isinstance(workload, machines_data.MeasuredWorkload):
            control_section = (
                '<p class="rule">The limit compares the cost of one run with the lowest-cost measured machine. '
                + 'A run uses the cheapest allowed faster machine that is free. At 1.00, the faster machine rule is off.</p>'
                + f'<section class="card"><h2>{escape(workload.name)}</h2>'
                + _controls(data, workload) + '</section>' + _where_checks_run(workload) + _measurements(workload, data))
        else:
            other_workloads.append(f'<p>{escape(workload.sentence)}</p>')
    source = console_theme.with_theme(Path(__file__).with_name(PAGE_NAME).read_text(encoding='utf-8'))
    body = page_frame.template(source, 'AWS Machines', console_tune.AWS_MACHINES_PAGE, action=_price_pill(data.price_read))
    page = body.replace('__PRIMARY_WORKLOAD__', control_section, 1).replace(
        '__OTHER_WORKLOADS__', ''.join(other_workloads), 1)
    return page_frame.rendered(page.encode(), started, now).decode()


def publish(directory: Path, now: int, folder: Path) -> Path:
    """Write the page beside the AWS prices page."""
    page = folder / PAGE_NAME
    data = machines_data.read_machines_data(directory, now)
    folder.mkdir(parents=True, exist_ok=True)
    write_text_atomic(page, render(data, now), new_file_mode=0o644)
    return page
