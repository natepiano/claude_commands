"""Chart boundaries, uncertainty, and publication over isolated history."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import charts
import costs
import index
import moments
import record
import test_moments as fixtures
from test_holds import hold_event
from test_index import Record, call, ci_job, ci_run


class ChartTests(unittest.TestCase):
    fixture: fixtures.MomentTests  # pyright: ignore[reportUninitializedInstanceVariable]

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.targets: mock.Mock = mock.Mock(return_value=())

    @override
    def setUp(self) -> None:
        self.fixture = fixtures.moment_fixture(self)
        _ = cast(object, self.enterContext(mock.patch.object(record, "configured_targets", self.targets)))

    def label_page(self) -> str:
        events = tuple(moments.BuildMoment(fixtures.START+timedelta(hours=1, seconds=number*10),
                                          f"Change {number}", "all") for number in range(4))
        self.fixture.progress([
            {"event_type": "phase_finished", "phase_instance_id": f"phase-{day}-{duration}",
             "working_dir": "/r/hana", "timestamp_epoch": fixtures.START.timestamp()+day*86400+3600,
             "phase_started_at": fixtures.START.timestamp()+day*86400+3600-duration}
            for day in range(2) for duration in (1200, 2400)
        ])
        coverage = tuple(costs.IndexedSourceCoverage(machine, fixtures.START.timestamp(), fixtures.END.timestamp())
                         for machine in ("natedev", "mac", "CI"))
        with closing(self.fixture.connection(*[
            fixtures.build(f"{machine}-{day}", day*86400+7200, target=machine,
                           mem_wait_s=3600*(day+1) if machine == "natedev" else 0)
            for machine in ("natedev", "mac", "CI") for day in range(2)
        ])) as connection, mock.patch.object(moments, "MOMENTS", events), \
             mock.patch.object(costs, "source_coverage", return_value=coverage):
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        return charts.render_html(series)

    def rendered_layout(self, page: str, probe: str, width: int) -> str:
        path = self.fixture.fixture.root/"label-layout.html"
        script = ("<script>const failures=[];"+probe+
                  ";const result=document.createElement('pre');result.id='layout-result';"+
                  "result.textContent=failures.length?failures.join('\\n'):'PASS';document.body.append(result);</script>")
        _ = path.write_text(page+script)
        result = subprocess.run([
            "google-chrome", "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--virtual-time-budget=300", f"--window-size={width},4000",
            "--dump-dom", path.as_uri(),
        ], capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r'<pre id="layout-result">(.*?)</pre>', result.stdout, re.S)
        self.assertIsNotNone(match, "The browser did not complete the layout probe.")
        assert match is not None
        return html.unescape(match[1])

    def machine_label_page(self, values: dict[str, tuple[float, ...]]) -> str:
        setup = r"""
const source=data.buckets[0],day=86400000;
data.moments=[];data.machines=Object.keys(fixtureValues);
data.end=data.start+Object.values(fixtureValues)[0].length*day;
data.buckets=Object.values(fixtureValues)[0].map((_,i)=>({...structuredClone(source),
 start:data.start+i*day,end:data.start+(i+1)*day,
 label:new Date(data.start+i*day).toISOString().slice(0,10).replaceAll('-',''),
 rows:Object.entries(fixtureValues).map(([machine,values])=>({machine,cause:data.causes[0],
 daily:{state:'measured',value:values[i]},perPhase:{state:'measured',value:values[i]}}))}));
redraw();
"""
        return self.label_page()+"<script>const fixtureValues="+json.dumps(values)+";"+setup+"</script>"

    def test_rendered_close_endpoints_with_unequal_values_never_share_a_value_label(self) -> None:
        page = self.machine_label_page({"natedev": (1000, 100, 100), "mac": (0, 1, 1), "CI": (0, 1.5, 1.5)})
        # Keep the false group visible beside the higher natedev endpoint.
        page = page.replace("</head>", "<style>svg .end-label{font-size:9px}</style></head>")
        probe = r"""
const svg=document.getElementById('machinePlot'),labels=[...svg.querySelectorAll('.end-label')];
if(!labels.length)failures.push('Missing endpoint labels');
for(const label of labels){const members=JSON.parse(label.dataset.members),values=members.map(name=>fixtureValues[name].at(-1));
 if(values.some(value=>value!==values[0]))failures.push('Unequal values share a label: '+label.textContent)}
for(const name of ['mac','CI'])if(!document.getElementById('machineLegend').textContent.includes(name))failures.push('Missing legend identity');
if(!document.getElementById('machineTable').textContent.includes('1.5 h/day'))failures.push('The table lost the CI value');
"""
        for width in (1280, 500):
            with self.subTest(width=width):
                self.assertEqual(self.rendered_layout(page, probe, width), "PASS")

    def test_rendered_gap_notes_yield_to_endpoint_labels_when_their_boxes_collide(self) -> None:
        page = self.machine_label_page({"natedev": (4,)+(2,)*29, "mac": (1.6,)*30, "CI": (0,)*30})
        probe = r"""
const missing=data.buckets.at(-1).rows.find(row=>row.machine==='natedev');
missing.daily={state:'unavailable',reason:'missing fixture coverage'};redraw();
const svg=document.getElementById('machinePlot'),labels=[...svg.querySelectorAll('.end-label')];
for(const name of ['natedev','mac'])if(!labels.some(label=>JSON.parse(label.dataset.members).includes(name)))failures.push('Missing endpoint label: '+name);
const overlaps=(a,b)=>a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;
for(const note of svg.querySelectorAll('.gap-label'))for(const label of labels)if(overlaps(note.getBoundingClientRect(),label.getBoundingClientRect()))failures.push('Gap note overlaps '+label.textContent);
if(!document.getElementById('machineTable').textContent.includes('missing fixture coverage'))failures.push('The table lost the gap reason');
"""
        for width in (1280, 500):
            with self.subTest(width=width):
                self.assertEqual(self.rendered_layout(page, probe, width), "PASS")

    def test_rendered_moment_numbers_have_a_band_clear_of_every_panel_title(self) -> None:
        page = self.label_page()
        probe = r"""
const titles=[...document.querySelectorAll('.panel-title')],numbers=[...document.querySelectorAll('.moment-number')];
if(titles.length!==6||numbers.length!==32)failures.push('Missing fixture titles or moment numbers');
function overlaps(a,b){return a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top}
for(const number of numbers)for(const title of titles)if(overlaps(number.getBoundingClientRect(),title.getBoundingClientRect()))failures.push('Moment '+number.textContent+' overlaps '+title.textContent);
for(const panel of document.querySelectorAll('.plot-panel')){const marks=[...panel.querySelectorAll('.moment-number')];for(let i=0;i<marks.length;i++)for(let j=i+1;j<marks.length;j++)if(overlaps(marks[i].getBoundingClientRect(),marks[j].getBoundingClientRect()))failures.push('Moment numbers overlap')}
"""
        for width in (1280, 500):
            with self.subTest(width=width):
                self.assertEqual(self.rendered_layout(page, probe, width), "PASS")

    def test_rendered_end_labels_stay_nearest_their_series_and_group_equal_endpoints(self) -> None:
        page = self.label_page()
        probe = r"""
for(const svg of document.querySelectorAll('#phase,#machinePlot')){
 const ends=new Map();for(const mark of svg.querySelectorAll('.series-point'))ends.set(mark.dataset.series,mark);
 const labels=[...svg.querySelectorAll('.end-label')];if(!ends.size||!labels.length)failures.push('Missing rendered endpoints or labels');
 const distance=(label,point)=>Math.hypot(Number(label.getAttribute('x'))-Number(point.getAttribute('cx')),Number(label.getAttribute('y'))-Number(point.getAttribute('cy')));
 for(const label of labels){const members=JSON.parse(label.dataset.members||'[]');if(!members.length)failures.push('Missing label identities');
 for(const member of members){const own=ends.get(member);if(!own||!label.firstChild.textContent.includes(member)){failures.push('Missing visible member '+member);continue}const nearest=Math.min(...[...ends.values()].map(point=>distance(label,point)));if(distance(label,own)>nearest+.1)failures.push(member+' label is nearer another endpoint')}
 }
}
const machine=document.getElementById('machinePlot'),group=[...machine.querySelectorAll('.end-label')].find(label=>label.firstChild.textContent==='mac, CI · 0');
if(!group)failures.push('Equal zero endpoints need one named group');
if(!machine.querySelector('.overlap-mark[data-series="mac"]')||!machine.querySelector('.overlap-mark[data-series="CI"]'))failures.push('An overlapping series has no visible mark');
for(const mark of machine.querySelectorAll('.overlap-mark'))if(mark.getAttribute('stroke-width')!=='2')failures.push('Missing surface ring');
"""
        for width in (1280, 500):
            with self.subTest(width=width):
                self.assertEqual(self.rendered_layout(page, probe, width), "PASS")

    def test_passed_tree_pairs_cross_midnight_and_the_first_edge(self) -> None:
        records = [fixtures.build("context", -60, duration_s=120, session="same", tree_key="tree", target="natedev"),
                   fixtures.build("first", 90, duration_s=60, session="same", tree_key="tree", target="natedev"),
                   fixtures.build("before-midnight", 86300, duration_s=60, session="same", tree_key="tree", target="natedev"),
                   fixtures.build("after-midnight", 86410, duration_s=120, session="same", tree_key="tree", target="natedev")]
        with closing(self.fixture.connection(*records)) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        repeats = [[row.seconds for row in bucket.costs.ranked if row.cause == "Reruns after a pass"] for bucket in series.buckets]
        self.assertEqual(repeats, [[60], [120]])
        self.assertEqual(sum(row.seconds for bucket in series.buckets for row in bucket.costs.ranked
                             if row.cause == "Memory wait"), 0)

    def test_context_uses_the_last_run_for_each_machine_and_session(self) -> None:
        with closing(self.fixture.connection(
            fixtures.build("old-pass", -600, target="natedev", session="shared", tree_key="tree"),
            fixtures.build("latest-failure", -120, target="natedev", session="shared", tree_key="tree", status=1),
            fixtures.build("other-machine", -60, target="mac", session="shared", tree_key="tree"),
            fixtures.build("local", 60, target="natedev", session="shared", tree_key="tree"),
            fixtures.build("remote", 120, target="mac", session="shared", tree_key="tree", duration_s=120),
        )) as connection:
            reading = costs.read_costs(connection, fixtures.START, fixtures.END, boundary_context=True)
        self.assertEqual([(row.machine, row.seconds) for row in reading.contributions
                          if row.cause == "Reruns after a pass"], [("mac", 120)])

    def test_window_and_context_plans_search_indexes_without_history_scans(self) -> None:
        with closing(self.fixture.connection(
            fixtures.build("before", -60, target="natedev", session="shared", tree_key="tree"),
            fixtures.build("after", 60, target="natedev", session="shared", tree_key="tree"),
        )) as connection:
            for context in (False, True):
                with self.subTest(boundary_context=context):
                    statements: list[str] = []
                    connection.set_trace_callback(statements.append)
                    _ = costs.read_costs(connection, fixtures.START, fixtures.END, boundary_context=context)
                    connection.set_trace_callback(None)
                    query = next(sql for sql in statements if sql.startswith("WITH window_steps"))
                    plan = cast(list[tuple[int, int, int, str]],
                                connection.execute("EXPLAIN QUERY PLAN " + query).fetchall())
                    details = [row[3] for row in plan]
                    self.assertTrue(any("SEARCH steps USING INDEX steps_started" in row for row in details), details)
                    self.assertTrue(any("SEARCH calls USING COVERING INDEX calls_copy" in row for row in details), details)
                    self.assertFalse(any(row.startswith(("SCAN steps", "SCAN prior", "SCAN calls", "SCAN main.calls"))
                                         for row in details), details)
                    if context:
                        self.assertTrue(any("SEARCH prior USING INDEX steps_session_previous" in row for row in details), details)
                        self.assertTrue(any("steps_execution_end" in row for row in details), details)
                        self.assertTrue(any("steps_wait_started" in row for row in details), details)

    def test_execution_from_before_window_covers_wait_across_midnight(self) -> None:
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("mac", -3600, target="mac", duration_s=93600),
            fixtures.build("wait", 93600, target="natedev", mem_wait_s=90000, call_id="eligible"),
        )) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual([b.costs.mac_idle.candidate_wait_seconds for b in series.buckets], [82800, 7200])
        self.assertEqual([b.costs.mac_idle.execution_overlap_seconds for b in series.buckets], [82800, 3600])
        self.assertEqual([b.costs.mac_idle.upper_bound_seconds for b in series.buckets], [0, 3600])

    def test_parallel_waits_split_without_duplicate_seconds(self) -> None:
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("a", 90000, target="natedev", mem_wait_s=7200, call_id="eligible"),
            fixtures.build("b", 91800, target="natedev", mem_wait_s=7200, call_id="eligible"),
        )) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual([b.costs.mac_idle.upper_bound_seconds for b in series.buckets], [3600, 5400])
        self.assertEqual([sum(r.seconds for r in b.costs.ranked if r.cause == "Memory wait") for b in series.buckets], [0, 14400])

    def test_complete_holds_split_and_unmatched_starts_keep_the_unknown_band(self) -> None:
        origin = fixtures.START
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("wait", 93600, target="natedev", mem_wait_s=14400, call_id="eligible"),
            hold_event("began", "lost", (origin+timedelta(seconds=79000)).isoformat(), "mac"),
            hold_event("began", "lost", (origin+timedelta(seconds=85000)).isoformat(), "mac"),
            hold_event("ended", "lost", (origin+timedelta(seconds=88000)).isoformat(), "mac"),
        )) as connection, mock.patch.object(costs, "source_coverage", return_value=(
            costs.IndexedSourceCoverage("mac", origin.timestamp(), fixtures.END.timestamp()),
        )), mock.patch.object(costs, "first_source_times", return_value={"mac": origin.timestamp()}):
            series = charts.collect_charts(connection, origin, fixtures.END, ZoneInfo("UTC"))
        idle = [b.costs.mac_idle for b in series.buckets]
        self.assertEqual([i.recorded_hold_seconds for i in idle], [1400, 1600])
        self.assertEqual([i.unknown_seconds for i in idle], [5800, 0])
        value = charts.cost_value(series.buckets[0], "mac", "Mac idle while natedev waits")
        self.assertEqual(value, charts.MacIdleUpperBound(0, 5800))
        page = charts.render_html(series)
        self.assertIn("Hatched bands show unknown idle time", page)
        self.assertIn('"state": "upper bound"', page)

    def test_missing_pair_fields_never_create_a_false_rerun(self) -> None:
        for missing in ({"session": None}, {"tree_key": None}, {"status": None}, {"duration_s": None}):
            with self.subTest(missing=missing), closing(self.fixture.connection(
                fixtures.build("before", -60, **{"session":"same", "tree_key":"tree", "target":"natedev", **missing}),
                fixtures.build("after", 60, session="same", tree_key="tree", target="natedev"),
            )) as connection:
                result = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
                self.assertEqual([row for b in result.buckets for row in b.costs.ranked if row.cause == "Reruns after a pass"], [])

    def test_exact_edges_assign_steps_phases_ci_and_moments_to_the_later_bucket(self) -> None:
        edge = fixtures.START+timedelta(days=1)
        self.fixture.progress([{"event_type": "phase_finished", "phase_instance_id": "edge", "working_dir": "/r/hana",
                                "timestamp_epoch": edge.timestamp(), "phase_started_at": edge.timestamp()-60}])
        job = ci_job(1, "build", "cancelled", (edge.isoformat(), edge.isoformat(), (edge+timedelta(hours=1)).isoformat()))
        run = ci_run(1, 1, [job])
        run.update(started_at=edge.isoformat(), created_at=edge.isoformat(), updated_at=(edge+timedelta(hours=1)).isoformat(), conclusion="cancelled")
        events: tuple[moments.Moment, ...] = (moments.BuildMoment(edge, "edge", "all"),
            moments.MemoryMoment(fixtures.END, "excluded"), moments.PendingMemoryMoment("pending"))
        with closing(self.fixture.connection(
            run, fixtures.build("at-edge", 86400, mem_wait_s=120, target="natedev"),
            fixtures.build("at-end", 172800, mem_wait_s=999, target="natedev"),
        )) as connection, mock.patch.object(moments, "MOMENTS", events):
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual([len(b.costs.phases) for b in series.buckets], [0, 1])
        self.assertEqual([sum(r.seconds for r in b.costs.ranked if r.cause == "Cancelled CI") for b in series.buckets], [0, 3600])
        self.assertEqual([sum(r.seconds for r in b.costs.ranked if r.cause == "Memory wait") for b in series.buckets], [0, 120])
        data = charts.chart_data(series)
        self.assertEqual(data["moments"], [{"at": edge.timestamp()*1000, "name": "edge", "scope": "all", "local": "Oct 11 00:00 UTC"}])
        self.assertEqual(data["pending"], ["pending: not live yet"])

    def test_calendar_edges_keep_dst_duration_and_partial_buckets(self) -> None:
        zone = ZoneInfo("America/New_York")
        start = datetime(2026, 10, 31, 12, tzinfo=zone).astimezone(UTC)
        end = datetime(2026, 11, 2, 12, tzinfo=zone).astimezone(UTC)
        edges = charts.bucket_edges(start, end, zone)
        self.assertEqual([(b-a).total_seconds()/3600 for a,b in zip(edges, edges[1:])], [12, 25, 12])
        reading = costs.CostReading((costs.CostContribution(edges[1].timestamp(), "cloud", "Memory wait", 90000),),
                                    (costs.SourceObservation(edges[1].timestamp(), "cloud"),), (), ())
        series = charts.build_series(reading, start, end, zone)
        self.assertEqual([b.thin for b in series.buckets], [True, False, True])
        value = charts.cost_json(charts.cost_value(series.buckets[1], "cloud", "Memory wait"), 3600*25/24)
        self.assertEqual(value, {"state": "measured", "value": 24})
        spring = charts.bucket_edges(datetime(2026,3,8,5,tzinfo=UTC), datetime(2026,3,9,4,tzinfo=UTC), zone)
        self.assertEqual((spring[1]-spring[0]).total_seconds(), 23*3600)

    def test_identical_starts_use_the_existing_id_order(self) -> None:
        with closing(self.fixture.connection(
            fixtures.build("a", 100, duration_s=60, target="natedev", session="same", tree_key="tree"),
            fixtures.build("b", 100, duration_s=120, target="natedev", session="same", tree_key="tree"),
        )) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual([r.seconds for b in series.buckets for r in b.costs.ranked if r.cause == "Reruns after a pass"], [120])

    def test_phase_gaps_keep_missing_starts_and_per_phase_unavailable(self) -> None:
        self.fixture.progress([
            {"event_type": "phase_finished", "phase_instance_id": "missing", "working_dir": "/r/hana", "timestamp_epoch": fixtures.START.timestamp()},
        ])
        with closing(self.fixture.connection(fixtures.build("record", 10, target="natedev", mem_wait_s=3600))) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        buckets = cast(list[dict[str, object]], charts.chart_data(series)["buckets"])
        self.assertEqual(buckets[0]["phase"], {"finished": 1, "valid": 0, "missing": 1, "state": "unavailable", "reason": "no valid durations"})
        self.assertEqual(buckets[1]["phase"], {"finished": 0, "valid": 0, "missing": 0, "state": "unavailable", "reason": "no finished phases"})
        self.assertEqual(charts.cost_value(series.buckets[1], "natedev", "Memory wait", per_phase=True),
                         charts.CostUnavailable("no finished phases"))
        self.assertEqual(charts.cost_value(series.buckets[0], "natedev", "Memory wait", per_phase=True), charts.MeasuredCost(3600))

    def test_unknown_machine_coverage_and_recorded_zero_are_distinct(self) -> None:
        with closing(self.fixture.connection(fixtures.build("cloud", 10, host="new-cloud", target="aws-new", mem_wait_s=0))) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual(series.machines, ("aws-new",))
        self.assertEqual(charts.cost_value(series.buckets[0], "aws-new", "Memory wait"), charts.MeasuredCost(0))
        self.assertEqual(charts.cost_value(series.buckets[1], "aws-new", "Memory wait"), charts.CostUnavailable("no recorded source coverage"))

    def monthly_mac_log(self, *records: Record) -> None:
        self.fixture.fixture.write(*records)
        path = self.fixture.fixture.root/"mac/2026-10.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = (self.fixture.fixture.root/"natedev/profiles.jsonl").replace(path)
        _ = index.update()

    def test_complete_indexed_mac_log_proves_zero_on_a_day_without_steps(self) -> None:
        self.monthly_mac_log(fixtures.build("before", 10, target="mac"),
                             fixtures.build("after", 172810, target="mac"))
        with closing(index.read_only()) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END+timedelta(days=1), ZoneInfo("UTC"))
        self.assertEqual(charts.cost_value(series.buckets[1], "mac", "Memory wait"), charts.MeasuredCost(0))
        self.assertEqual(charts.cost_value(series.buckets[1], "mac", "Failed runs"), charts.MeasuredCost(0))

    def test_missing_or_unindexed_mac_log_keeps_the_gap_and_names_the_source(self) -> None:
        self.monthly_mac_log(fixtures.build("before", 10, target="mac"),
                             fixtures.build("after", 172810, target="mac"))
        path = self.fixture.fixture.root/"mac/2026-10.jsonl"
        with path.open("ab") as source:
            _ = source.write(b"{}\n")
        for state in ("unindexed", "missing"):
            with self.subTest(source=state), closing(index.read_only()) as connection:
                series = charts.collect_charts(connection, fixtures.START, fixtures.END+timedelta(days=1), ZoneInfo("UTC"))
                self.assertIsInstance(charts.cost_value(series.buckets[1], "mac", "Memory wait"), charts.CostUnavailable)
                buckets = cast(list[dict[str, object]], charts.chart_data(series)["buckets"])
                rows = cast(list[dict[str, object]], buckets[1]["rows"])
                row = next(row for row in rows if row["machine"] == "mac" and row["cause"] == "Memory wait")
                self.assertEqual(row["daily"], {"state": "unavailable", "reason": "no complete indexed mac monthly log covers this day"})
            path.unlink(missing_ok=True)

    def test_bucket_sums_match_shared_costs_and_each_source_reads_once(self) -> None:
        self.fixture.progress([{"event_type":"phase_finished", "phase_instance_id":"one", "working_dir":"/r/hana",
                                "timestamp_epoch":fixtures.START.timestamp()+1, "phase_started_at":fixtures.START.timestamp()}])
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("wait", 90000, mem_wait_s=7200, call_id="eligible", target="natedev", crates_compiled=50),
        )) as connection:
            expected = costs.collect_costs(connection, fixtures.START, fixtures.END)
            statements: list[str] = []
            connection.set_trace_callback(statements.append)
            before = index.index_path().read_bytes()
            with mock.patch.object(costs, "read_costs", wraps=costs.read_costs) as shared, \
                 mock.patch.object(moments, "progress_events", wraps=moments.progress_events) as progress, \
                 mock.patch.object(costs, "mac_hold_events", wraps=costs.mac_hold_events) as holds:
                series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
            self.assertEqual(shared.call_count, 1)
            self.assertEqual(progress.call_count, 1)
            self.assertEqual(holds.call_count, 1)
            self.assertEqual(len([sql for sql in statements if sql.startswith("WITH window_steps")]), 1)
            self.assertEqual(len([sql for sql in statements if sql.startswith("SELECT r.conclusion")]), 1)
            self.assertEqual(index.index_path().read_bytes(), before)
        for row in expected.ranked:
            self.assertAlmostEqual(sum(r.seconds for b in series.buckets for r in b.costs.ranked
                                       if (r.machine, r.cause) == (row.machine, row.cause)), row.seconds)

    def test_browserless_cli_publishes_one_standalone_page_without_index_writes(self) -> None:
        with closing(self.fixture.connection(fixtures.build("record", 10, mem_wait_s=60))):
            pass
        cache = self.fixture.fixture.root/"cache"
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME":str(cache), "BUILDLOG_CHARTS_NO_OPEN":"1"}):
            result = fixtures.read_only_cli(self, ["charts", "--days", "7"])
        path = cache/"buildlog/charts/latest.html"
        self.assertIn(str(path), result.stdout)
        page = path.read_text()
        self.assertEqual(page.count('<svg id='), 3)
        self.assertNotIn('<script src=', page)
        self.assertNotIn('https://', page)
        self.assertIn('prefers-color-scheme:dark', page)
        self.assertIn('role="img"', page)
        self.assertNotIn('@DATA@', page)

    def test_html_escapes_recorded_machine_and_moment_names(self) -> None:
        name = '</script><img src=x onerror=alert(1)>'
        at = fixtures.START+timedelta(hours=1)
        with closing(self.fixture.connection(fixtures.build("unsafe", 10, target=name))), \
             mock.patch.object(moments, "MOMENTS", (moments.BuildMoment(at, name, "all"),)):
            with closing(index.read_only()) as connection:
                page = charts.render_html(charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC")))
        self.assertNotIn(name, page)
        self.assertIn('\\u003c/script', page)

    def test_invalid_cli_window_never_opens_the_index(self) -> None:
        with mock.patch.object(index, "read_only", side_effect=AssertionError("index opened")):
            self.assertEqual(charts.main(["--days", "8"]), 2)

    def test_absent_mac_coverage_keeps_candidate_waits_unknown_in_both_views(self) -> None:
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("wait", 7200, target="natedev", mem_wait_s=3600, call_id="eligible"),
        )) as connection:
            reading = costs.read_costs(connection, fixtures.START, fixtures.END)
        result = costs.aggregate_costs(reading, fixtures.START, fixtures.END)
        self.assertEqual(result.mac_idle.unknown_seconds, 3600)
        self.assertEqual(result.mac_idle.upper_bound_seconds, 3600)
        self.assertIn("upper bound; 0.50 h/day unknown", costs.render_costs(result))
        series = charts.build_series(reading, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual(charts.cost_value(series.buckets[0], "mac", "Mac idle while natedev waits"),
                         charts.MacIdleUpperBound(0, 3600))

    def test_applicable_missing_waits_keep_mixed_buckets_unavailable(self) -> None:
        for records, missing, expected in (
            ([fixtures.build("missing", 10, target="natedev", mem_wait_s=None)], 1, charts.CostUnavailable("1 applicable waits missing")),
            ([fixtures.build("zero", 10, target="natedev", mem_wait_s=0)], 0, charts.MeasuredCost(0)),
            ([fixtures.build("missing", 10, target="natedev", mem_wait_s=None),
              fixtures.build("measured", 20, target="natedev", mem_wait_s=60)], 1, charts.CostUnavailable("1 applicable waits missing")),
            ([fixtures.build("launch", 10, target="natedev", step="launch", mem_wait_s=None)], 0, charts.MeasuredCost(0)),
        ):
            fixture = fixtures.moment_fixture(self)
            with self.subTest(records=records), closing(fixture.connection(*records)) as connection:
                series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
                self.assertEqual(charts.cost_value(series.buckets[0], "natedev", "Memory wait"), expected)
                text = costs.render_costs(series.buckets[0].costs)
                self.assertEqual("applicable waits missing" in text, missing > 0)
                self.assertEqual(sum(row.missing_waits for row in series.buckets[1].costs.ranked), 0)

    def test_configured_machines_without_events_keep_named_unavailable_values(self) -> None:
        with mock.patch.object(record, "configured_targets", return_value=("natedev", "mac", "aws")), \
             mock.patch.object(costs, "first_source_times", return_value={machine: fixtures.START.timestamp()-1
                                                                       for machine in ("natedev", "mac", "aws")}), \
             closing(self.fixture.connection()) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        self.assertEqual(series.machines, ("aws", "mac", "natedev"))
        for machine in series.machines:
            with self.subTest(machine=machine):
                self.assertEqual(charts.cost_value(series.buckets[0], machine, "Memory wait"),
                                 charts.CostUnavailable("no recorded source coverage"))
        self.assertIn('"aws"', charts.render_html(series))

    def configured_machine_series(self, *records: Record, targets: tuple[str, ...] = ("natedev", "aws"),
                                  start: datetime = fixtures.START, end: datetime = fixtures.END) -> charts.ChartSet:
        self.targets.return_value = targets
        with closing(self.fixture.connection(*records)) as connection:
            return charts.collect_charts(connection, start, end, ZoneInfo("UTC"))

    def test_all_machines_omit_a_configured_machine_until_its_first_source(self) -> None:
        series = self.configured_machine_series(
            fixtures.build("local", 10, target="natedev", mem_wait_s=3600),
            fixtures.build("later-local", 86410, target="natedev", mem_wait_s=1800),
            fixtures.build("later-cloud", 86420, target="aws", mem_wait_s=1800),
        )
        probe = r"""
const waits=causeSeries().find(series=>series.label==='Memory wait').values;
if(waits.some(point=>point.state!=='measured'||point.value!==1))failures.push('An inactive machine hides recorded costs');
"""
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 1280), "PASS")

    def test_machine_without_a_log_has_one_message_and_a_named_selector_option(self) -> None:
        series = self.configured_machine_series(fixtures.build("local", 10, target="natedev", mem_wait_s=3600))
        probe = r"""
const emptyOption=[...document.getElementById('machine').options].find(option=>option.value==='aws');
if(!emptyOption?.textContent.includes('no data yet'))failures.push('The selector hides the missing source');
document.getElementById('machine').value='aws';redraw();
const svg=document.getElementById('cause');
if(svg.querySelectorAll('text').length!==1||svg.textContent!=='aws: no data yet'||svg.querySelector('.plot-panel'))failures.push('The machine needs one no-data message');
"""
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 500), "PASS")

    def test_all_machines_retain_missing_applicable_waits_with_an_inactive_target(self) -> None:
        series = self.configured_machine_series(
            fixtures.build("missing", 10, target="natedev", mem_wait_s=None),
            fixtures.build("recorded", 20, target="natedev", mem_wait_s=3600),
        )
        probe = self.memory_wait_unavailable_probe("1 applicable waits missing")
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 1280), "PASS")

    def test_a_missing_historical_log_remains_required_without_steps_in_the_window(self) -> None:
        self.monthly_mac_log(fixtures.build("old-mac", -86400, target="mac"))
        self.fixture.fixture.write(fixtures.build("local", 10, target="natedev", mem_wait_s=3600))
        _ = index.update()
        (self.fixture.fixture.root/"mac/2026-10.jsonl").unlink()
        self.targets.return_value = ("natedev", "mac", "aws")
        with closing(index.read_only()) as connection:
            series = charts.collect_charts(connection, fixtures.START, fixtures.END, ZoneInfo("UTC"))
        probe = self.memory_wait_unavailable_probe("no complete indexed mac monthly log covers this day")
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 1280), "PASS")

    def memory_wait_unavailable_probe(self, reason: str) -> str:
        return ("const wait=causeSeries().find(series=>series.label==='Memory wait').values[0];"
                + "if(wait.state!=='unavailable'||wait.reason!==" + json.dumps(reason)
                + ")failures.push('The unavailable wait lost its reason');")

    def test_empty_indexed_monthly_log_keeps_the_machine_without_data(self) -> None:
        path = self.fixture.fixture.root/"aws/2026-11.jsonl"
        path.parent.mkdir(parents=True)
        path.touch()
        start = datetime(2026, 11, 1, tzinfo=UTC)
        series = self.configured_machine_series(
            fixtures.build("local", 10, target="natedev", mem_wait_s=3600, started_at=(start+timedelta(seconds=10)).isoformat()),
            start=start, end=start+timedelta(days=1),
        )
        probe = r"""
const wait=causeSeries().find(series=>series.label==='Memory wait').values[0];
if(wait.state!=='measured'||wait.value!==1)failures.push('An empty log hides measured costs');
const emptyOption=[...document.getElementById('machine').options].find(option=>option.value==='aws');
if(!emptyOption?.textContent.includes('no data yet'))failures.push('An empty log activates its machine');
document.getElementById('machine').value='aws';redraw();
if(document.getElementById('cause').textContent!=='aws: no data yet')failures.push('The empty machine needs one message');
"""
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 1280), "PASS")

    def test_other_group_omits_members_without_a_source_in_every_view(self) -> None:
        targets = ("natedev", "mac", "CI", "cloud-a", "cloud-b", "cloud-c", "cloud-d", "cloud-e", "cloud-f")
        series = self.configured_machine_series(
            fixtures.build("group-wait", 10, target="cloud-e", mem_wait_s=3600), targets=targets,
        )
        probe = r"""
document.getElementById('machine').value='Other';redraw();
const wait=causeSeries().find(series=>series.label==='Memory wait').values[0];
if(wait.state!=='measured'||wait.value!==1)failures.push('An inactive member hides its group');
if(!document.getElementById('causeTable').textContent.includes('1 h/day'))failures.push('The table hides its group');
const group=machineSeries().find(series=>series.label==='Other').values[0];
if(group.state!=='measured'||group.value!==1)failures.push('The machine plot hides its group');
"""
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 500), "PASS")

    def test_no_data_cells_agree_in_the_table_tooltip_and_detail(self) -> None:
        series = self.configured_machine_series(fixtures.build("local", 10, target="natedev", mem_wait_s=3600))
        probe = r"""
document.getElementById('machine').value='aws';redraw();
if(causeSeries().some(series=>series.values.some(point=>point.state!=='no data yet')))failures.push('The cell state differs');
for(const id of ['causeTable','causeDetail']){
 const text=document.getElementById(id).textContent;
 if(!text.includes('no data yet')||text.includes('no complete indexed aws'))failures.push(id+' hides the no-data state');
}
showTooltip(configs[1],0);
if(!document.getElementById('causeTooltip').textContent.includes('no data yet'))failures.push('The tooltip hides the no-data state');
if(document.getElementById('cause').viewBox.baseVal.width>1120)failures.push('The empty chart exceeds the width cap');
"""
        self.assertEqual(self.rendered_layout(charts.render_html(series), probe, 1280), "PASS")

    def test_cli_uses_the_user_zone_for_edges_and_moment_labels(self) -> None:
        event = moments.BuildMoment(datetime.now(UTC)-timedelta(hours=1), "zone marker", "all")
        with closing(self.fixture.connection()), \
             mock.patch.dict(os.environ, {"USER_ZONE": "UTC", "TZ": "America/Los_Angeles"}), \
             mock.patch.object(moments, "MOMENTS", (event,)), \
             mock.patch.object(charts, "publish") as publication:
            self.assertEqual(charts.main(["--no-open"]), 0)
        series = cast(charts.ChartSet, publication.call_args.args[0])
        self.assertEqual(series.zone, "UTC")
        self.assertTrue(all(bucket.start.hour == 0 for bucket in series.buckets[1:]))
        self.assertEqual(charts.chart_data(series)["moments"], [{
            "at": event.at.timestamp()*1000, "name": "zone marker", "scope": "all",
            "local": event.at.strftime("%b %d %H:%M UTC"),
        }])
